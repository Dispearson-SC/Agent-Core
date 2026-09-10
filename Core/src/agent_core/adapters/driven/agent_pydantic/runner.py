"""Driven adapter: AgentRunner over Pydantic AI.

Phase:   F1 (hooks, policy, audit) / F3 (deferred) / F5 (compaction) / F6 (MCP, skills)
Tasks:   docs/TASKS.md#t-f1-12, docs/TASKS.md#t-f5-07
Status:  ENFORCEMENT HOOK AND F1 RUNNER BODY IMPLEMENTED (t-f1-12)
         PROCESS-HISTORY WIRING IMPLEMENTED (t-f5-07)
         Suspension F3 / MCP and skills F6 / media F7 / peers F9
Implements: ports/agent_runner.py

THIS FILE IS THE SECURITY ENFORCEMENT POINT OF THE WHOLE SYSTEM
    Policy is consulted here. Audit is written here. Untrusted content is wrapped here.
    All three happen in `before_tool_execute`, i.e. BEFORE any side effect.

    Two of the five silent-bug areas in CLAUDE.md live in this file. Nothing here fails a
    test when it is wrong; it simply stops protecting.

WHY PYDANTIC AI PASSED THE SELECTION TEST (verified against its documentation)
    The question that decides any agent SDK: can I put my own code in the middle of a
    turn? Pydantic AI answers yes. Hooks exist at seven lifecycle stages, each with
    before_ / after_ / wrap_ / _error variants:

        run -> node -> model_request -> tool_validate -> tool_execute
            -> output_validate -> output_process

    `before_tool_execute` receives VALIDATED arguments, may modify them (return `args`),
    and may block execution entirely by raising `SkipToolExecution(result)`.

    That is the approval gate, without writing an approval gate. docs/DECISIONS.md#d3.

THE HOOK SKETCH - now `PolicyEnforcement` below (verified against pydantic-ai 2.31)

    async def before_tool_execute(ctx, *, call, tool_def, args):
        decision = policy.decide(rules, call.tool_name, args)          # snapshot, not caller
        await audit.record_tool_call(turn_id, caller, name, args, decision)   # BEFORE
        if decision.blocks_execution:
            raise SkipToolExecution(refusal_result(decision))
        return args

    THE CALLER IS NOT AN ARGUMENT TO `decide`. An older sketch in this file read
    `policy.decide(caller, call.tool_name, args)`; it predates the port freeze and matches
    no version of the settled contract. `ToolPolicy.decide` takes the RuleSet snapshot
    that `load_rules(caller)` already narrowed, precisely so no call site can ask caller
    A's rules about caller B - see ports/tool_policy.py. The caller is still passed to the
    AUDIT write, because the audit row must name who asked.

    TODO(F6): the untrusted-result half of the pair, using the constants below.

        async def after_tool_execute(ctx, *, call, tool_def, args, result):
            if is_untrusted(call.tool_name):        # mcp_*, web_*, browser_*, media_*
                return wrap_in_delimiters(truncate(result, budget_for(call.tool_name)))
            return result
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability, ProcessHistory, ValidatedToolArgs
from pydantic_ai.exceptions import SkipToolExecution, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from agent_core.domain.compaction import CompactionPolicy, ContextState
from agent_core.domain.policy import Effect, PolicyDecision, RuleSet
from agent_core.domain.profile import AgentProfile, profile_content_hash
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    Usage,
)
from agent_core.ports.agent_runner import ToolResolution
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.context_engine import ContextEngine
from agent_core.ports.model_gateway import ModelGateway
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.tool_provider import ToolProvider

# Tool-name prefixes whose results are third-party text the model will read: the natural
# vector for indirect prompt injection. Their results are ALWAYS wrapped in
# untrusted-content delimiters. CLAUDE.md non-negotiable #4.
UNTRUSTED_PREFIXES: Final[tuple[str, ...]] = ("mcp_", "web_", "browser_", "media_")

# Smaller result budget for MCP than for local tools. Hermes uses 50K against 100K, with
# the stated reason that MCP servers routinely return un-paginated 20-50K payloads.
MCP_RESULT_BUDGET_CHARS: Final[int] = 50_000
LOCAL_RESULT_BUDGET_CHARS: Final[int] = 100_000

# Case-insensitive so a differently-cased tag cannot forge or prematurely close the
# boundary. A model that has read a hostile page will try exactly that.
UNTRUSTED_OPEN: Final[str] = "<untrusted-tool-output>"
UNTRUSTED_CLOSE: Final[str] = "</untrusted-tool-output>"


def refusal_result(decision: PolicyDecision) -> str:
    """The text the MODEL receives in place of the tool result when policy blocks a call.

    `PolicyDecision.reason` is carried verbatim and is the whole payload that matters: the
    port docstring says the reason exists so the agent can adapt instead of retrying
    blindly, and paraphrasing it here would quietly disconnect what an auditor reads in
    the audit row from what the model was actually told.

    `rule_id` is deliberately NOT included. It is an internal identifier with no meaning to
    the model, and putting it in the conversation only invites the model to reason about
    the shape of the rule store in front of whoever it is talking to.
    """
    if decision.effect is Effect.NEEDS_APPROVAL:
        return f"Tool call is awaiting human approval and did not run. Reason: {decision.reason}"
    return f"Tool call refused by policy and did not run. Reason: {decision.reason}"


class PolicyEnforcement(AbstractCapability[Any]):
    """The `before_tool_execute` gate: policy, then audit, then - only then - the tool.

    Attached to a Pydantic AI `Agent` as a capability, so the ordering below is the
    ordering production runs. It is constructed per turn, because `rules` is the frozen
    snapshot `ToolPolicy.load_rules(caller)` returned once at turn start (D13) and
    `turn_id` names one turn. Reusing an instance across turns would file this turn's
    denials under the previous turn's id and answer "why was this allowed?" with the wrong
    story.

    THE ORDERING IS THE POINT, AND IT IS NOT A STYLE CHOICE
        The audit write happens BEFORE the raise. Written after, it lives on the far side
        of a control-flow exception: anything that goes wrong between the two - and an
        exception path is precisely where things go wrong - leaves a refusal that
        happened with no record that it did. CLAUDE.md non-negotiable #6 says the sink is
        append-only and that a failed turn must still leave a trace; a denial with no row
        is that failure in its purest form, and no test elsewhere in the suite sees it.

        Recorded is the INTENT and the VERDICT, never the outcome - see the port
        docstring. A tool about to run that then crashes the process must still appear.
    """

    def __init__(
        self,
        *,
        policy: ToolPolicy,
        rules: RuleSet,
        audit: AuditSink,
        turn_id: TurnId,
        caller: CallerIdentity,
    ) -> None:
        self._policy = policy
        self._rules = rules
        self._audit = audit
        self._turn_id = turn_id
        self._caller = caller

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        """Consult policy, record the verdict, and block the call when it is not ALLOW.

        `ctx` is unused on purpose. Every input to the verdict is already settled: the
        snapshot knows whose rules these are, and reaching into the run context for a
        second identity is the hole `RuleSet` exists to close.
        """
        arguments: dict[str, object] = dict(args)
        decision = self._policy.decide(self._rules, call.tool_name, arguments)

        # BEFORE the raise. See the class docstring; this line and the next must not swap.
        await self._audit.record_tool_call(
            self._turn_id,
            self._caller,
            call.tool_name,
            arguments,
            decision,
        )

        # `blocks_execution` is "not ALLOW", so an effect added to the enum later fails
        # closed here instead of falling through to execution unnoticed.
        #
        # TODO(F3): NEEDS_APPROVAL must SUSPEND rather than refuse - raise Pydantic AI's
        # `ApprovalRequired` from this same hook so the call is deferred with its
        # validated arguments and a human can resolve it. Until that lands it blocks,
        # which is the wrong answer in the safe direction rather than the right answer in
        # the dangerous one.
        if decision.blocks_execution:
            raise SkipToolExecution(refusal_result(decision))

        return args


class UnsupportedHistoryError(TypeError):
    """`history` arrived in a shape this adapter cannot turn into a conversation.

    `ConversationStore.load_history` returns `object` and its on-disk encoding is not
    settled: `PgConversationStore.append_outcome` is still pending, so no assistant turn
    has ever been written and the row format for one is undecided
    (docs/TASKS.md#t-f1-13).

    Guessing at that format here would put a second, drifting copy of the store's encoding
    inside the adapter - the exact duplication CLAUDE.md's conventions forbid. So the
    adapter accepts Pydantic AI's own message type, which is the vocabulary D7 says this
    port mirrors, and refuses anything else by name.
    """


class MidExchangeCompactionError(RuntimeError):
    """A compaction separated a tool call from its return, so the history was NOT sent.

    CLAUDE.md non-negotiable #5. The provider rejects such a conversation with a 400, and
    it does so on the NEXT request rather than at the cut, so the traceback names a turn
    that did nothing wrong. Refusing here puts the failure back where it was caused.

    IT IS LOUD ON PURPOSE. Quietly falling back to the uncompacted history would leave a
    broken ladder producing an agent that looks correct and never compacts - the exact
    shape of failure CLAUDE.md's silent-bug table exists to keep out of this file. A turn
    that dies with this name is one bug report; a ladder that silently stops working is a
    bill nobody can explain.

    Only a pairing THIS compaction broke is refused. A history that arrived unpaired is
    somebody else's defect - and F3's deferred calls arrive that way legitimately - so
    the guard compares the before and the after rather than judging the after alone.
    """


class UnsupportedToolsetError(TypeError):
    """`ToolProvider.toolset_for` returned something Pydantic AI cannot register.

    The port returns `object` so that use cases never import Pydantic AI. That makes the
    adapter the one place the real type is known, and therefore the one place a mismatch
    can be reported with a useful message instead of an AttributeError deep inside a run.
    """


# `(wire model id, proxy base url) -> Model`. Injected so a test can hand the runner a
# `FunctionModel` and so the composition root, not this adapter, decides which provider
# class serves a deployment. The default below serves BOTH LiteLLM modes, so an injected
# factory is a choice rather than the only way to reach a provider.
ModelFactory = Callable[[str, str | None], Model]


def litellm_model_factory(model_id: str, base_url: str | None) -> Model:
    """The default, and production's only path to a model: both LiteLLM modes.

    `base_url` is `ModelGateway.base_url()` verbatim - None on day 1 (library mode), the
    proxy URL on day 2 - and `adapters/driven/llm_litellm/models.py` owns what each of
    those means. It used to REFUSE the None case, which was safe and left production
    unable to call a model at all; the day-1 endpoint now exists, and a case that
    genuinely cannot be served still raises `ModelEndpointUnavailableError` from there.

    Imported lazily because that module pulls in the OpenAI SDK and litellm, and a process
    that supplies its own factory should not pay for imports it never uses.
    """
    from agent_core.adapters.driven.llm_litellm.models import model_for

    return model_for(model_id, base_url)


# How big the model's window is, when the composition root has not said. Deliberately NOT
# zero: `domain.compaction.target_tokens` reads zero as "unknown" and resolves towards
# compaction, which is the right fallback and a ruinous default - it would climb the whole
# ladder on every request. `adapters/driven/context/engine.py` carries the same constant
# for the same reason; both are a stand-in until a deployment states its own number.
DEFAULT_CONTEXT_WINDOW: Final[int] = 128_000

# How many tokens a history costs, for the TRIGGER only. Injected for the same reason
# `ModelFactory` is: which estimator a deployment trusts is not this adapter's decision.
TokenEstimator = Callable[[Sequence[ModelMessage]], int]


def ladder_token_estimator(messages: Sequence[ModelMessage]) -> int:
    """The default: the ladder's own estimator, so both sides of the trigger agree.

    `ContextEngine.should_compress` compares `estimated_tokens` against the window, and
    the ladder then measures the same history with `estimate_tokens` to decide when to
    stop climbing. Two different estimators on those two sides is a trigger that fires for
    a history the ladder considers small enough to leave alone - a paid pass that frees
    nothing, which is the most expensive shape of all.

    Imported lazily, exactly as `litellm_model_factory` is, so a process that injects its
    own estimator never imports the ladder at all.
    """
    from agent_core.adapters.driven.context.engine import estimate_tokens

    return estimate_tokens(messages)


def _utc_now() -> datetime:
    """The wall clock, in one place so a test can replace it.

    Non-determinism is allowed here because `run` is only ever reached from inside a DBOS
    step - `StartTurn` is invoked as one - never from a workflow body. CLAUDE.md
    non-negotiable #2.
    """
    return datetime.now(UTC)


def _as_message_history(history: object) -> list[ModelMessage] | None:
    """Translate the store's history into what Pydantic AI prepends, or refuse."""
    if history is None:
        return None
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise UnsupportedHistoryError(
            f"history must be a sequence of Pydantic AI ModelMessage values, got "
            f"{type(history).__name__}. The store's encoding is unsettled while "
            "append_outcome is pending - docs/TASKS.md#t-f1-13."
        )
    messages = list(history)
    if not messages:
        return None
    for message in messages:
        if not isinstance(message, (ModelRequest, ModelResponse)):
            raise UnsupportedHistoryError(
                f"history carries a {type(message).__name__}; this adapter accepts only "
                "Pydantic AI ModelMessage values. The store's encoding is unsettled while "
                "append_outcome is pending - docs/TASKS.md#t-f1-13."
            )
    return messages


def _unpaired(messages: Sequence[ModelMessage]) -> tuple[frozenset[str], frozenset[str]]:
    """`(calls with no return, returns with no call)` - the two ways a provider 400s.

    A tool return arrives inside a `ModelRequest` and carries the `tool_call_id` of the
    `ToolCallPart` it answers; a retry prompt for a failed call carries the same id, which
    is why the returning side is read by attribute rather than by one part type.
    """
    calls = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    returns: set[str] = set()
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            call_id = getattr(part, "tool_call_id", None)
            if isinstance(call_id, str):
                returns.add(call_id)
    return frozenset(calls - returns), frozenset(returns - calls)


def _refuse_if_compaction_broke_pairing(
    before: Sequence[ModelMessage], after: Sequence[ModelMessage]
) -> None:
    """CLAUDE.md #5, enforced at the one place a compacted history becomes a request.

    Pydantic AI repairs a dangling tool call by synthesizing a return BEFORE a capability
    sees the history, so `before` is normally already paired - the compaction is the only
    thing left that can break it, and it does so AFTER that repair. The comparison is
    still before-versus-after rather than a verdict on `after` alone, because the shapes
    that survive the repair (F3's deferred calls) are unpaired by design.
    """
    orphan_calls_before, orphan_returns_before = _unpaired(before)
    orphan_calls_after, orphan_returns_after = _unpaired(after)
    broken_calls = orphan_calls_after - orphan_calls_before
    broken_returns = orphan_returns_after - orphan_returns_before
    if not broken_calls and not broken_returns:
        return
    raise MidExchangeCompactionError(
        "compaction cut inside an exchange and the history was not sent: tool calls left "
        f"without a return {sorted(broken_calls)}, tool returns left without a call "
        f"{sorted(broken_returns)}. Cut only at complete-exchange boundaries - "
        "CLAUDE.md non-negotiable #5."
    )


def _domain_usage(usage: RunUsage) -> Usage:
    """Pydantic AI accounting -> the domain's `Usage`.

    `context_window_used` stays None: no provider LiteLLM fronts reports it, and
    domain/turn.py says None is the COMMON case that `ContextEngine` must handle with a
    local estimate. Inventing a fraction here would silently disable that fallback.
    """
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cache_read_tokens,
        cost_usd=usage.cost if usage.cost is not None else Decimal("0"),
        context_window_used=None,
    )


class PydanticAgentRunner:
    """`AgentRunner` over Pydantic AI. The F1 half is implemented; later phases are named.

    WHAT F1 DOES
        Builds one cached Pydantic AI `Agent` per profile, loads the policy snapshot once
        per turn, attaches `PolicyEnforcement` for that turn, runs the tool loop to
        completion and returns a FINISHED `TurnOutcome`.

    WHY THE AGENT IS CACHED AND WHAT THE KEY MUST CARRY
        Rebuilding per turn re-registers tools and re-resolves MCP servers on every
        request - slow, and it spawns stdio children far more often than intended.

        The key is `(profile id, model, content hash)`. `AgentProfile.content_hash` is a
        hash of the RESOLVED profile (domain/profile.py), so it already fingerprints the
        toolset, the persona and the limits: edit any of them and the next turn gets a new
        agent. A stale cached agent serving a changed profile is a permissions bug that
        survives a deploy, which is why the model and the id are named explicitly as well
        rather than trusting one derived string to mean everything.

    WHY THE CAPABILITY IS ATTACHED PER RUN, NOT PER AGENT
        `PolicyEnforcement` holds this turn's rule snapshot and this turn's id, so it
        cannot outlive the turn (see its own docstring). `Agent.run` accepts
        `capabilities=`, so the agent stays cached and the enforcement stays per turn.
        Attaching it to the cached agent instead would file every later turn's denials
        under the first turn's id.

    NO MODEL CALL INSIDE A TRANSACTION (CLAUDE.md #3)
        `load_rules` completes - and its connection is returned - before `agent.run`
        starts. Nothing here holds a database connection across the model call.

    AUDIT WRITES OUTSIDE THE DOMAIN TRANSACTION (CLAUDE.md #6)
        Every audit write goes through the injected `AuditSink`, which the composition
        root binds to its own pool. This adapter never opens a transaction of its own, so
        there is none for an audit row to be trapped inside.

    LEFT FOR LATER PHASES, DELIBERATELY
        - SUSPENSION / `HumanGateway` (F3): `output_type` is plain text, so no
          `DeferredToolRequests` can come back and `pending` is always empty. Until then a
          NEEDS_APPROVAL verdict blocks in `before_tool_execute` - the wrong answer in the
          safe direction. `resume` raises rather than pretending. docs/TASKS.md#t-f3-02.
        - MEDIA, PEERS and the rest below. COMPACTION (F5) is no longer among them: the
          `ContextEngine` attaches as a `ProcessHistory` capability in
          `_history_capability`, alongside the enforcement capability. The engine owns
          the strategy; this adapter only wires it in and does not second-guess the
          trigger. docs/TASKS.md#t-f5-07.
        - UNTRUSTED RESULTS, MCP AND SKILLS (F6): the `after_tool_execute` half of the
          pair sketched at the top of this module, plus MCP toolsets and the skill index in
          the system prompt. docs/TASKS.md#t-f6-04.
        - MEDIA (F7): `profile.media.delivery` decides BinaryContent versus ImageUrl /
          AudioUrl, and the default is BYTES because Pydantic AI hands a URL to the
          PROVIDER, which then downloads it - a disclosure for evidence containing
          personal data. docs/TASKS.md#t-f7-01.
        - PEERS (F9): a peer's answer is untrusted content and gets wrapped exactly like
          an MCP result. docs/TASKS.md#t-f9-04.

    STREAMING NOTE (F3, saves an afternoon)
        A DEFERRED tool call never reaches `event_stream_handler` - deferred means not
        executed, and only executing tools emit events. The pause context arrives via
        separate events (DeferredToolRequestsEvent). Without knowing this you will hunt a
        phantom bug in the UI.
    """

    def __init__(
        self,
        *,
        model: ModelGateway,
        policy: ToolPolicy,
        audit: AuditSink,
        tools: ToolProvider | None = None,
        context: ContextEngine | None = None,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        token_estimator: TokenEstimator = ladder_token_estimator,
        model_factory: ModelFactory = litellm_model_factory,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._model = model
        self._policy = policy
        self._audit = audit
        self._tools = tools
        self._context = context
        self._context_window = context_window
        self._token_estimator = token_estimator
        self._model_factory = model_factory
        self._clock = clock
        self._agents: dict[tuple[str, str, str], Agent[Any, str]] = {}

    def usage_limits_for(self, profile: AgentProfile) -> UsageLimits:
        """The profile's ceilings, as Pydantic AI enforces them.

        Public because a limit nobody can read back is a limit nobody tests. `BudgetState`
        (domain/budget.py) still owns the wrap-up notice; this is only the hard stop.

        `cost_limit` depends on the provider having pricing data. When it does not, Pydantic
        AI emits `CostNotFoundWarning` rather than silently enforcing nothing - which is why
        the limit is passed anyway: a ceiling that warns is findable, and one that is simply
        absent is not.
        """
        return UsageLimits(
            request_limit=profile.max_iterations,
            cost_limit=profile.max_cost_usd,
        )

    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:
        """One turn, start to finish. Ordinary outcomes never raise - ports/agent_runner.py.

        `turn_id` is the caller's, per call. This adapter never generates one (CLAUDE.md
        non-negotiable #2) and no longer needs a pre-bound instance to hold one: the port
        carries the seat, so a runner is a plain per-process collaborator again.
        """
        # Before the model call and before any audit write, so a malformed history is a
        # cheap, obvious failure rather than a paid one.
        message_history = _as_message_history(history)

        agent = await self._agent_for(profile)

        # ONCE per turn (D13). `load_rules` is the only policy call that touches I/O; its
        # connection is released here, before the model call below.
        rules = await self._policy.load_rules(request.caller)
        enforcement = PolicyEnforcement(
            policy=self._policy,
            rules=rules,
            audit=self._audit,
            turn_id=turn_id,
            caller=request.caller,
        )

        capabilities: list[AbstractCapability[Any]] = [enforcement]
        history_capability = self._history_capability(request.session, profile)
        if history_capability is not None:
            capabilities.append(history_capability)

        try:
            result = await agent.run(
                request.input.text,
                message_history=message_history,
                capabilities=capabilities,
                usage_limits=self.usage_limits_for(profile),
            )
        except UsageLimitExceeded as exhausted:
            # An exhausted budget is an ORDINARY outcome, not an exception - see
            # application/start_turn.py, WHAT THIS METHOD MUST NEVER DO. Letting it escape
            # turns a ceiling the operator chose into a 500 and loses the turn.
            return TurnOutcome(
                turn_id=turn_id,
                result=TurnResult(
                    text=f"The turn stopped because its budget was exhausted: {exhausted}",
                    finished_at=self._clock(),
                ),
            )

        # F1 has no deferred tools, so the run can only have FINISHED - see LEFT FOR LATER
        # PHASES above. The two-shaped translation arrives with F3, not a third shape here.
        return TurnOutcome(
            turn_id=turn_id,
            result=TurnResult(
                text=str(result.output),
                usage=_domain_usage(result.usage),
                finished_at=self._clock(),
            ),
        )

    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        """PSEUDO-CODE - F3, and it genuinely cannot be written before F3 lands.

        Resuming means rebuilding `DeferredToolResults` from `resolutions` and continuing
        the run. Nothing in F1 can produce a deferred request in the first place: the
        agent's output type is plain text, so no `DeferredToolRequests` can come back, and
        `before_tool_execute` blocks a NEEDS_APPROVAL verdict rather than suspending on it.
        A resume implemented now would have no suspension to resume and no `tool_call_id`
        to round-trip, so it could only be a lie that type-checks.

        THE SILENT BUG TO GUARD WHEN IT IS WRITTEN: every `tool_call_id` must be the id
        Pydantic AI issued. A regenerated or re-cased id is dropped WITHOUT an exception,
        and the agent asks the same question forever. Assert ids round-trip in an
        integration test.
        """
        raise NotImplementedError(
            "PydanticAgentRunner.resume: F3 - docs/TASKS.md#t-f3-02. Nothing suspends yet, "
            "so there is no deferred call to resume."
        )

    def _history_capability(
        self, session: SessionRef, profile: AgentProfile
    ) -> ProcessHistory[Any] | None:
        """The F5 seam: every model request's history goes through the `ContextEngine`.

        WHY A CAPABILITY AND NOT A CALL BEFORE `agent.run`
            One turn is MANY model requests - the tool loop makes one per iteration - and
            the history grows with each of them. Compacting once before the run would
            leave the growth inside the run uncompacted, which is precisely the traffic
            that overflows a window on a long tool loop. `ProcessHistory` fires
            `before_model_request`, so compaction reaches the request rather than going
            around it. docs/TASKS.md#t-f5-07.

        THIS ADAPTER DOES NOT SECOND-GUESS THE TRIGGER
            It asks `should_compress` and obeys the answer in both directions. The
            `ContextState` it builds is the honest one it can build: `window_used` stays
            None because no provider LiteLLM fronts reports it (see `_domain_usage`), so
            the port's own mandatory fallback decides - the one place that fallback lives.

        ONE EXHAUSTED LADDER ENDS THE ASKING FOR THIS RUN
            `CompactionResult.made_progress` False means the ladder freed nothing, and
            asking again on the next request of the same run is the infinite loop
            domain/compaction.py warns about - which ALSO rewrites the prompt prefix each
            pass and re-bills the whole prompt at full price. The run continues on the
            history it has; the turn either fits or fails honestly.
        """
        engine = self._context
        if engine is None:
            return None

        policy: CompactionPolicy = profile.compaction
        passes = 0
        exhausted = False

        async def process(
            ctx: RunContext[Any], messages: list[ModelMessage]
        ) -> list[ModelMessage]:
            nonlocal passes, exhausted
            if exhausted:
                return messages
            state = ContextState(
                session=session,
                window_used=None,
                estimated_tokens=self._token_estimator(messages),
                context_window=self._context_window,
                message_count=len(messages),
                passes_so_far=passes,
            )
            if not engine.should_compress(state, policy):
                return messages

            result = await engine.compress(session, list(messages), policy)
            if not result.made_progress:
                exhausted = True
                return messages

            compacted = _as_message_history(result.compacted_history) or []
            # BEFORE the history can become a request. See the error's own docstring.
            _refuse_if_compaction_broke_pairing(messages, compacted)
            passes += 1
            return compacted

        return ProcessHistory[Any](process)

    async def _agent_for(self, profile: AgentProfile) -> Agent[Any, str]:
        """The cached agent for this profile, built on first use. See the class docstring."""
        key = (
            profile.id,
            profile.model,
            profile.content_hash or profile_content_hash(profile),
        )
        cached = self._agents.get(key)
        if cached is not None:
            return cached

        model = self._model_factory(
            self._model.model_id_for(profile.model), self._model.base_url()
        )
        agent: Agent[Any, str] = Agent(
            model,
            # The persona only. The assembled system prompt - persona plus the skill INDEX -
            # is F6, step 4 of application/start_turn.py. `instructions` rather than
            # `system_prompt` so it is not replayed as part of the stored history.
            instructions=profile.persona,
            toolsets=await self._toolsets_for(profile),
        )
        self._agents[key] = agent
        return agent

    async def _toolsets_for(self, profile: AgentProfile) -> list[AbstractToolset[Any]]:
        """Whatever `ToolProvider` says this profile has, validated at the boundary.

        None means no tool provider is wired yet - the F1 local adapter is
        docs/TASKS.md#t-f1-07 and the first real one is F4. An agent with no tools is a
        useful, honest thing; a placeholder that pretends to have them is not.

        MCP toolsets compose INSIDE `ToolProvider` (ports/tool_provider.py), so nothing
        here changes when F6 lands - and MCP tools reach `before_tool_execute` by the same
        path as local ones, which is CLAUDE.md non-negotiable #4.
        """
        if self._tools is None:
            return []
        toolset = await self._tools.toolset_for(profile)
        if not isinstance(toolset, AbstractToolset):
            raise UnsupportedToolsetError(
                f"ToolProvider.toolset_for returned {type(toolset).__name__}; this adapter "
                "registers Pydantic AI AbstractToolset values."
            )
        return [toolset]
