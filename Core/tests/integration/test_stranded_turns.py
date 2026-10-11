"""`stranded-turns` against a REAL DBOS system database (D30).

A turn written under application version `.1` and left PENDING is what a deploy that bumped the
pin leaves behind. The check must list it, must not list the row of the version it claims, and
must not list history (finished workflows). It reads; it never launches DBOS.

The skip pattern, the production DBOS config and the abandon trick are the ones
`test_partition_recovery.py` uses.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.cli import stranded_turns
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.composition import Settings
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DATABASE = "agent_core_stranded_turns_test"
_OLD_VERSION = "agent-core-turns.1"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _conninfo(database: str) -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{database}"


def _drop() -> None:
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DATABASE}_dbos_sys", _DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _Finishes:
    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


class _Channel:
    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        return None


def _request(session: str) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId("t")),
        caller=CallerIdentity(
            subject_id="u", channel="telegram", tenant_id=TenantId("t"), roles=frozenset()
        ),
        profile_id="p-1",
        input=UserInput(text="hi"),
    )


async def _run_turns(sessions: list[str]) -> list[str]:
    from dbos import DBOS, DBOSConfig

    settings = Settings(app_conninfo=_conninfo(_DATABASE))
    DBOS(config=cast("DBOSConfig", dict(composition.dbos_config(settings))))
    DBOS.launch()
    try:
        ids: list[str] = []
        for session in sessions:
            handle = await turn_workflow.enqueue_turn(_request(session))
            await asyncio.wait_for(handle.get_result(), 30)
            ids.append(handle.workflow_id)
        return ids
    finally:
        DBOS.destroy()


def _set_row(workflow_id: str, *, status: str, version: str) -> None:
    with psycopg.connect(_conninfo(f"{_DATABASE}_dbos_sys"), autocommit=True) as system:
        system.execute(
            "UPDATE dbos.workflow_status SET status = %s, application_version = %s "
            "WHERE workflow_uuid = %s",
            (status, version, workflow_id),
        )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_it_lists_open_turns_of_other_versions_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", _Finishes()),
            channels=ChannelRegistry((("telegram", _Channel()),)),
        ),
    )
    _drop()
    try:
        stranded_id, pending_now_id, finished_old_id = asyncio.run(
            _run_turns(["s-1", "s-2", "s-3"])
        )
        _set_row(stranded_id, status="PENDING", version=_OLD_VERSION)
        current = composition.PINNED_DBOS_APPLICATION_VERSION
        _set_row(pending_now_id, status="PENDING", version=current)
        _set_row(finished_old_id, status="SUCCESS", version=_OLD_VERSION)

        settings = Settings(app_conninfo=_conninfo(_DATABASE))
        found = stranded_turns.find_stranded_workflows(settings)

        assert [item.workflow_id for item in found] == [stranded_id]
        assert found[0].application_version == _OLD_VERSION
        assert found[0].status == "PENDING"
        assert found[0].queue_name == turn_workflow.TURN_QUEUE_NAME
        assert stranded_turns.run_stranded_check(settings, write_line=lambda _: None) == 1

        _set_row(stranded_id, status="CANCELLED", version=_OLD_VERSION)
        assert stranded_turns.run_stranded_check(settings, write_line=lambda _: None) == 0
    finally:
        _drop()


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_database_dbos_never_touched_is_clean_not_an_error() -> None:
    _drop()
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{_DATABASE}_dbos_sys"')
    try:
        settings = Settings(app_conninfo=_conninfo(_DATABASE))
        assert stranded_turns.find_stranded_workflows(settings) == ()
    finally:
        _drop()


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_missing_system_database_is_reported_as_unavailable_not_clean() -> None:
    _drop()
    settings = Settings(app_conninfo=_conninfo(_DATABASE))

    assert stranded_turns.run_stranded_check(settings, write_line=lambda _: None) == 2
