"""Driven adapter: `RouteAudit` on Postgres (`audit_turn_routes`, migration 0045, D95).

Phase:   Phase 5 (fast communication), P1
Tests:   Core/tests/integration/test_route_audit.py
Implements: ports/route_audit.py

Built on the AUDIT pool (`composition.py`), never the domain pool: the row must survive the
rollback of the turn it describes (CLAUDE.md #6). One INSERT per consultation, off the event loop.
"""

from __future__ import annotations

import asyncio

from agent_core.adapters.driven.persistence_pg.audit_repository import ConnectionFactory
from agent_core.domain.routing import RouteRecord
from agent_core.domain.turn import TurnId

__all__ = ["PgRouteAudit"]

_INSERT = (
    "INSERT INTO audit_turn_routes (turn_id, tenant_id, router, requested_profile, profile_id, "
    "decision, label, confidence, calibrated, model, intent, elapsed_ms, fallback_reason) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
        )
        await asyncio.to_thread(self._insert, params)

    def _insert(self, params: tuple[object, ...]) -> None:
        with self._connect() as connection:
            connection.execute(_INSERT, params)
