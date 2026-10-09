"""A suspension the user is told about - and told nothing else about.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-06

THE TWO REQUIREMENTS THAT PULL AGAINST EACH OTHER

    D17 (docs/DECISIONS.md, and docs/ARCHITECTURE.md section 15): a peer suspension can
    take hours - the peer runs its own turn and may itself suspend on a human. So before
    the turn suspends, the agent must tell its own user *"I am checking on that."*
    Silence is indistinguishable from a crash, and the customer leaves.

    CLAUDE.md non-negotiable #11: the user must see THAT something is pending and must
    NOT see WHICH tool - and, for a peer ask, not which peer either. `PeerPolicy
    .visibility` defaults to NONE precisely so a customer-service agent cannot advertise
    whose personal assistant it just paged; a notice that names the peer hands that back
    on the user-facing side.

    Both halves must hold at once. A notice that says nothing satisfies #11 and fails
    D17; a notice that repeats the model's own tool-call arguments satisfies D17 and
    fails #11.

WHY THESE ASSERT PROPERTIES AND NOT A STRING FROM THE PRODUCTION MODULE

    Importing the notice constant and asserting equality with it would assert that a
    string equals itself. The properties below are what the two rules actually ask for:
    the notice is not the raw ask, it carries none of the three facts that must not
    travel, and it still says something a person can read.

WHY THE ASSERTION IS ABOUT THE PERSISTED COPY, NOT ONLY THE RETURNED ONE

    `StartTurn` returns into `_step_start`, and the workflow only then reaches
    `DBOS.recv_async` - the point at which the turn actually suspends. A notice that
    exists only on the returned value is one that a crash in that window loses, and the
    recovered turn reads the durable record instead. "Before the turn suspends" is only
    true if the notice is in what `ConversationStore.append_outcome` was handed.

Fakes only - no database, no network, no model. `FakeContextEngine` and
`FakeSkillRegistry` are declared here rather than imported from another test module for
the same reason `test_start_turn.py` declares its own: `tests/fakes/ports.py` does not own
them yet, and a test that imports another test's helpers breaks when that module moves.
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

PROFILE = AgentProfile(id="customer_service", persona="You help customers.", model="m")

# The tool's public name, as `adapters/driven/tools/peers.py` declares it. Spelled here
# rather than imported: `application/` may not import an adapter, and a test that reached
# into one to describe application behaviour would be asserting the wrong layer.
ASK_PEER = "ask_peer"

RULES = (
    PolicyRule(
        rule_id="r-peers",
        tool_pattern="ask_*",
        effect=Effect.ALLOW,
        reason="This profile may consult its peers.",
    ),
    PolicyRule(
        rule_id="r-pricing",
        tool_pattern="pricing_apply",
        effect=Effect.NEEDS_APPROVAL,
        reason="A price change above 15% needs a human.",
    ),
)

# The three facts the notice must never carry, named so a fourth is one line to add.
PEER_ID = "personal-assistant-of-alice"
PEER_QUESTION = "What delivery window did Alice agree to for order 4417?"
FORBIDDEN = (PEER_ID, ASK_PEER, PEER_QUESTION)

# What the runner hands back when the model called `ask_peer`. `reason` is the field a
# human-facing renderer reads (see `adapters/driven/human/gateway.py::_render`), and this
# is the shape that leaks: the peer's id and the customer's own question, verbatim.
#
# `DELEGATION`, not `EVIDENCE` (t-f9-08). It used to wear `EVIDENCE` because that was the
# closest of the two kinds that existed, and `start_turn.py` told a peer ask apart by its
# TOOL NAME. The kind is the discriminator now - `domain/turn.py::PendingKind` - so a
# fixture still labelled `EVIDENCE` describes a request for the user's own photo that
# happens to be called `ask_peer`, which is a different thing and is not what this module
# is about. Every assertion below is unchanged, including the one that pins `kind` itself
# through the notice rewrite.
PEER_SUSPENSION = TurnOutcome(
    turn_id=TurnId("turn-1"),
    pending=(
        PendingRequest(
            kind=PendingKind.DELEGATION,
            tool_call_id=ToolCallId("call-9"),
            tool_name=ASK_PEER,
            arguments={"target": PEER_ID, "question": PEER_QUESTION},
            reason=f"Waiting on {PEER_ID} to answer: {PEER_QUESTION}",
        ),
    ),
)

# An ordinary approval. Here the human IS the one being asked, so the real reason is what
# they need in order to answer - scrubbing this one would be a regression, not a fix.
APPROVAL_REASON = "A price change above 15% needs a human."
APPROVAL_SUSPENSION = TurnOutcome(
    turn_id=TurnId("turn-1"),
    pending=(
        PendingRequest(
            kind=PendingKind.APPROVAL,
            tool_call_id=ToolCallId("call-7"),
            tool_name="pricing_apply",
            arguments={"pct_change": 22},
            reason=APPROVAL_REASON,
        ),
    ),
)

FINISHED = TurnOutcome(
    turn_id=TurnId("turn-1"),
    result=TurnResult(
        text="It arrives Thursday.",
        usage=Usage(input_tokens=90, output_tokens=12, cost_usd=Decimal("0.002")),
    ),
)


class FakeContextEngine:
    """F5 owns the real engine; this records the accounting calls a turn must make."""

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

    async def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        return ()

    async def read(self, name: str) -> str:
        raise NotImplementedError("F6 - docs/TASKS.md#t-f6-02")


def _use_case(
    caller: CallerIdentity, outcome: TurnOutcome
) -> tuple[StartTurn, FakeConversationStore]:
    store = FakeConversationStore()
    use_case = StartTurn(
        runner=FakeAgentRunner(outcome),
        tools=FakeToolProvider(toolset=object(), tool_names=(ASK_PEER, "pricing_apply")),
        policy=FakeToolPolicy(RuleSet.for_caller(caller, RULES)),
        store=store,
        audit=FakeAuditSink(),
        context=FakeContextEngine(),
        skills=FakeSkillRegistry(),
        profiles={PROFILE.id: PROFILE},
    )
    return use_case, store


def _request(session: SessionRef, caller: CallerIdentity) -> TurnRequest:
    return TurnRequest(
        session=session,
        caller=caller,
        profile_id=PROFILE.id,
        input=UserInput(text="When is my order arriving?"),
    )


def _only_pending(outcome: TurnOutcome) -> PendingRequest:
    assert len(outcome.pending) == 1, f"expected one pending request, got {outcome.pending}"
    return outcome.pending[0]


@pytest.mark.phase("F9")
def test_a_peer_suspension_is_communicable_before_the_turn_suspends(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """D17: the user is told something is happening, and told it BEFORE the turn suspends.

    Asserted on the copy `append_outcome` received, because that is the one that exists on
    the far side of a crash between `StartTurn` returning and `DBOS.recv_async` beginning
    the durable wait.
    """
    use_case, store = _use_case(caller, PEER_SUSPENSION)

    outcome = asyncio.run(use_case.execute(TurnId("turn-1"), _request(session, caller)))

    assert store.outcomes, "the suspension was never persisted, so nothing can tell the user"
    persisted_turn_id, persisted = store.outcomes[-1]
    assert persisted_turn_id == TurnId("turn-1")
    assert persisted.is_suspended, "a persisted peer ask must still read as suspended"

    notice = _only_pending(persisted).reason
    assert notice != _only_pending(PEER_SUSPENSION).reason, (
        "the raw peer ask reached the persisted record unchanged - there is no notice"
    )
    assert len(notice.split()) >= 3, (
        f"a notice has to say something a person can read; got {notice!r}"
    )
    assert _only_pending(outcome).reason == notice, (
        "the returned outcome must carry the same notice - a renderer reading the return "
        "value must not be able to see a different story from the database"
    )


@pytest.mark.phase("F9")
@pytest.mark.silent
def test_the_notice_names_neither_the_peer_nor_the_tool(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """CLAUDE.md #11: THAT something is pending, never WHICH tool - nor which peer.

    SILENT-BUG AREA. Nothing fails when the notice leaks: the turn suspends, resumes and
    completes exactly as before. The only symptom is a customer-service agent telling a
    customer whose personal assistant it just paged, which is the fishing boundary
    `PeerPolicy.visibility=NONE` exists to hold.
    """
    use_case, store = _use_case(caller, PEER_SUSPENSION)

    outcome = asyncio.run(use_case.execute(TurnId("turn-1"), _request(session, caller)))

    for label, subject in (("persisted", store.outcomes[-1][1]), ("returned", outcome)):
        reason = _only_pending(subject).reason
        for secret in FORBIDDEN:
            assert secret.casefold() not in reason.casefold(), (
                f"the {label} notice carries {secret!r}: {reason!r}"
            )


@pytest.mark.phase("F9")
def test_the_notice_does_not_cost_the_turn_its_way_back(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """Everything resumption keys on survives verbatim.

    `PendingRequest.tool_call_id` must round-trip exactly or the resumed result is dropped
    silently and the agent loops asking again; `tool_name` and `arguments` are what
    whoever turns this deferred call into a real `AgentMailbox.ask()` reads. Redacting the
    RECORD rather than the notice would trade one silent bug for a worse one.
    """
    use_case, store = _use_case(caller, PEER_SUSPENSION)

    asyncio.run(use_case.execute(TurnId("turn-1"), _request(session, caller)))

    persisted = _only_pending(store.outcomes[-1][1])
    original = _only_pending(PEER_SUSPENSION)
    assert persisted.tool_call_id == original.tool_call_id
    assert persisted.tool_name == original.tool_name
    assert persisted.arguments == original.arguments
    assert persisted.kind == original.kind


@pytest.mark.phase("F9")
def test_an_approval_still_reaches_the_human_who_has_to_answer_it(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """The rewrite is peer-shaped, not suspension-shaped.

    For an approval the person reading the ask IS the one who must decide, and the reason
    is the whole of what they have to decide on. A blanket rewrite would leave them
    approving an unnamed action - which is how a four-eyes rule becomes a rubber stamp.
    """
    use_case, store = _use_case(caller, APPROVAL_SUSPENSION)

    asyncio.run(use_case.execute(TurnId("turn-1"), _request(session, caller)))

    assert _only_pending(store.outcomes[-1][1]).reason == APPROVAL_REASON, (
        "an approval's reason is the human's only input; it must pass through untouched"
    )


@pytest.mark.phase("F9")
def test_a_finished_turn_is_untouched(session: SessionRef, caller: CallerIdentity) -> None:
    """No notice and no rebuild: a turn that answered has nothing to be communicable about."""
    use_case, store = _use_case(caller, FINISHED)

    outcome = asyncio.run(use_case.execute(TurnId("turn-1"), _request(session, caller)))

    assert outcome is FINISHED, "a finished turn must not be rebuilt on the way out"
    assert store.outcomes[-1][1] is FINISHED
