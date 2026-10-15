"""The routing step's reads on a REAL Postgres (L18).

Covers: composition's `_pg_session_has_history` (a follow-up is not routed) and `_pg_turn_lookup`
(a routed turn is visible as running before its row exists).

WHAT IS BEING DEFENDED
    1. A session that already holds another turn of the same tenant is a follow-up; the turn's own
       row, another session's and another tenant's turns are not.
    2. A turn whose route was recorded (its `routed` event) but whose row is not written yet (an
       intent plan persists the turn only with its reply) reads as running, in its tenant only;
       once the row exists the row answers.
    3. Such a turn does not read as running for ever (slice 39): once its workflow recorded a
       `failed` event, or its route is older than the routed-turn age bound with still no row
       (the process died before the row and nothing recovered it), it reads as `failed`. A row,
       once written, answers instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from agent_core import composition
from agent_core.domain.turn import CallerIdentity, SessionId, SessionRef, TenantId, TurnId
from tests.fakes import peer_world as w

_DATABASE = "agent_core_routing_lookups_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")

EARLIER = "00000000-0000-4000-8000-000000000011"
CURRENT = "00000000-0000-4000-8000-000000000012"
PLANNED = "00000000-0000-4000-8000-000000000013"


def _connect() -> Any:
    return psycopg.connect(w.conninfo(_DATABASE))


def _turn(conn: Any, turn_id: str, session: str, tenant: str, state: str = "finished") -> None:
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
        "profile_version, profile_snapshot) VALUES (%s, %s, %s, 'p', %s, '1', '{}')",
        (turn_id, session, tenant, state),
    )


def _session(session: str, tenant: str = "t") -> SessionRef:
    return SessionRef(session_id=SessionId(session), tenant_id=TenantId(tenant))


def test_a_session_with_another_turn_of_its_tenant_has_history() -> None:
    asyncio.run(w.fresh_databases(_DATABASE))
    with _connect() as conn:
        _turn(conn, EARLIER, "s1", "t")
        _turn(conn, CURRENT, "s2", "t", "started")

    has_history = composition._pg_session_has_history(w.conninfo(_DATABASE))  # noqa: SLF001
    assert asyncio.run(has_history(_session("s1"), TurnId(CURRENT))) is True
    assert asyncio.run(has_history(_session("s2"), TurnId(CURRENT))) is False, "its own row"
    assert asyncio.run(has_history(_session("s1", "other"), TurnId(CURRENT))) is False
    assert asyncio.run(has_history(_session("s3"), TurnId(CURRENT))) is False


def _routed_event(
    conn: Any, turn_id: str, tenant: str, kind: str = "routed", age: str = "0"
) -> None:
    conn.execute(
        "INSERT INTO turn_events (event_id, tenant_id, session_id, turn_id, root_turn_id, kind, "
        "agent, detail, at) VALUES (%s, %s, 's9', %s, %s, %s, 'owner', %s, "
        "now() - %s::interval)",
        (f"{turn_id}:{kind}", tenant, turn_id, turn_id, kind, kind, f"{age} seconds"),
    )


def test_a_routed_turn_without_its_row_yet_reads_as_running_in_its_tenant_only() -> None:
    asyncio.run(w.fresh_databases(_DATABASE))
    with _connect() as conn:
        _routed_event(conn, PLANNED, "t")
    lookup = composition._pg_turn_lookup(w.conninfo(_DATABASE))  # noqa: SLF001
    person = CallerIdentity("manager-7", "shop", TenantId("t"))
    stranger = CallerIdentity("x", "shop", TenantId("other"))

    view = asyncio.run(lookup(TurnId(PLANNED), person))
    assert view is not None and (view.status, view.text) == ("running", None)
    assert asyncio.run(lookup(TurnId(PLANNED), stranger)) is None
    assert asyncio.run(lookup(TurnId(EARLIER), person)) is None, "no row and no route: unknown"

    with _connect() as conn:
        _turn(conn, PLANNED, "s9", "t")
        conn.execute(
            "UPDATE turns SET result = '{\"text\": \"done\"}' WHERE turn_id = %s", (PLANNED,)
        )
    view = asyncio.run(lookup(TurnId(PLANNED), person))
    assert view is not None and (view.status, view.text) == ("finished", "done")


PERSON = CallerIdentity("manager-7", "shop", TenantId("t"))


def test_a_routed_turn_whose_workflow_failed_before_its_row_reads_as_failed() -> None:
    asyncio.run(w.fresh_databases(_DATABASE))
    with _connect() as conn:
        _routed_event(conn, PLANNED, "t")
        _routed_event(conn, PLANNED, "t", kind="failed")
        _routed_event(conn, EARLIER, "other", kind="failed")  # another tenant's failure
        _routed_event(conn, EARLIER, "t")
    lookup = composition._pg_turn_lookup(w.conninfo(_DATABASE))  # noqa: SLF001

    view = asyncio.run(lookup(TurnId(PLANNED), PERSON))
    assert view is not None and (view.status, view.text) == ("failed", None)
    view = asyncio.run(lookup(TurnId(EARLIER), PERSON))
    assert view is not None and view.status == "running", "a failure of another tenant"


def test_a_routed_turn_still_without_its_row_past_the_age_bound_reads_as_failed() -> None:
    asyncio.run(w.fresh_databases(_DATABASE))
    bound = composition.ROUTED_TURN_ROW_MAX_AGE_SECONDS
    with _connect() as conn:
        _routed_event(conn, PLANNED, "t", age=str(bound + 60))
        _routed_event(conn, EARLIER, "t", age=str(bound - 60))
    lookup = composition._pg_turn_lookup(w.conninfo(_DATABASE))  # noqa: SLF001

    view = asyncio.run(lookup(TurnId(PLANNED), PERSON))
    assert view is not None and view.status == "failed"
    view = asyncio.run(lookup(TurnId(EARLIER), PERSON))
    assert view is not None and view.status == "running", "younger than the bound"


def test_once_the_row_exists_it_answers_even_after_a_failed_or_old_route() -> None:
    asyncio.run(w.fresh_databases(_DATABASE))
    with _connect() as conn:
        _routed_event(conn, PLANNED, "t", age="100000")
        _routed_event(conn, PLANNED, "t", kind="failed")
        _turn(conn, PLANNED, "s9", "t", "started")
    lookup = composition._pg_turn_lookup(w.conninfo(_DATABASE))  # noqa: SLF001

    view = asyncio.run(lookup(TurnId(PLANNED), PERSON))
    assert view is not None and view.status == "running"
