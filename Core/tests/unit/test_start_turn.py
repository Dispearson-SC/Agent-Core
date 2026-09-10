"""StartTurn - the four properties the use case exists to guarantee.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-11

Fakes only: no database, no network, no model. If this module ever needs one, a port is
leaking (tests/conftest.py says the same thing).

The four properties, and why each is here rather than left to an integration test:

1. ORDER, not merely occurrence. `append_request` must have COMPLETED before the model
   runs, so a process killed mid-turn still leaves the operator something to read. A test
   that only asserts "both were called" passes on a fired-and-forgotten write.
2. A tool this caller may not use must never be advertised. The advertised surface is
   whatever `ToolPolicy.filter_toolset` returned, computed from the ONE `RuleSet` snapshot
   `load_rules` produced, and computed BEFORE the runner is invoked.
3. A SUSPENDED outcome is persisted exactly like a finished one. The process may die while
   a human takes three days to answer.
4. An unknown `profile_id` raises, and raises before anything is written. Falling back to a
   default profile would run the agent with the wrong permissions over a typo.

`FakeContextEngine` and `FakeSkillRegistry` are declared here rather than in
tests/fakes/ports.py because those ports land in F5 and F6; the shared fakes file says so
in its own TODO list, and this task does not own that file.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from agent_core.application.start_turn import StartTurn
from agent_core.domain.compaction import CompactionPolicy, CompactionResult, ContextState
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    Usage,
    UserInput,
)
from agent_core.ports.skill_registry import SkillMeta
from tests.fakes.ports import (
    FakeAgentRunner,
    FakeAuditSink,
    FakeConversationStore,
    FakeToolPolicy,
    FakeToolProvider,
)

PROFILE = AgentProfile(id="delivery_optimizer", persona="You optimise deliveries.", model="m")

# The provider offers four names. Only the first two survive the rules below: `run_shell`
# is denied outright, and `mcp_maps_lookup` matches nothing and falls to the DENY default.
OFFERED_TOOLS = ("route_plan", "pricing_apply", "run_shell", "mcp_maps_lookup")
EXPECTED_ADVERTISED = ("route_plan", "pricing_apply")

RULES = (
    PolicyRule(
        rule_id="r-route",
        tool_pattern="route_*",
        effect=Effect.ALLOW,
        reason="Routing is read-mostly.",
    ),
    PolicyRule(
        rule_id="r-pricing",
        tool_pattern="pricing_apply",
        effect=Effect.NEEDS_APPROVAL,
        reason="A price change needs a human.",
    ),
    PolicyRule(
        rule_id="r-shell",
        tool_pattern="run_shell",
        effect=Effect.DENY,
        reason="No shell from a chat channel.",
    ),
)


class RecordingToolPolicy(FakeToolPolicy):
    """`FakeToolPolicy` plus a journal, so ORDER against the runner can be asserted."""

    def __init__(self, rules: RuleSet, journal: list[str]) -> None:
        super().__init__(rules)
        self._journal = journal
        self.loaded: list[RuleSet] = []
        self.filter_calls: list[tuple[RuleSet, tuple[str, ...]]] = []
        self.filter_results: list[tuple[str, ...]] = []

    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        self._journal.append("load_rules")
        snapshot = await super().load_rules(caller)
        self.loaded.append(snapshot)
        return snapshot

    def filter_toolset(self, rules: RuleSet, tool_names: tuple[str, ...]) -> tuple[str, ...]:
        self._journal.append("filter_toolset")
        self.filter_calls.append((rules, tool_names))
        kept = super().filter_toolset(rules, tool_names)
        self.filter_results.append(kept)
        return kept


class RecordingConversationStore(FakeConversationStore):
    """`FakeConversationStore` plus a journal. The write is journalled AFTER it completed,
    which is what makes "persisted before the model ran" a real assertion rather than
    "scheduled before the model ran"."""

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def append_request(self, turn_id: TurnId, request: TurnRequest) -> None:
        await super().append_request(turn_id, request)
        self._journal.append("append_request")

    async def append_outcome(self, turn_id: TurnId, outcome: TurnOutcome) -> None:
        await super().append_outcome(turn_id, outcome)
        self._journal.append("append_outcome")


class RecordingAgentRunner(FakeAgentRunner):
    """`FakeAgentRunner` plus a journal entry the moment the model would be called."""

    def __init__(self, outcome: TurnOutcome, journal: list[str]) -> None:
        super().__init__(outcome)
        self._journal = journal

    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:
        self._journal.append("run")
        return await super().run(turn_id, request, profile, history)


class FakeContextEngine:
    """F5 owns the real engine; this records the accounting calls F1 must already make."""

    def __init__(self) -> None:
        self.updates: list[tuple[SessionRef, Usage]] = []

    def on_session_start(self, session: SessionRef) -> None:
        return None

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        self.updates.append((session, usage))

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        return False

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        raise NotImplementedError("F5 - docs/TASKS.md#t-f5-02")

    def on_session_end(self, session: SessionRef) -> None:
        return None


class FakeSkillRegistry:
    """F6 owns the real registry. Metadata only - never a skill body."""

    def __init__(self) -> None:
        self.index_calls: list[AgentProfile] = []

    async def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        self.index_calls.append(profile)
        return ()

    async def read(self, name: str) -> str:
        raise NotImplementedError("F6 - docs/TASKS.md#t-f6-02")


def _request(session: SessionRef, caller: CallerIdentity, profile_id: str) -> TurnRequest:
    return TurnRequest(
        session=session,
        caller=caller,
        profile_id=profile_id,
        input=UserInput(text="Re-route the 14:00 batch."),
    )


FINISHED = TurnOutcome(
    turn_id=TurnId("turn-1"),
    result=TurnResult(
        text="Rerouted.",
        usage=Usage(input_tokens=120, output_tokens=30, cost_usd=Decimal("0.004")),
    ),
)

SUSPENDED = TurnOutcome(
    turn_id=TurnId("turn-1"),
    pending=(
        PendingRequest(
            kind=PendingKind.APPROVAL,
            tool_call_id=ToolCallId("call-7"),
            tool_name="pricing_apply",
            arguments={"pct_change": 22},
            reason="A price change above 15% needs a human.",
        ),
    ),
)


class _Wiring:
    """One assembled use case plus every fake it was built from."""

    def __init__(self, caller: CallerIdentity, outcome: TurnOutcome) -> None:
        self.journal: list[str] = []
        self.policy = RecordingToolPolicy(RuleSet.for_caller(caller, RULES), self.journal)
        self.tools = FakeToolProvider(toolset=object(), tool_names=OFFERED_TOOLS)
        self.store = RecordingConversationStore(self.journal)
        self.runner = RecordingAgentRunner(outcome, self.journal)
        self.audit = FakeAuditSink()
        self.context = FakeContextEngine()
        self.skills = FakeSkillRegistry()
        self.use_case = StartTurn(
            runner=self.runner,
            tools=self.tools,
            policy=self.policy,
            store=self.store,
            audit=self.audit,
            context=self.context,
            skills=self.skills,
            profiles={PROFILE.id: PROFILE},
        )


@pytest.mark.phase("F1")
def test_the_request_is_persisted_before_the_model_is_invoked(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """A process killed mid-turn must still leave the operator the request to read."""
    wiring = _Wiring(caller, FINISHED)

    asyncio.run(wiring.use_case.execute(TurnId("turn-1"), _request(session, caller, PROFILE.id)))

    assert "append_request" in wiring.journal, "the request was never persisted"
    assert "run" in wiring.journal, "the runner was never invoked"
    assert wiring.journal.index("append_request") < wiring.journal.index("run"), (
        "append_request must have COMPLETED before the model call starts; "
        f"journal was {wiring.journal}"
    )


@pytest.mark.phase("F1")
@pytest.mark.silent
def test_a_denied_tool_is_never_advertised_to_the_runner(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """SILENT-BUG AREA: nothing fails when the filter is skipped, the agent simply gains a
    tool it may not use. Advertising a forbidden tool also invites the model to keep
    retrying it, which burns the budget for nothing."""
    wiring = _Wiring(caller, FINISHED)

    asyncio.run(wiring.use_case.execute(TurnId("turn-1"), _request(session, caller, PROFILE.id)))

    assert wiring.policy.load_rules_calls == [caller], (
        "the snapshot must be loaded once, for the caller who actually asked"
    )
    assert wiring.tools.tool_names_for_calls == [PROFILE], (
        "the advertised names must come from the provider, for the resolved profile"
    )
    assert len(wiring.policy.filter_calls) == 1, (
        "filter_toolset runs exactly once per turn, over the whole offered set"
    )

    filtered_rules, filtered_names = wiring.policy.filter_calls[0]
    assert filtered_rules is wiring.policy.loaded[0], (
        "the filter must reduce the SAME snapshot load_rules returned; a second snapshot "
        "can disagree with the first mid-turn"
    )
    assert filtered_names == OFFERED_TOOLS

    advertised = wiring.policy.filter_results[0]
    assert advertised == EXPECTED_ADVERTISED, (
        "DENY drops the tool; NEEDS_APPROVAL keeps it advertised; an unmatched name falls "
        "to the DENY default"
    )
    assert "run_shell" not in advertised
    assert "mcp_maps_lookup" not in advertised

    assert wiring.journal.index("filter_toolset") < wiring.journal.index("run"), (
        "the toolset must be narrowed BEFORE the model runs, not alongside it; "
        f"journal was {wiring.journal}"
    )


@pytest.mark.phase("F1")
def test_a_suspended_outcome_is_persisted_too(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """The whole point is that the process may die while a human takes three days."""
    wiring = _Wiring(caller, SUSPENDED)
    turn_id = TurnId("turn-1")

    outcome = asyncio.run(wiring.use_case.execute(turn_id, _request(session, caller, PROFILE.id)))

    assert outcome is SUSPENDED, "the use case returns the outcome unchanged"
    assert outcome.is_suspended
    assert wiring.store.outcomes == [(turn_id, SUSPENDED)], (
        "a suspended outcome must be persisted exactly like a finished one"
    )
    assert [call.kind for call in wiring.audit.calls] == [], (
        "a suspended turn has not ended: recording turn_end here would double-count when "
        "the turn later resumes and finishes"
    )
    assert wiring.context.updates == [], "there is no usage to account for until the turn ends"


@pytest.mark.phase("F1")
def test_a_finished_outcome_is_accounted_for_and_audited(
    session: SessionRef, caller: CallerIdentity
) -> None:
    wiring = _Wiring(caller, FINISHED)
    turn_id = TurnId("turn-1")

    asyncio.run(wiring.use_case.execute(turn_id, _request(session, caller, PROFILE.id)))

    assert FINISHED.result is not None
    assert wiring.context.updates == [(session, FINISHED.result.usage)]
    assert [call.kind for call in wiring.audit.calls] == ["turn_end"]
    assert wiring.audit.calls[0].payload == (
        turn_id,
        FINISHED.result.usage,
        FINISHED.result.usage.cost_usd,
    )
    assert wiring.journal.index("append_outcome") < len(wiring.journal)


@pytest.mark.phase("F1")
def test_the_runner_is_handed_the_turn_id_the_caller_generated(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """The seat, asserted rather than described.

    `TurnOutcome` needs a turn id and every audit row is filed under one, so the runner
    must receive the id this use case was called with. It must not be left to reconstruct
    one - CLAUDE.md non-negotiable #2 - and it must not be handed a different one, which
    would file the turn's denials under an id that names no turn.
    """
    wiring = _Wiring(caller, FINISHED)
    turn_id = TurnId("turn-seat-1")

    asyncio.run(wiring.use_case.execute(turn_id, _request(session, caller, PROFILE.id)))

    assert len(wiring.runner.run_calls) == 1
    called_turn_id, called_request, _profile, _history = wiring.runner.run_calls[0]
    assert called_turn_id == turn_id, (
        "the runner was not handed the turn id the caller generated"
    )
    assert called_request.profile_id == PROFILE.id


@pytest.mark.phase("F1")
def test_an_unknown_profile_id_raises_and_writes_nothing(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """Never fall back to a default profile: a typo would silently run the agent with the
    wrong permissions."""
    wiring = _Wiring(caller, FINISHED)

    with pytest.raises(KeyError) as raised:
        asyncio.run(
            wiring.use_case.execute(TurnId("turn-1"), _request(session, caller, "no_such_profile"))
        )

    assert "no_such_profile" in str(raised.value), (
        "the error must name the id that was not found, or a typo is untraceable"
    )
    assert wiring.store.requests == [], "nothing may be written for a turn that cannot start"
    assert wiring.runner.run_calls == [], "the model must never be invoked without a profile"
