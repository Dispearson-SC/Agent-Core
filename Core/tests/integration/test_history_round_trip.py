"""The seam nothing ever crossed: `append` -> `load_history` -> `run`, for real.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-24
Covers:  adapters/driven/persistence_pg/conversation_repository.py (the encoding)
         adapters/driven/agent_pydantic/runner.py (`_as_message_history`, the reader)

WHY THIS FILE EXISTS, WHICH IS THE WHOLE POINT OF THE ANCHOR
    `PgConversationStore` wrote `{"role": ..., "content": ...}` and `PydanticAgentRunner`
    accepts Pydantic AI `ModelMessage` values. Neither side was ever handed the other's
    output: every runner test injected a history it had built itself, and every store test
    asserted on rows it had inserted itself. Both suites were green and the FIRST turn of
    the operator console died - `append_request` writes before `load_history` reads, so
    the very first read already carried a message the runner refused.

    That is the seventh time in this build that a collaborator every test supplied and
    production never exercised hid a defect. The test that would have caught it is this
    one, and it is the shape to copy: the REAL store, its REAL encoding, and the REAL
    runner, in production's order, with nothing hand-built in between.

WHAT IS AND IS NOT STOOD IN FOR
    The history is NOT: it comes out of Postgres, through the adapter's own decode.

    The MODEL is, and deliberately. A `FunctionModel` is what makes the strongest
    assertion available - the messages the provider was actually handed - and the seam
    under test ends at the runner, not at the network. `test_f0_end_to_end.py` already
    pays for a real provider; paying again here would buy flakiness, not coverage.

    The policy engine and the audit sink are fakes for the same reason: they are the
    runner's collaborators, not this seam's, and `test_f0_end_to_end.py` drives the real
    ones.

NEEDS A REAL POSTGRES and is skipped when one is not reachable - same pattern as
`test_conversation_repository.py`, which this file sits beside deliberately: a round trip
that could be written without a database would not be a round trip.
"""

from __future__ import annotations

import asyncio
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
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
)
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DB = "agent_core_history_round_trip_test"
_DBOS_DB = "agent_core_history_round_trip_test_dbos"

_QUESTION = "How much to ship 2kg to Cordoba?"
_FOLLOW_UP = "And to Rosario?"
_TOOL_NAME = "quote_shipping"


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


def _store(conninfo: str, profile: AgentProfile) -> PgConversationStore:
    return PgConversationStore(conninfo, profiles={profile.id: profile})


class _Recorder:
    """A `FunctionModel` body that keeps the messages of every request it served."""

    __name__ = "history_round_trip_model"

    def __init__(self, text: str = "Around 12 USD.") -> None:
        self.text = text
        self.requests: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        return ModelResponse(
            parts=[TextPart(self.text)],
            usage=RequestUsage(input_tokens=9, output_tokens=4),
        )


def _runner(recorder: _Recorder) -> Any:
    rule = PolicyRule(
        rule_id="r-allow-everything",
        tool_pattern="*",
        effect=Effect.ALLOW,
        reason="this seam is about history, not about policy",
    )

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(recorder)

    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), (rule,))),
        audit=fakes.FakeAuditSink(),
        model_factory=factory,
    )


def _user_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


# --------------------------------------------------------------------------------------
# The round trip itself


def _turn(
    store: PgConversationStore,
    runner: Any,
    profile: AgentProfile,
    session: SessionRef,
    text: str,
) -> list[ModelMessage]:
    """One turn, driven in exactly the order `application/start_turn.py` drives it.

    Load the conversation so far, persist this request into it, run. The order is the
    subject of half the assertions below, so it is written out here once rather than
    re-typed per turn where a transposition would look like a typo.
    """
    request = TurnRequest(
        session=session, caller=_caller(), profile_id=profile.id, input=UserInput(text=text)
    )
    history = asyncio.run(store.load_history(session))
    # Asserted HERE, before the runner is reached, so the encoding defect fails as an
    # assertion naming the store rather than as an `UnsupportedHistoryError` raised three
    # layers down - which is how it reached a human the first time.
    assert all(isinstance(message, (ModelRequest, ModelResponse)) for message in history), (
        "load_history must return Pydantic AI ModelMessage values: the runner accepts "
        "nothing else, and every other reader of a history in this tree - the compaction "
        "ladder, _unpaired, _pending_tool_calls - is typed the same way. "
        "docs/TASKS.md#t-f1-24"
    )
    asyncio.run(store.append_request(TurnId(str(uuid.uuid4())), request))
    asyncio.run(runner.run(TurnId(str(uuid.uuid4())), request, profile, history))
    return history


def test_a_second_turn_loads_back_as_messages_the_real_runner_accepts() -> None:
    """The anchor's own symptom: two turns in one session, real store, real runner.

    THE SECOND TURN IS THE ONE THAT USED TO DIE. Its history is the first turn's message
    read back out of Postgres, and `load_history` returned `{"role": ..., "content": ...}`
    dicts that `_as_message_history` refuses by name. Asserting the SHAPE before handing
    it to the runner keeps the reproduction on an assertion rather than on a traceback
    from three layers down, and names what was wrong: the store's encoding, not the reader.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    store = _store(conninfo, profile)
    session = _session()
    recorder = _Recorder()
    runner = _runner(recorder)

    first = _turn(store, runner, profile, session, _QUESTION)
    assert first == [], "a fresh session has nothing said before it"

    second = _turn(store, runner, profile, session, _FOLLOW_UP)

    assert second, "the first turn was persisted, so the second turn's history is not empty"
    assert _user_prompts(second) == [_QUESTION], (
        "the stored message must carry the first turn's text verbatim, and must NOT "
        "already carry the second turn's: history is what was said BEFORE this turn"
    )

    assert len(recorder.requests) == 2
    assert _user_prompts(recorder.requests[0]) == [_QUESTION]
    assert _user_prompts(recorder.requests[1]) == [_QUESTION, _FOLLOW_UP], (
        "the model must see each question exactly once, in order. Persisting the request "
        "before reading the history makes the current sentence arrive twice - once as "
        "prior context and once as the new prompt - and Pydantic AI merges the two rather "
        "than deduplicating them, so the provider is billed twice for it."
    )


def test_a_tool_call_and_its_return_survive_the_round_trip_together() -> None:
    """CLAUDE.md non-negotiable #5, asserted against the ENCODING rather than the ladder.

    A tool call separated from its return makes the provider reject the whole
    conversation, and it does so as a 400 on a LATER request - so an encoding that loses
    `tool_call_id`, or that stores a call and drops its return, surfaces nowhere near the
    store that caused it. `_unpaired` is the runner's own pairing check; running it over
    what came BACK out of Postgres is the cheapest place to prove the encoding is not the
    thing that will break the pairing.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    store = _store(conninfo, profile)
    session = _session()

    call = ToolCallPart(
        tool_name=_TOOL_NAME, args={"kg": 2, "to": "Cordoba"}, tool_call_id="call_MiXeD_42"
    )
    exchange: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content=_QUESTION)]),
        ModelResponse(parts=[call]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name=_TOOL_NAME,
                    content={"price_usd": "12.00"},
                    tool_call_id=call.tool_call_id,
                )
            ]
        ),
        ModelResponse(parts=[TextPart("Around 12 USD.")]),
    ]
    asyncio.run(store.append_messages(session, exchange))

    history = asyncio.run(store.load_history(session))

    assert [type(message) for message in history] == [type(m) for m in exchange]
    orphan_calls, orphan_returns = runner_module._unpaired(history)
    assert not orphan_calls and not orphan_returns, (
        "the round trip broke a tool call away from its return - CLAUDE.md #5"
    )

    stored_call = next(
        part
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )
    assert stored_call.tool_call_id == call.tool_call_id, (
        "the tool_call_id is the PROVIDER's string and is matched byte for byte: a "
        "re-cased or regenerated id binds to no pending call and the turn stays suspended "
        "forever, with nothing raised anywhere"
    )
    assert stored_call.args == call.args

    # And the runner accepts what came back, which is the other half of the claim.
    recorder = _Recorder()
    request = TurnRequest(
        session=session,
        caller=_caller(),
        profile_id=profile.id,
        input=UserInput(text=_FOLLOW_UP),
    )
    asyncio.run(_runner(recorder).run(TurnId(str(uuid.uuid4())), request, profile, history))
    assert len(recorder.requests) == 1
    sent = recorder.requests[0]
    assert any(
        isinstance(part, ToolReturnPart) and part.tool_call_id == call.tool_call_id
        for message in sent
        if isinstance(message, ModelRequest)
        for part in message.parts
    ), "the tool return reached the provider still bound to its call"


def test_the_stored_row_still_reads_as_the_users_own_message_in_the_transcript() -> None:
    """`messages.role` is not free-form: a USER-facing read branches on it.

    `transcript_entries` (migration 0016) decides USER_MESSAGE versus AGENT_MESSAGE with
    `CASE WHEN m.role = 'user'`. Settling the encoding on Pydantic AI's `ModelMessage`
    made it tempting to store the library's own discriminator - 'request' / 'response' -
    in that column, which would have relabelled every customer's own sentence as something
    the agent said. Nothing in the suite looked at that column, and nobody reads a
    transcript in a test, so the only reviewer would have been the person it lied to.
    """
    conninfo = _migrated_conninfo()
    profile = _profile()
    store = _store(conninfo, profile)
    session = _session()

    asyncio.run(
        store.append_request(
            TurnId(str(uuid.uuid4())),
            TurnRequest(
                session=session,
                caller=_caller(),
                profile_id=profile.id,
                input=UserInput(text=_QUESTION),
            ),
        )
    )

    with psycopg.connect(conninfo) as conn:
        kinds = [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM transcript_entries WHERE session_id = %s "
                "AND entry_id LIKE 'message:%%'",
                (session.session_id,),
            ).fetchall()
        ]

    assert kinds == ["user_message"], (
        "the inbound turn must project as the USER's message; 'agent_message' here means "
        "the transcript is attributing the customer's words to the agent"
    )


def test_one_sessions_history_never_carries_another_sessions_messages() -> None:
    """The tenant-shaped half of the same round trip, through the real encode and decode."""
    conninfo = _migrated_conninfo()
    profile = _profile()
    store = _store(conninfo, profile)
    mine = _session()
    theirs = _session()

    asyncio.run(
        store.append_messages(mine, [ModelRequest(parts=[UserPromptPart(content="mine")])])
    )
    asyncio.run(
        store.append_messages(theirs, [ModelRequest(parts=[UserPromptPart(content="theirs")])])
    )

    history = asyncio.run(store.load_history(mine))
    assert _user_prompts(history) == ["mine"]
