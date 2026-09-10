"""Use case: StartTurn - run one turn from a fresh request.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-11
Status:  PSEUDO-CODE ONLY

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
    nothing about DBOS - that is what makes it replayable.
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

    def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        """PSEUDO-CODE - implement in F1, extended in F5/F6.

        `turn_id` is PASSED IN, not generated here. The caller generated it inside a DBOS
        step; generating it here would produce a new id on replay and fork the
        conversation. CLAUDE.md non-negotiable #2.

        STEP 1 - RESOLVE THE PROFILE
            profile = self._profiles[request.profile_id]
            Unknown id -> raise. Never fall back to a default profile: a typo would
            silently run the agent with the wrong permissions.

        STEP 2 - PERSIST THE REQUEST FIRST
            self._store.append_request(turn_id, request)
            BEFORE the model is invoked and before any tool can run. If the process dies
            mid-turn, the session is still reconstructable and the operator can see what
            was in flight.

        STEP 3 - BUILD THE TOOLSET, THEN FILTER IT
            names = self._tools.tool_names_for(profile)
            allowed = self._policy.filter_toolset(request.caller, names)
            The model must never SEE a tool this caller may not use. Advertising a
            forbidden tool invites repeated attempts that burn the budget for nothing.
            (F6: `tool_names_for` reads the MCP schema cache, so this does not spawn
            stdio children just to answer a permissions question.)

        STEP 4 - ASSEMBLE THE SYSTEM PROMPT
            persona = profile.persona
            index   = self._skills.index(profile)      # F6; metadata ONLY
            system  = persona + rendered index
            Never inline a skill BODY here. One line per skill; the model calls
            `skill_view` when it wants more. That is the whole optimisation.

        STEP 5 - LOAD HISTORY (already compacted)
            history = self._store.load_history(request.session)
            Returns [summary checkpoint] + [recent messages] once F5 lands. Before F5 it
            returns everything, which is correct and simply more expensive.

        STEP 6 - RUN
            outcome = self._runner.run(request, profile, history)
            Policy enforcement per call and the audit write happen INSIDE the adapter's
            `before_tool_execute` hook - i.e. before any side effect. Not here: this class
            never sees individual tool calls.

        STEP 7 - ACCOUNT AND PERSIST
            if outcome.result: self._context.update_from_response(session, usage)
            self._store.append_outcome(turn_id, outcome)
            self._audit.record_turn_end(turn_id, usage, cost)

            Persist a SUSPENDED outcome too. The whole point is that the process may die
            while a human takes three days to answer.

        STEP 8 - RETURN
            Return the outcome unchanged. This class does NOT decide what to do about
            pending requests: that is the workflow's job. Keeping the decision out of here
            is what lets the same use case serve the HTTP path and the cron path.

        WHAT THIS METHOD MUST NEVER DO
            - Loop waiting for a human. The durable wait is DBOS.recv() in the workflow.
            - Raise on a policy denial or an exhausted budget. Both are ordinary outcomes.
            - Open a database transaction around the model call. CLAUDE.md #3.
        """
        raise NotImplementedError("F1 - docs/TASKS.md#t-f1-11")
