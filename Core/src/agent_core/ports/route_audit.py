"""Port: RouteAudit - which way did a router send a turn, and why? (D95)

Phase:   Phase 5 (fast communication), P1
Adapter: adapters/driven/persistence_pg/route_audit.py (`audit_turn_routes`, on the audit pool)
Per-vertical: NO

A port of its own rather than a sixth `AuditSink` member: `AuditSink` is frozen with a regression
lock (`tests/unit/test_ports_audit_sink.py`), and a route is not a tool call, a human decision, a
medium or a cost. The rules are `AuditSink`'s all the same (CLAUDE.md #6): append-only, written
outside the domain transaction, one row per consultation, and every member a `record_` returning
nothing.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.routing import RouteRecord
from agent_core.domain.turn import TurnId

__all__ = ["RouteAudit"]


class RouteAudit(Protocol):
    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        """Append one router consultation for `turn_id`. Raises when the row was not written."""
        ...
