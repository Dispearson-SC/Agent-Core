"""The other half of the conversation: what the AGENT said reaches Postgres too.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-29
Covers:  domain/turn.py (the `messages` seat on `TurnOutcome`)
         adapters/driven/agent_pydantic/runner.py (the producer)
         application/start_turn.py (the carrier)
         adapters/driven/persistence_pg/conversation_repository.py (the writer)

WHY THIS FILE EXISTS
    `t-f1-24` proved a history round-trips. It round-tripped the USER's half. `TurnOutcome`
    carried a result and a pending list and no messages, so `StartTurn` had nothing to
    append but the prompt and the agent's own replies were never persisted by anything.
    Every test that needed an agent reply put one there itself - `append_messages` called
    by hand - which is the seventh instance of the pattern this build keeps hitting: the
    fixture that supplies a missing collaborator is what stops anyone noticing it is
    missing.

    So nothing here hand-builds a `ModelResponse` and stores it. Every assertion below is
    made against rows that PRODUCTION wrote: the real `StartTurn`, the real
    `PydanticAgentRunner`, the real `PgConversationStore`, in production's own order.

WHAT IS STOOD IN FOR, AND WHY
    The MODEL, exactly as `test_history_round_trip.py` stands it in: a `FunctionModel` is
    what makes the strongest assertion available - the parts the runner actually produced -
    and the seam under test ends at the store, not at the network. The policy engine, the
    audit sink, the context engine and the skill registry are the use case's collaborators
    rather than this seam's, and `test_f0_end_to_end.py` already pays for the real ones.

    The STORE is not, the RUNNER is not, and the USE CASE is not. Those three are the seam.

NEEDS A REAL POSTGRES and is skipped when one is not reachable - the same pattern as
`test_conversation_repository.py` and `test_history_round_trip.py`, which this file sits
beside deliberately: a write path that could be asserted without a database would not be
the write path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
)
from agent_core.application.start_turn import StartTurn
from agent_core.domain.compaction import CompactionPolicy, CompactionResult, ContextState
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    Usage,
    UserInput,
)
from agent_core.ports.skill_registry import SkillMeta
from tests.fakes import ports as fakes

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DB = "agent_core_agent_reply_test"
_DBOS_DB = "agent_core_agent_reply_test_dbos"

_QUESTION = "How much to ship 2kg to Cordoba?"
_FOLLOW_UP = "And what did you just tell me?"
_ANSWER = "Around 12 USD."
_LATER_ANSWER = "I told you it was around 12 USD."
_TOOL_NAME = "quote_shipping"
_TOOL_CALL_ID = "call_MiXeD_42"
_REASONING = "The customer is in Cordoba, so the inland surcharge applies."


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _app_conninfo() -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{_APP_DB}\1", _ADMIN_CONNINFO)


def _migrated_conninfo() -> str:
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO, app_database=_APP_DB, dbos_database=_DBOS_DB
        )
    )
    conninfo = _app_conninfo()
    asyncio.run(migrations.apply_all_migrations(conninfo))
    return conninfo


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="operator",
        tenant_id=TenantId("t-1"),
        channel="cli",
        roles=frozenset({"operator"}),
    )


def _session() -> SessionRef:
    return SessionRef(session_id=SessionId(f"s-{uuid.uuid4()}"), tenant_id=TenantId("t-1"))


def _profile() -> AgentProfile:
    """A profile with a VERSION, because `append_request` refuses one without (D20)."""
    registry = ProfileVersionRegistry()
    return registry.assign(
        AgentProfile.from_mapping(
            {
                "id": "delivery",
                "persona": "You are a delivery desk assistant.",
                "model": "minimax/MiniMax-M3",
            }
        )
    )


# --------------------------------------------------------------------------------------
# The collaborators that are NOT this seam


class _ContextEngine:
    """Accounting only. `should_compress` is False so no ladder runs inside this seam."""

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
        raise NotImplementedError("this seam never compacts")

    def on_session_end(self, session: SessionRef) -> None:
        return None


class _SkillRegistry:
    async def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        return ()

    async def read(self, name: str) -> str:
        raise NotImplementedError("this seam reads no skill")


class _Script:
    """A `FunctionModel` body that plays a fixed list of responses, one per request."""

    __name__ = "agent_reply_model"

    def __init__(self, *responses: ModelResponse) -> None:
        self._responses = list(responses)
        self.requests: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return self._responses[index]


async def quote_shipping(kg: int, to: str) -> str:
    """Quote a shipment. A real tool body, so a real ToolReturnPart is produced."""
    return json.dumps({"price_usd": "12.00", "kg": kg, "to": to})


def _use_case(
    conninfo: str, profile: AgentProfile, script: _Script
) -> tuple[StartTurn, PgConversationStore]:
    """The production wiring, with only the model and the turn's policy/audit stood in."""
    rule = PolicyRule(
        rule_id="r-allow-everything",
        tool_pattern="*",
        effect=Effect.ALLOW,
        reason="this seam is about persistence, not about policy",
    )
    policy = fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), (rule,)))
    tools = fakes.FakeToolProvider(
        FunctionToolset([quote_shipping], max_retries=0), (_TOOL_NAME,)
    )

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(script)

    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=policy,
        audit=fakes.FakeAuditSink(),
        tools=tools,
        model_factory=factory,
    )
    store = PgConversationStore(conninfo, profiles={profile.id: profile})
    use_case = StartTurn(
        runner=runner,
        tools=tools,
        policy=policy,
        store=store,
        audit=fakes.FakeAuditSink(),
        context=_ContextEngine(),
        skills=_SkillRegistry(),
        profiles={profile.id: profile},
    )
    return use_case, store


def _turn(
    use_case: StartTurn, profile: AgentProfile, session: SessionRef, text: str
) -> TurnOutcome:
    """One turn through the real use case - the only way a row is written here."""
    request = TurnRequest(
        session=session, caller=_caller(), profile_id=profile.id, input=UserInput(text=text)
    )
    return asyncio.run(use_case.execute(TurnId(str(uuid.uuid4())), request))


def _agent_texts(history: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, TextPart)
    ]


def _user_prompts(history: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def _stored_rows(conninfo: str, session: SessionRef) -> list[tuple[str, Any]]:
    with psycopg.connect(conninfo) as conn:
        return [
            (role, content)
            for role, content in conn.execute(
                "SELECT role, content FROM messages WHERE session_id = %s ORDER BY seq ASC",
                (session.session_id,),
            ).fetchall()
        ]


# --------------------------------------------------------------------------------------
# The anchor's own symptom


def test_the_agents_reply_is_in_the_history_the_next_turn_loads() -> None:
    """A conversation with no agent turns in it is not a conversation.

    The second turn's history is what the FIRST turn left behind, read back out of
    Postgres by the same `load_history` the runner is handed. Before `t-f11-29` it was the
    user's prompt and nothing else: a model asked what it had just said could not answer,
    and the transcript showed half an exchange.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    session = _session()
    script = _Script(
        ModelResponse(
            parts=[TextPart(_ANSWER)], usage=RequestUsage(input_tokens=9, output_tokens=4)
        )
    )
    use_case, store = _use_case(conninfo, profile, script)

    _turn(use_case, profile, session, _QUESTION)
    history = asyncio.run(store.load_history(session))

    assert _agent_texts(history) == [_ANSWER], (
        "the agent's own reply must be persisted by the turn that produced it. "
        "TurnOutcome carries the messages the runner added; StartTurn hands them to "
        "append_outcome. docs/TASKS.md#t-f11-29"
    )
    assert _user_prompts(history) == [_QUESTION], (
        "the prompt is written once, by append_request. A runner that also returns the "
        "prompt request makes the model read the customer's sentence twice."
    )
    assert [type(message) for message in history] == [ModelRequest, ModelResponse], (
        "the stored conversation alternates: the question, then the answer to it"
    )

    _turn(use_case, profile, session, _FOLLOW_UP)

    assert len(script.requests) == 2
    second = script.requests[1]
    assert _agent_texts(second) == [_ANSWER], (
        "the model must be handed back what it said a moment ago, or it cannot answer a "
        "question about its own last turn"
    )
    assert _user_prompts(second) == [_QUESTION, _FOLLOW_UP]


def test_a_tool_call_and_its_return_are_persisted_by_the_same_turn() -> None:
    """CLAUDE.md non-negotiable #5, on the path that first made it reachable.

    A tool call separated from its return makes the provider reject the whole
    conversation, and it does so as a 400 on a LATER request - so a write path that stores
    the call and loses the return surfaces nowhere near the turn that caused it. Both
    messages belong to one outcome and land in one transaction; `_unpaired` over what came
    back out of Postgres is the cheapest proof that they did.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    session = _session()
    script = _Script(
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=_TOOL_NAME,
                    args={"kg": 2, "to": "Cordoba"},
                    tool_call_id=_TOOL_CALL_ID,
                )
            ],
            usage=RequestUsage(input_tokens=9, output_tokens=6),
        ),
        ModelResponse(
            parts=[TextPart(_ANSWER)], usage=RequestUsage(input_tokens=20, output_tokens=4)
        ),
    )
    use_case, store = _use_case(conninfo, profile, script)

    _turn(use_case, profile, session, _QUESTION)
    history = asyncio.run(store.load_history(session))

    # Asserted BEFORE the pairing check, because an empty history has nothing unpaired in
    # it: `_unpaired` answers "did anything come apart", not "is anything there".
    assert [type(message) for message in history] == [
        ModelRequest,
        ModelResponse,
        ModelRequest,
        ModelResponse,
    ], (
        "one turn that used a tool stores four messages: the question, the tool call, the "
        "return answering it, and the answer. docs/TASKS.md#t-f11-29"
    )

    orphan_calls, orphan_returns = runner_module._unpaired(history)
    assert not orphan_calls and not orphan_returns, (
        f"the stored turn broke a tool call away from its return: calls without a return "
        f"{sorted(orphan_calls)}, returns without a call {sorted(orphan_returns)}. "
        "CLAUDE.md non-negotiable #5"
    )

    stored_call = next(
        part
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )
    assert stored_call.tool_call_id == _TOOL_CALL_ID, (
        "the tool_call_id is the PROVIDER's string and is matched byte for byte"
    )
    assert any(
        isinstance(part, ToolReturnPart) and part.tool_call_id == _TOOL_CALL_ID
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
    ), "the return that answers the stored call must be stored with it"
    assert _agent_texts(history) == [_ANSWER]


def test_a_reasoning_block_never_becomes_a_stored_message() -> None:
    """Reasoning is ADMIN-only (t-f10-04) and `transcript_entries` projects `content` raw.

    The moment a `ModelResponse` reaches `messages`, a `ThinkingPart` inside it is one
    unnamed projection away from a USER read - CLAUDE.md non-negotiable #11 breached
    through a door nobody opened deliberately. The store names the parts it may write
    instead of handing the blob over, so the rule is structural: a USER read path cannot
    leak what was never written to the table it reads.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    session = _session()
    script = _Script(
        ModelResponse(
            parts=[ThinkingPart(content=_REASONING), TextPart(_ANSWER)],
            usage=RequestUsage(input_tokens=9, output_tokens=11),
        )
    )
    use_case, store = _use_case(conninfo, profile, script)

    _turn(use_case, profile, session, _QUESTION)

    rows = _stored_rows(conninfo, session)
    assert [role for role, _ in rows] == ["user", "assistant"], (
        "the reply must be stored, and stored as the AGENT's - transcript_entries branches "
        "on this exact column"
    )
    blob = json.dumps([content for _, content in rows])
    assert _REASONING not in blob, (
        "the model's reasoning reached the messages table, which transcript_entries "
        "projects verbatim to a USER audience. Reasoning is ADMIN-only - t-f10-04, "
        "CLAUDE.md non-negotiable #11"
    )
    assert "thinking" not in blob, (
        "a ThinkingPart survived into the stored conversation under its own part_kind"
    )

    history = asyncio.run(store.load_history(session))
    assert _agent_texts(history) == [_ANSWER], (
        "stripping the reasoning must not strip the answer with it"
    )

    with psycopg.connect(conninfo) as conn:
        payloads = [
            json.dumps(row[0])
            for row in conn.execute(
                "SELECT payload FROM transcript_entries WHERE session_id = %s "
                "AND entry_id LIKE 'message:%%'",
                (session.session_id,),
            ).fetchall()
        ]
    assert payloads, "the turn produced transcript entries"
    assert not any(_REASONING in payload for payload in payloads), (
        "the USER-facing transcript projection carries the model's reasoning"
    )


def test_replaying_append_outcome_does_not_say_the_agent_answered_twice() -> None:
    """A crashed DBOS step is re-executed, and `append_outcome` has always been idempotent.

    It was idempotent for free while it only ever UPDATEd one row. Now that it also
    appends, a re-executed step would give an operator a transcript in which the agent said
    the same thing twice - and would hand the model its own answer twice on the next turn,
    which reads as a stutter nobody can explain from the code.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    session = _session()
    script = _Script(
        ModelResponse(
            parts=[TextPart(_ANSWER)], usage=RequestUsage(input_tokens=9, output_tokens=4)
        )
    )
    use_case, store = _use_case(conninfo, profile, script)

    request = TurnRequest(
        session=session,
        caller=_caller(),
        profile_id=profile.id,
        input=UserInput(text=_QUESTION),
    )
    turn_id = TurnId(str(uuid.uuid4()))
    outcome = asyncio.run(use_case.execute(turn_id, request))

    asyncio.run(store.append_outcome(turn_id, outcome))

    history = asyncio.run(store.load_history(session))
    assert _agent_texts(history) == [_ANSWER], (
        "a replayed append_outcome appended the agent's reply a second time"
    )


def test_a_second_outcome_for_one_turn_appends_its_own_messages() -> None:
    """The other half of the replay guard, and the half a turn-keyed one gets wrong.

    A suspended turn is written by `append_outcome` TWICE under one turn id: once by
    `StartTurn` when it suspends, and again by `ResumeTurn` when the human finally answers.
    The second call carries DIFFERENT messages - the tool return and everything the agent
    said after it. A guard keyed on the turn's state or on its id would drop exactly those,
    and the only symptom would be a conversation that forgets the answer somebody waited
    three days for. The guard asks the content instead.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    session = _session()
    script = _Script(
        ModelResponse(
            parts=[TextPart(_ANSWER)], usage=RequestUsage(input_tokens=9, output_tokens=4)
        ),
        ModelResponse(
            parts=[TextPart(_LATER_ANSWER)],
            usage=RequestUsage(input_tokens=9, output_tokens=4),
        ),
    )
    use_case, store = _use_case(conninfo, profile, script)

    turn_id = TurnId(str(uuid.uuid4()))
    asyncio.run(
        use_case.execute(
            turn_id,
            TurnRequest(
                session=session,
                caller=_caller(),
                profile_id=profile.id,
                input=UserInput(text=_QUESTION),
            ),
        )
    )

    # A real second outcome, produced by a real run, delivered under the FIRST turn's id -
    # which is the shape `ResumeTurn` writes when a continuation finishes.
    later = _turn(use_case, profile, _session(), _FOLLOW_UP)
    asyncio.run(
        store.append_outcome(
            turn_id, TurnOutcome(turn_id=turn_id, result=later.result, messages=later.messages)
        )
    )

    history = asyncio.run(store.load_history(session))
    assert _agent_texts(history) == [_ANSWER, _LATER_ANSWER], (
        "the second outcome of one turn carries messages of its own and they must be "
        "appended, not mistaken for a replay of the first"
    )
