"""The routing step's reads on a REAL Postgres (L18).

Covers: composition's `_pg_session_has_history` (a follow-up is not routed) and `_pg_turn_lookup`
(a routed turn is visible as running before its row exists).

WHAT IS BEING DEFENDED
    1. A session that already holds another turn of the same tenant is a follow-up; the turn's own
       row, another session's and another tenant's turns are not.
    2. A turn whose route was recorded (its `routed` event) but whose row is not written yet (an
       intent plan persists the turn only with its reply) reads as running, in its tenant only;
       once the row exists the row answers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from agent_core import composition
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId
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
