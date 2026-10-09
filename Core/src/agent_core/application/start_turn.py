"""Use case: StartTurn - run one turn from a fresh request.

Phase:   F1 / F9 (communicable suspension)
Tasks:   docs/TASKS.md#t-f1-11, docs/TASKS.md#t-f9-06, docs/TASKS.md#t-f11-29
Status:  IMPLEMENTED (t-f1-11, t-f9-06) - STEP 4 STILL PENDING, SEE THE SEAT NOTE BELOW

LAYER RULE
    Imports domain/ and ports/ ONLY. No Pydantic AI, no DBOS, no FastAPI, no driver.
    `pyproject.toml` enforces this mechanically via ruff banned-api; if you find yourself
    fighting that rule, you are about to break the architecture, not the linter.

TESTABILITY IS THE POINT
    Every dependency arrives through the constructor as a Protocol. This class must be
    fully testable with fakes: no database, no network, no model. If a test for this file
    needs Postgres, a port is leaking.

WHO CALLS IT
    `adapters/driving/workflow/turn_workflow.py` inside a @DBOS.step(). This class knows
    nothing about DBOS - that is what makes it replayable. DBOS supports coroutine steps
    (verified, D13), so an async use case costs the workflow nothing.

WHY `execute` IS ASYNC (D13)
    The application layer is async. This method awaits the store, the model, the audit sink
    and the tool provider; every one of those is I/O the process must not block on while
    other turns are in flight.

    The one thing it does NOT await is the policy verdict. `ToolPolicy` is split: the single
    async `load_rules` call is hoisted to step 3, and `filter_toolset` / `decide` are pure
    sync functions over the snapshot it returns. See `ports/tool_policy.py`.

THE SEAT NOTE - WHAT THIS FILE COMPUTES AND CANNOT YET HAND OVER
    `AgentRunner.run` takes `(turn_id, request, profile, history)`, and
    `tests/unit/test_ports_agent_runner.py` type-checks that arity in both directions.
    The `turn_id` seat is the one that was MISSING and has been added: this use case holds
    the id, the runner needs it for the outcome and for every audit row, and it used to be
    smuggled in through a `for_turn` pre-binding on the adapter. It is a parameter now.

    Two products of this use case still have nowhere to sit on that call:

      - the advertised toolset from step 3, and
      - the assembled system prompt from step 4 (persona plus the skill index, F6).

    Neither fits in `AgentProfile` either: `toolsets` names BUNDLES, not tools, so narrowing
    it by tool name would silently strip an agent of every tool whose bundle name differs
    from it. Rather than smuggle them through `history: object` or widen a frozen port from
    the wrong side, both gaps are named in ONE place - `_runner_profile` below - so t-f1-12
    resolves them deliberately instead of inheriting a workaround. The composition root
    already builds the runner with the same `ToolPolicy` (`composition.py`), so the
    enforcement point in `before_tool_execute` is not left without a rule source in the
    meantime.

COMMUNICABLE SUSPENSION - t-f9-06, D17, CLAUDE.md non-negotiable #11
    A peer ask can take hours: the peer runs a whole turn of its own and may suspend on
    its own human before answering. Meanwhile a customer is waiting. D17 calls the
    requirement COMMUNICABILITY - "before suspending, the agent tells its own user it is
    checking" - and docs/ARCHITECTURE.md section 15 records it as the one risk in F9 that
    is a product problem rather than a technical one. A turn that suspends silently is
    indistinguishable, from the far side, from a process that crashed.

    Non-negotiable #11 pulls the other way and both halves have to hold: the user sees
    THAT something is pending and never WHICH tool - nor, for a peer ask, which peer.
    `PeerPolicy.visibility` defaults to NONE so a customer-service agent cannot advertise
    whose personal assistant it just paged; a user-facing notice naming that peer hands
    the same fact back through the front door.

    So the ONE thing a suspended turn carries that a person may read - `PendingRequest
    .reason` - is replaced, for a `PendingKind.DELEGATION` only, by
    `PEER_SUSPENSION_NOTICE`. Which request that is is decided by the KIND, never by the
    tool name (t-f9-08); `_notice_for` below records why the name was never a discriminator.
    This is the
    copy `EntryKind.PENDING_PLACEHOLDER` (domain/transcript.py) exists to be: the
    user-visible stand-in for a `PENDING_REQUEST` they may not see. No second mechanism
    is introduced here and none is needed - the placeholder already IS the mechanism, and
    this is the sentence it says.

    THREE THINGS THIS DELIBERATELY DOES NOT DO.
      - It does not touch `tool_call_id`, `tool_name`, `arguments` or `kind`. Those are
        the record, not the message: `tool_call_id` must round-trip verbatim or the
        resumed result is dropped silently, and `tool_name`/`arguments` are what turns
        the deferred call into a real `AgentMailbox.ask()`. Redacting the RECORD to
        protect the MESSAGE would trade one silent bug for a worse one - which is exactly
        why D18 stores everything and filters on read.
      - It does not touch an APPROVAL. There the person reading the ask IS the one who
        must decide, and the reason is the whole of what they decide on. A blanket
        rewrite turns a four-eyes rule (D25) into a rubber stamp.
      - It does not name a channel, and it must not grow one. Pushing the notice to
        WhatsApp or Telegram is delivery, which D23 settled as a channel registry in the
        driving adapter - `application/` never learns a channel exists. If telling the
        user ever needs more than the durable record this method writes, that is D23
        being reopened deliberately, not an import added here.

    WHY THE REWRITE HAPPENS BEFORE STEP 7 AND NOT AFTER STEP 8. `execute` returns into
    `_step_start`, and the workflow only then reaches `DBOS.recv_async` - the moment the
    turn actually suspends. A notice that existed only on the returned value would be
    lost by a crash in that window, and the recovered turn would read the durable record
    instead. "Before the turn suspends" is only true if the notice is inside what
    `append_outcome` was handed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    TurnId,
    TurnOutcome,
    TurnRequest,
)
from agent_core.ports.agent_runner import AgentRunner
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.context_engine import ContextEngine
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.skill_registry import SkillRegistry
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.tool_provider import ToolProvider

# The shared peer-ask mechanism's tool name, as `adapters/driven/tools/peers.py` declares
# it. Spelled, not imported: `application/` may not import an adapter.
#
# NOTHING IN THIS MODULE READS IT ANY MORE (t-f9-08). The alternative it used to weigh - "a
# domain change plus a migration of every stored outcome" - turned out to cost nothing,
# because no module reads a stored `pending` blob back into a `PendingRequest`; see the
# STORED-OUTCOME note on `PendingKind`. `PendingKind.DELEGATION` now carries the fact, and
# `_notice_for` below decides from the kind.
#
# IT IS STILL EXPORTED FOR ONE CALLER, WHICH MUST STOP IMPORTING IT
#     `adapters/driving/workflow/turn_workflow.py::_answerable_by_a_human` imports this
#     constant and matches on it. That function becomes
#     `return pending.kind.answerable_by_a_human`, and this constant dies with the import.
#     Removing it here first would only break that module, and a name kept for one migration
#     is cheaper than two modules disagreeing about what a peer ask is.
ASK_PEER_TOOL: Final[str] = "ask_peer"

# What the user is told while a peer is being consulted. Deliberately a sentence and not a
# redaction marker: "[redacted]" tells a customer the system is hiding something from
# them, which reads worse than the silence it replaced. This is the copy behind
# `EntryKind.PENDING_PLACEHOLDER` - THAT something is happening, and nothing about what.
PEER_SUSPENSION_NOTICE: Final[str] = (
    "I am checking on that now and will come back to you as soon as I have an answer."
)


class UnknownProfileError(KeyError):
    """`request.profile_id` names no loaded profile.

    A subclass of `KeyError` so the obvious `except KeyError` at a call site still catches
    it, and a named type so a driving adapter can answer 404 rather than 500 without
    string-matching a message.

    It exists at all because the alternative - falling back to a default profile - turns a
    typo into an agent running with someone else's permissions, and nothing downstream can
    tell the difference.
    """


class StartTurn:
    """Starts a turn and returns its two-shaped outcome."""

    def __init__(
        self,
        *,
        runner: AgentRunner,
        tools: ToolProvider,
        policy: ToolPolicy,
        store: ConversationStore,
        audit: AuditSink,
        context: ContextEngine,
        skills: SkillRegistry,
        profiles: dict[str, AgentProfile],
    ) -> None:
        self._runner = runner
        self._tools = tools
        self._policy = policy
        self._store = store
        self._audit = audit
        self._context = context
        self._skills = skills
        self._profiles = profiles

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        """Run one turn. Steps are numbered to match the task notes in docs/TASKS.md.

        `turn_id` is PASSED IN, not generated here. The caller generated it inside a DBOS
        step; generating it here would produce a new id on replay and fork the
        conversation. CLAUDE.md non-negotiable #2.

        WHAT THIS METHOD MUST NEVER DO
            - Loop waiting for a human. The durable wait is DBOS.recv() in the workflow.
            - Raise on a policy denial or an exhausted budget. Both are ordinary outcomes.
            - Open a database transaction around the model call. CLAUDE.md #3.
        """
        # STEP 1 - RESOLVE THE PROFILE. Before the write, so an unknown id leaves no
        # half-started turn behind for an operator to puzzle over.
        profile = self._resolve_profile(request.profile_id)

        # STEP 2 - READ THE CONVERSATION SO FAR, THEN PERSIST THIS REQUEST INTO IT.
        #
        # The order of these two lines is load-then-append, and it is not interchangeable
        # (t-f1-24). `load_history` answers "what was said BEFORE this turn"; appending
        # first makes it answer "...including the sentence I am about to send", and the
        # runner then hands the model that sentence twice - once as prior context and once
        # as the new prompt. Pydantic AI merges the two into one request rather than
        # deduplicating them, so the provider is billed twice for it and shown a
        # conversation that never happened. This was invisible for as long as the history
        # could not be read back at all.
        #
        # THE ORDERING RULE IS UNCHANGED. `ports/conversation_store.py` requires the turn
        # to be persisted BEFORE any side effect runs, and a read is not a side effect: the
        # append below still completes before step 6 invokes the model, which is the whole
        # of what makes a crash mid-turn reconstructable.
        #
        # AWAITED, not fired and forgotten. "Persisted first" is only true if the write has
        # completed before the model call starts; a bare coroutine reads identically here
        # and guarantees nothing.
        history = await self._store.load_history(request.session)
        await self._store.append_request(turn_id, request)

        # STEP 3 - LOAD THE RULES ONCE, LIST THE TOOLS, THEN NARROW.
        # `load_rules` is the only policy call that touches I/O, and hoisting it out of the
        # per-call path is the whole point of the D13 split. The snapshot it returns already
        # carries the caller it was narrowed for (`RuleSet.for_caller`), which is why
        # `filter_toolset` takes no caller: there is no second identity that could disagree
        # with the one the snapshot holds.
        rules = await self._policy.load_rules(request.caller)
        tool_names = await self._tools.tool_names_for(profile)
        # The model must never SEE a tool this caller may not use: advertising a forbidden
        # tool invites repeated attempts that burn the budget for nothing. NEEDS_APPROVAL
        # tools stay advertised - the model may ask, a human just has to agree.
        advertised = self._policy.filter_toolset(rules, tool_names)

        # STEP 4 - ASSEMBLE THE SYSTEM PROMPT (F6): persona plus the skill INDEX, one line
        # per skill, never a skill body. Deferred with step 3's toolset for the same reason;
        # see THE SEAT NOTE in the module docstring.

        # STEP 5 - HISTORY was loaded in step 2, above, and for the reason given there.
        # Already compacted once F5 lands; before then it returns everything, which is
        # correct and simply more expensive.

        # STEP 6 - RUN. The longest await in the turn. Per-call policy enforcement and the
        # audit write happen inside the adapter's `before_tool_execute` hook, i.e. before
        # any side effect - this class never sees individual tool calls.
        outcome = await self._runner.run(
            turn_id, request, self._runner_profile(profile, advertised), history
        )

        # STEP 6b - MAKE A SUSPENSION COMMUNICABLE (t-f9-06, D17). Before the outcome is
        # persisted, and therefore before the workflow can begin its durable wait, a peer
        # ask's user-facing text becomes a notice that says something is happening and
        # names neither the peer nor the tool. See COMMUNICABLE SUSPENSION above.
        outcome = _communicable(outcome)

        # STEP 7 - ACCOUNT AND PERSIST. A SUSPENDED outcome is persisted exactly like a
        # finished one: the process may die while a human takes three days to answer.
        #
        # AND THIS IS WHERE THE AGENT'S HALF OF THE CONVERSATION IS WRITTEN (t-f11-29).
        # `outcome.messages` carries what the runner added - the model's replies, its tool
        # calls and the returns answering them - and `append_outcome` persists them with
        # the terminal state in one transaction. Step 2 wrote the prompt and nothing wrote
        # the answer, so a stored conversation used to be the user's sentences alone.
        #
        # THIS USE CASE NEVER LOOKS INSIDE ONE. They are opaque by construction (see
        # `TurnOutcome.messages`): the runner adapter produced them and the store adapter
        # encodes them, and an `application/` module that read a part would be naming
        # Pydantic AI, which the layer rule at the top of this file forbids.
        if outcome.result is not None:
            self._context.update_from_response(request.session, outcome.result.usage)

        await self._store.append_outcome(turn_id, outcome)

        if outcome.result is not None:
            # Only a FINISHED turn has ended, and only a finished turn carries the usage
            # this record is made of. Writing a turn_end for a suspended turn would book a
            # second one when the turn later resumes and finishes, and an audit trail that
            # ends a turn twice cannot be reconciled with the conversation it describes.
            # The suspension itself is already durable: `append_outcome` above wrote it.
            await self._audit.record_turn_end(
                turn_id, outcome.result.usage, outcome.result.usage.cost_usd
            )

        # STEP 8 - RETURN THE OUTCOME UNCHANGED. What to do about pending requests is the
        # workflow's decision, which is what lets one use case serve the HTTP path and the
        # cron path.
        return outcome

    def _resolve_profile(self, profile_id: str) -> AgentProfile:
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise UnknownProfileError(
                f"No profile is loaded under id {profile_id!r}. Refusing to fall back to a "
                "default: a typo must not run the agent with another profile's permissions."
            ) from None

    def _runner_profile(
        self, profile: AgentProfile, advertised: tuple[str, ...]
    ) -> AgentProfile:
        """The profile the runner may build its agent from.

        THE ONE PLACE THE REMAINING SEAT LIVES. `advertised` is the policy-narrowed tool
        surface from step 3 and belongs on this call, but `AgentRunner.run` takes
        `(turn_id, request, profile, history)` and `AgentProfile` has no field that holds
        tool names - `toolsets` names bundles. So the profile passes through untouched
        today.

        Deliberately a named method rather than an inline `self._runner.run(turn_id,
        request, profile, ...)`: the gap is then one function a reviewer can find, and
        t-f1-12 has exactly one place to change when the runner grows a seat for the
        narrowed surface.
        """
        return profile


def _communicable(outcome: TurnOutcome) -> TurnOutcome:
    """The same outcome, with every peer ask's user-facing text replaced by the notice.

    Module-level and pure - no `self`, no clock, no id. It runs inside a `@DBOS.step()`
    (`_step_start`), and a step that produced a different value on replay would hand the
    recovered turn a different conversation than the one that was persisted; CLAUDE.md
    non-negotiable #2. Pure also means the property is testable on its own, without
    assembling a use case around it.

    A FINISHED outcome and a suspension with nothing to rewrite are returned unchanged -
    the identical object, not a rebuilt equal one. Rebuilding would be harmless today and
    would quietly become the thing that drops a field the day `TurnOutcome` grows one.

    THAT DAY WAS t-f11-29. `TurnOutcome` grew `messages` - the agent's half of the
    conversation - and the rebuild below now carries it explicitly. Dropping it here would
    have lost the model's replies for exactly the turns that suspended, i.e. the ones whose
    history matters most, and no test between here and the provider's eventual 400 would
    have said so.
    """
    if not outcome.pending:
        return outcome
    rewritten = tuple(_notice_for(pending) for pending in outcome.pending)
    if rewritten == outcome.pending:
        return outcome
    # `result` is None by I2 for any outcome carrying `pending`, so this rebuild cannot
    # drop an answer: `TurnOutcome.__post_init__` refuses the both-shapes case outright.
    return TurnOutcome(
        turn_id=outcome.turn_id, pending=rewritten, messages=outcome.messages
    )


def _notice_for(pending: PendingRequest) -> PendingRequest:
    """One pending request, with `reason` replaced when - and only when - an AGENT answers it.

    DECIDED BY `kind`, NEVER BY `tool_name` (t-f9-08). It used to match on the name,
    because both kinds a peer ask could wear were defined in terms of a human and
    `PendingKind.EVIDENCE` - a request for the user's photo - was the closest fit. The name
    is not the discriminator: a second agent-executed mechanism would keep its raw `reason`
    and hand the user the peer's id and their own question verbatim, and an evidence
    request from a tool whose name happened to match would lose the one sentence that tells
    the person WHAT to send. `PendingKind.DELEGATION` now carries the fact itself.

    THE MATCH IS EXHAUSTIVE, AND NOT DERIVED FROM `answerable_by_a_human`
        `kind.answerable_by_a_human` answers who may be ASKED; this function chooses COPY.
        Deriving one from the other would give a future kind the peer's sentence by
        default, and a default wearing a decision's clothes is the defect `t-f10-01`
        already paid for. Written out, a new member is a type error here - mypy reports the
        missing return - so the sentence a person reads is always chosen for them.
    """
    match pending.kind:
        case PendingKind.APPROVAL | PendingKind.EVIDENCE:
            return pending
        case PendingKind.DELEGATION:
            return replace(pending, reason=PEER_SUSPENSION_NOTICE)
