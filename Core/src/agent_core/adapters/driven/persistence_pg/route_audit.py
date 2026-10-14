"""Driven adapter: `RouteAudit` on Postgres (`audit_turn_routes`, migrations 0045-0046, D95, and
`audit_turn_intents`, migration 0047, D97).

Phase:   Phase 5 (fast communication), P1
Tests:   Core/tests/integration/test_route_audit.py
Implements: ports/route_audit.py

Built on the AUDIT pool (`composition.py`), never the domain pool: the row must survive the
rollback of the turn it describes (CLAUDE.md #6). One INSERT per consultation, off the event loop.
"""

from __future__ import annotations

import asyncio

from agent_core.adapters.driven.persistence_pg.audit_repository import ConnectionFactory
from agent_core.domain.intents import IntentRecord
from agent_core.domain.routing import RouteRecord
from agent_core.domain.turn import TurnId

__all__ = ["PgRouteAudit"]

_INSERT = (
    "INSERT INTO audit_turn_routes (turn_id, tenant_id, router, requested_profile, profile_id, "
    "decision, label, confidence, calibrated, model, intent, elapsed_ms, fallback_reason, "
    "reason, input_tokens, output_tokens) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_INSERT_INTENT = (
    "INSERT INTO audit_turn_intents (turn_id, tenant_id, intent, profile_id, plan_used, "
    "tool_count, reply_mode, fallback_reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)


class PgRouteAudit:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        params = (
            str(turn_id),
            tenant_id,
            record.router,
            record.requested_profile,
            record.profile_id,
            None if record.decision is None else record.decision.value,
            record.label,
            record.confidence,
            record.calibrated,
            record.model,
            record.intent,
            record.elapsed_ms,
            record.fallback_reason,
            record.reason,
            record.input_tokens,
            record.output_tokens,
        )
        await asyncio.to_thread(self._insert, _INSERT, params)

    async def record_intent(self, turn_id: TurnId, tenant_id: str, record: IntentRecord) -> None:
        params = (
            str(turn_id),
            tenant_id,
            record.intent,
            record.profile_id,
            record.plan_used,
            record.tool_count,
            record.reply_mode,
            record.fallback_reason,
        )
        await asyncio.to_thread(self._insert, _INSERT_INTENT, params)

    def _insert(self, sql: str, params: tuple[object, ...]) -> None:
        with self._connect() as connection:
            connection.execute(sql, params)
