"""Progress events on a REAL Postgres and a REAL DBOS (C21, D52).

Phase:   Agent-comms v2, wave 3
Covers:  adapters/driven/events_pg/publisher.py, persistence_pg/turn_events_migration.py,
         turn_workflow._step_progress (a root turn and a peer's turn)

WHAT IS BEING DEFENDED

    1. IDEMPOTENT AND NOTIFYING. Publishing an event id twice leaves one row and sends one NOTIFY
       carrying only the sequence number.
    2. A PEER RUN EMITS ITS EVENTS IN ORDER WITH THE OWNER CHAIN. A asks B; A's turn and B's turn
       each publish `started` and `finished`, B's under A's root turn with A as the asker.
    3. A `tool_called` MADE WITHOUT THE CHAIN IS FILED UNDER THE TURN'S `started` EVENT.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest

from agent_core.adapters.driven.events_pg.publisher import NOTIFY_CHANNEL, PgEventPublisher
from agent_core.adapters.driving.peers.worker import AskDisposition
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.events import EventKind, EventOwner, ProgressEvent, progress_event_id
from tests.fakes import peer_world as w

_DATABASE = "agent_core_turn_events_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def _event(turn: str, kind: EventKind, **changes: Any) -> ProgressEvent:
    base: dict[str, Any] = {
        "event_id": progress_event_id(turn, kind),
        "kind": kind,
        "tenant_id": "t",
        "session_id": "s",
        "turn_id": turn,
        "agent": "agent-x",
        "at": datetime(2026, 10, 12, tzinfo=UTC),
    }
    return ProgressEvent(**{**base, **changes})


def _rows() -> list[tuple[Any, ...]]:
    with psycopg.connect(w.conninfo(_DATABASE)) as conn:
        return conn.execute(
            "SELECT seq, turn_id, root_turn_id, kind, agent, asker_turn_id, asker, call, tool, "
            "session_id FROM turn_events ORDER BY seq"
        ).fetchall()


def test_publishing_twice_leaves_one_row_and_one_notification_with_only_the_sequence() -> None:
    async def scenario() -> list[str]:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        notified: list[str] = []
        async with await psycopg.AsyncConnection.connect(
            w.conninfo(_DATABASE), autocommit=True
        ) as listener:
            await listener.execute(f"LISTEN {NOTIFY_CHANNEL}")
            event = _event("turn-1", EventKind.STARTED)
            await publisher.publish(event)
            await publisher.publish(event)
            async for note in listener.notifies(timeout=1.0):
                notified.append(note.payload)
        return notified

    notified = asyncio.run(scenario())

    (row,) = _rows()
    assert row[1:4] == ("turn-1", "turn-1", "started"), "a root turn files under itself"
    assert notified == [str(row[0])], "one notification, the sequence number alone"


def test_a_tool_call_without_the_chain_is_filed_under_the_turns_started_event() -> None:
    async def scenario() -> None:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        owner = EventOwner("root-1", "asker-1", "asker-agent", "corr-1")
        await publisher.publish(_event("callee-1", EventKind.STARTED, owner=owner, agent="callee"))
        await publisher.publish(
            _event(
                "callee-1",
                EventKind.TOOL_CALLED,
                event_id="tool-1",
                agent="",
                session_id="",
                tool="lookup",
                detail="allow",
            )
        )

    asyncio.run(scenario())

    started, tool = _rows()
    assert tool[2:9] == (
        "root-1",
        "tool_called",
        "callee",
        "asker-1",
        "asker-agent",
        "corr-1",
        "lookup",
    )
    assert tool[9] == "s", "the session is the started event's"
    assert started[2] == "root-1"


def test_a_peer_run_publishes_started_and_finished_with_its_owner_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    world.start.b_answers_directly = True
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None
    import dataclasses

    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        dataclasses.replace(deps, events=PgEventPublisher(w.conninfo(_DATABASE))),
    )

    async def scenario() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: w.rows(_DATABASE), "A's ask to reach the mailbox")
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: world.start.starts(w.B) == 1, "B to run")
            await w.result_of(str(w.ASKING_TURN))
            (corr,) = w.rows(_DATABASE)
            await w.result_of(turn_workflow.peer_workflow_id(corr))
            await w.until(lambda: len(_rows()) >= 4, "four events")

    asyncio.run(scenario())

    events = _rows()
    root = str(w.ASKING_TURN)
    by_turn: dict[str, list[tuple[Any, ...]]] = {}
    for row in events:
        by_turn.setdefault(row[1], []).append(row)
    assert [r[3] for r in by_turn[root]] == ["started", "finished"]
    (callee_turn,) = [t for t in by_turn if t != root]
    callee = by_turn[callee_turn]
    assert [r[3] for r in callee] == ["started", "finished"]
    assert {r[2] for r in callee} == {root}, "filed under the root a person started"
    assert {(r[4], r[5], r[6]) for r in callee} == {(w.B, root, w.A)}
    assert all(cast("int", r[0]) > 0 for r in events)


def test_the_documented_tiering_measurement_query_runs_against_the_schema() -> None:
    """C24 (D56): the SQL in OPERATIONS.md ('Model tiering and its measurement') is valid against
    the migrated schema and reads the columns it names."""
    from pathlib import Path

    import psycopg

    text = (Path(__file__).parents[3] / "docs" / "OPERATIONS.md").read_text()
    sql = text.split("```sql", 1)[1].split("```", 1)[0] if "Model tiering and its" in text else ""
    assert "percentile_cont" in sql, "the measurement query is missing from OPERATIONS.md"

    asyncio.run(w.fresh_databases(_DATABASE))
    with psycopg.connect(w.conninfo(_DATABASE)) as conn:
        assert conn.execute(sql).fetchall() == [], "an empty database has no peer answers"
