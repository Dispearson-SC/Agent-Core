"""Use case: StartTurn - run one turn from a fresh request.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-11
Status:  IMPLEMENTED (t-f1-11) - STEP 4 STILL PENDING, SEE THE SEAT NOTE BELOW

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
"""

from __future__ import annotations

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest
from agent_core.ports.agent_runner import AgentRunner
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.context_engine import ContextEngine
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.skill_registry import SkillRegistry
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.tool_provider import ToolProvider


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

        # STEP 2 - PERSIST THE REQUEST FIRST, and AWAIT it. "Persisted first" is only true
        # if the write has completed before the model call starts; a fired-and-forgotten
        # coroutine reads identically and guarantees nothing.
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

        # STEP 5 - LOAD HISTORY. Already compacted once F5 lands; before then it returns
        # everything, which is correct and simply more expensive.
        history = await self._store.load_history(request.session)

        # STEP 6 - RUN. The longest await in the turn. Per-call policy enforcement and the
        # audit write happen inside the adapter's `before_tool_execute` hook, i.e. before
        # any side effect - this class never sees individual tool calls.
        outcome = await self._runner.run(
            turn_id, request, self._runner_profile(profile, advertised), history
        )

        # STEP 7 - ACCOUNT AND PERSIST. A SUSPENDED outcome is persisted exactly like a
        # finished one: the process may die while a human takes three days to answer.
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
