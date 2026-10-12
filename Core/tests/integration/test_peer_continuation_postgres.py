"""A follow-up ask continues the callee's session on the REAL stores (C12, D45).

Covers: adapters/driven/peers/mailbox.py (`peer_session_id`, the ownership check),
adapters/driving/peers/worker.py (`_session_for`), adapters/driving/workflow/turn_workflow.py
(`continue_session`) and the conversation store behind the callee's second turn.

WHAT IS BEING DEFENDED
    The asker asks the billing specialist, reads the `session` in the result, and asks again with
    `continue_session`. The specialist's SECOND turn must be filed in the first one's session and so
    see the first exchange; the same session id from another tenant must be refused. Nothing is a
    double but the model: real migrations, stores, `StartTurn`, DBOS and the durable peer starter.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core import composition
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driving.peers import worker as peer_worker
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.adapters.driving.workflow.bootstrap import launch_dbos
from agent_core.domain.peers import AgentId, PeerContinuationRefusedError
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)

_ASKER = "support_triage"
_PEER = "billing_specialist"
_LEDGER_TOOL = "account_history"
_ADMIN = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DATABASE = "agent_core_peer_continuation_pg"
_DEADLINE = 90.0
_CORE = Path(__file__).resolve().parents[2]


def _reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no reachable Postgres instance")


def _conninfo(database: str) -> str:
    head, _, _ = _ADMIN.rpartition("/")
    return f"{head}/{database}"


def _drop() -> None:
    with psycopg.connect(_ADMIN, connect_timeout=5, autocommit=True) as admin:
        for name in (f"{_DATABASE}_dbos_sys", f"{_DATABASE}_dbos", _DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _profiles_with_continuation(target: Path) -> Path:
    """The shipped profiles, with the asker opted in to continuation (the shipped one is not)."""
    shutil.copytree(_CORE / "profiles", target)
    path = target / f"{_ASKER}.yaml"
    text = path.read_text()
    assert "\npeers:\n  enabled: true\n" in text
    path.write_text(
        text.replace(
            "\npeers:\n  enabled: true\n", "\npeers:\n  enabled: true\n  allow_continuation: true\n"
        )
    )
    return target


class _Model:
    """The asker asks twice (the second time continuing); the specialist reports what it saw."""

    __name__ = "continuation_model"

    def __init__(self) -> None:
        self.asker_calls = 0
        self.specialist_histories: list[list[str]] = []
        self.session_seen = ""

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts = [
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        if _LEDGER_TOOL in {t.name for t in info.function_tools}:
            self.specialist_histories.append(prompts)
            return ModelResponse(parts=[TextPart(f"I have seen {len(prompts)} question(s)")])
        self.asker_calls += 1
        if self.asker_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="ask_peer",
                        args={"target": _PEER, "question": "first question"},
                        tool_call_id="call_one",
                    )
                ]
            )
        if self.asker_calls == 2:
            returns = [
                str(part.content)
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            found = re.search(r'session="([^"]+)"', returns[-1])
            assert found, f"the first result named no session: {returns[-1]!r}"
            self.session_seen = found.group(1)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="ask_peer",
                        args={
                            "target": _PEER,
                            "question": "and the second one?",
                            "continue_session": self.session_seen,
                        },
                        tool_call_id="call_two",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("all done")])


def test_a_follow_up_runs_in_the_first_session_and_sees_the_first_exchange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dbos import DBOS

    model = _Model()
    real = runner_module.PydanticAgentRunner

    def factory(model_id: str, base_url: str | None) -> Model:
        del model_id, base_url
        return FunctionModel(model)

    monkeypatch.setattr(
        composition, "PydanticAgentRunner", lambda **kw: real(**kw, model_factory=factory)
    )
    app = _conninfo(_DATABASE)
    profiles = _profiles_with_continuation(tmp_path / "profiles")
    for name, value in {
        "AGENT_CORE_DATABASE_URL": app,
        "AGENT_CORE_AUDIT_DATABASE_URL": app,
        "AGENT_CORE_ADMIN_DATABASE_URL": _ADMIN,
        "AGENT_CORE_PROFILES_DIR": str(profiles),
        "MINIMAX_API_KEY": "placeholder-this-run-calls-no-provider",
    }.items():
        monkeypatch.setenv(name, value)
    tenant = TenantId("t-c12")
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("s-c12"), tenant_id=tenant),
        caller=CallerIdentity(
            subject_id="operator", channel="cli", tenant_id=tenant, roles=frozenset({"operator"})
        ),
        profile_id=_ASKER,
        input=UserInput(text="was this customer charged twice, and what about the other one?"),
    )
    asking = TurnId(str(uuid.uuid4()))
    seen: dict[str, Any] = {}
    container: composition.Container | None = None

    async def serve_one(queue: peer_worker.PeerQueue) -> None:
        report = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DEADLINE
        while report is None and loop.time() < deadline:
            report = await peer_worker.dispatch_next_ask(
                queue,
                turn_workflow.DurablePeerTurnStarter(),
                target=AgentId(_PEER),
                wake=turn_workflow.signal_peer_answer,
            )
            await asyncio.sleep(0.05)
        assert report is not None, "the ask was never claimed"

    async def drive(container: composition.Container) -> None:
        queue = container.mailbox
        assert isinstance(queue, peer_worker.PeerQueue)
        handle = await turn_workflow.enqueue_turn(request, turn_id=asking)
        await serve_one(queue)  # the first question
        await serve_one(queue)  # the follow-up
        seen["outcome"] = await asyncio.wait_for(handle.get_result(), _DEADLINE)
        with pytest.raises(PeerContinuationRefusedError):
            await container.mailbox.ask(  # type: ignore[call-arg]
                container.profiles[_ASKER].peers,
                AgentId(_PEER),
                "borrowed",
                from_session=SessionRef(
                    session_id=request.session.session_id, tenant_id=TenantId("t-other")
                ),
                turn_id=TurnId("elsewhere"),
                hop=0,
                asker=AgentId(_ASKER),
                request_key="x",
                continue_session=model.session_seen,
            )

    try:
        _drop()
        container = asyncio.run(composition.start_container())
        container.domain_pool.open()
        container.audit_pool.open()
        launch_dbos(container)
        asyncio.run(drive(container))
        with psycopg.connect(app) as conn:
            seen["sessions"] = [
                str(r[0])
                for r in conn.execute(
                    "SELECT peer_session_id FROM peer_messages ORDER BY enqueued_at"
                ).fetchall()
            ]
    finally:
        DBOS.destroy()
        if container is not None:
            container.audit_pool.close()
            container.domain_pool.close()
        _drop()

    assert seen["outcome"].result.text == "all done"
    assert (
        len(seen["sessions"]) == 2
        and seen["sessions"][0] == seen["sessions"][1] == model.session_seen
    )
    first, second = model.specialist_histories
    assert first == ["first question"], "the first exchange starts fresh"
    assert second[-1] == "and the second one?" and "first question" in second, (
        f"the follow-up did not see the first exchange: {second!r}"
    )
