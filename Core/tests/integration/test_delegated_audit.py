"""Both identities of a delegated turn on a REAL Postgres (S8, migration 0048).

Covers: persistence_pg/delegation_audit_migration.py, audit_repository.py (`record_tool_call`),
        route_audit.py, composition's requester and request-persisted lookups.

WHAT IS BEING DEFENDED
    1. A tool call made under a delegated identity keeps the subject it ran as in `caller` (as a
       peer turn's row does) and the person it acted for in `on_behalf_of`; a person's own call and
       a peer turn's call act for nobody (NULL).
    2. The approval requester of a delegated turn is the PERSON, so four-eyes still compares the
       approver with the human who asked, never with the peer identity.
    3. Whether a turn's request is in the store can be read back by turn id (the intent plan's
       unambiguous fallback, slice 37).
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.persistence_pg.audit_repository import PgAuditSink
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import (
    CallerIdentity,
    TenantId,
    TurnId,
    delegated_identity,
    peer_identity,
)
from tests.fakes import peer_world as w

_DATABASE = "agent_core_delegated_audit_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")

ROUTED = "00000000-0000-4000-8000-000000000001"
OWN = "00000000-0000-4000-8000-000000000002"
PEER = "00000000-0000-4000-8000-000000000003"
NOBODY = "00000000-0000-4000-8000-000000000004"

PERSON = CallerIdentity("manager-7", "shop", TenantId("t"), frozenset({"manager"}))
ALLOWED = PolicyDecision(effect=Effect.ALLOW, reason="peers read", rule_id="peer-reads")


def _connect() -> Any:
    return psycopg.connect(w.conninfo(_DATABASE))


def test_a_delegated_call_records_both_identities_and_the_others_act_for_nobody() -> None:
    async def scenario() -> None:
        await w.fresh_databases(_DATABASE)
        sink = PgAuditSink(_connect)
        await sink.record_tool_call(
            TurnId(ROUTED), delegated_identity(PERSON), "read_ledger", {}, ALLOWED
        )
        await sink.record_tool_call(TurnId(OWN), PERSON, "read_ledger", {}, ALLOWED)
        await sink.record_tool_call(
            TurnId(PEER), peer_identity(TenantId("t")), "read_ledger", {}, ALLOWED
        )

    asyncio.run(scenario())

    with _connect() as conn:
        rows = conn.execute(
            "SELECT turn_id::text, caller, on_behalf_of, rule_id FROM audit_tool_calls "
            "ORDER BY at, id"
        ).fetchall()
    assert rows == [
        (ROUTED, "peer-agent", "manager-7", "peer-reads"),
        (OWN, "manager-7", None, "peer-reads"),
        (PEER, "peer-agent", None, "peer-reads"),
    ]


def test_the_requester_of_a_delegated_turn_is_the_person_and_of_a_peer_turn_unchanged() -> None:
    async def scenario() -> None:
        await w.fresh_databases(_DATABASE)
        sink = PgAuditSink(_connect)
        await sink.record_tool_call(
            TurnId(ROUTED), delegated_identity(PERSON), "read_ledger", {}, ALLOWED
        )
        await sink.record_tool_call(
            TurnId(PEER), peer_identity(TenantId("t")), "read_ledger", {}, ALLOWED
        )

    asyncio.run(scenario())

    lookup = composition._pg_requester_lookup(w.conninfo(_DATABASE))  # noqa: SLF001
    assert asyncio.run(lookup(TurnId(ROUTED))) == "manager-7"
    assert asyncio.run(lookup(TurnId(PEER))) == "peer-agent"
    assert asyncio.run(lookup(TurnId(NOBODY))) is None


def test_whether_a_turns_request_is_persisted_is_read_back_by_its_id() -> None:
    asyncio.run(w.fresh_databases(_DATABASE))
    with _connect() as conn:
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
            "profile_version, profile_snapshot) VALUES (%s, 's', 't', 'p', 'started', '1', '{}')",
            (ROUTED,),
        )

    persisted = composition._pg_request_persisted(w.conninfo(_DATABASE))  # noqa: SLF001
    assert asyncio.run(persisted(TurnId(ROUTED))) is True
    assert asyncio.run(persisted(TurnId(NOBODY))) is False
