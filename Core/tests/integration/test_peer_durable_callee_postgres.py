"""A durable callee turn against the REAL Postgres stores (C1i, D33).

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driving/workflow/turn_workflow.py (`peer_workflow_id`, the callee's turn id),
         adapters/driven/persistence_pg/conversation_repository.py and the audit tables

WHAT IS BEING DEFENDED

    A durable callee's turn id is its DBOS workflow id. `turns.turn_id`, `audit_*` and the
    other turn tables are UUID columns, so a workflow id that is no UUID made the callee's first
    `INSERT INTO turns` fail on real Postgres. Every earlier peer test used an in-memory store
    for the callee's turn and never saw it. Here nothing on the callee's side is a double but
    the model: real migrations, real stores, the real `StartTurn`, a real DBOS workflow started
    by the real `DurablePeerTurnStarter`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core import composition
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driving.peers import worker as peer_worker
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.adapters.driving.workflow.bootstrap import launch_dbos
from agent_core.domain.peers import AgentId
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
_DATABASE = "agent_core_peer_durable_pg"
_DEADLINE = 90.0


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


class _Model:
    """Told apart by the tools on offer: the specialist alone holds the ledger tool."""

    __name__ = "durable_callee_model"

    def __init__(self) -> None:
        self.asker_calls = 0
        self.peer_calls = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        if _LEDGER_TOOL in {t.name for t in info.function_tools}:
            self.peer_calls += 1
            if self.peer_calls == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name=_LEDGER_TOOL,
                            args={"account_id": "acc-1"},
                            tool_call_id="call_ledger",
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("one charge and one hold")])
        self.asker_calls += 1
        if self.asker_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="ask_peer",
                        args={"target": _PEER, "question": "was it charged twice?"},
                        tool_call_id="call_ask",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("billing says: no double charge")])


def test_a_durable_callee_persists_its_turn_and_answers_the_asker(
    monkeypatch: pytest.MonkeyPatch,
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
    for name, value in {
        "AGENT_CORE_DATABASE_URL": app,
        "AGENT_CORE_AUDIT_DATABASE_URL": app,
        "AGENT_CORE_ADMIN_DATABASE_URL": _ADMIN,
        "MINIMAX_API_KEY": "placeholder-this-run-calls-no-provider",
    }.items():
        monkeypatch.setenv(name, value)
    tenant = TenantId("t-c1i")
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("s-c1i"), tenant_id=tenant),
        caller=CallerIdentity(
            subject_id="operator", channel="cli", tenant_id=tenant, roles=frozenset({"operator"})
        ),
        profile_id=_ASKER,
        input=UserInput(text="was this customer charged twice?"),
    )
    asking = TurnId(str(uuid.uuid4()))
    seen: dict[str, Any] = {}
    container: composition.Container | None = None

    async def drive(container: composition.Container) -> None:
        queue = container.mailbox
        assert isinstance(queue, peer_worker.PeerQueue)
        handle = await turn_workflow.enqueue_turn(request, turn_id=asking)
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
        seen["outcome"] = await asyncio.wait_for(handle.get_result(), _DEADLINE)

    # Everything that opens a resource is inside the `try`, so a failure in the middle of the
    # set-up still destroys DBOS, closes the pools and drops the databases (C1k).
    try:
        _drop()
        container = asyncio.run(composition.start_container())
        container.domain_pool.open()
        container.audit_pool.open()
        launch_dbos(container)
        asyncio.run(drive(container))
        with psycopg.connect(app) as conn:
            seen["turns"] = [str(r[0]) for r in conn.execute("SELECT turn_id FROM turns")]
            seen["audit"] = conn.execute(
                "SELECT count(*) FROM audit_tool_calls WHERE caller = 'peer-agent'"
            ).fetchone()
            seen["state"] = conn.execute("SELECT state FROM peer_messages").fetchall()
    finally:
        DBOS.destroy()
        if container is not None:
            container.audit_pool.close()
            container.domain_pool.close()
        _drop()

    assert seen["outcome"].result.text == "billing says: no double charge"
    callee = [t for t in seen["turns"] if t != str(asking)]
    assert len(callee) == 1, "the callee's turn row was persisted"
    uuid.UUID(callee[0])  # and its id is a real UUID
    assert seen["audit"][0] >= 1, "the callee's tool call reached the audit trail"
    assert seen["state"] == [("answered",)]
