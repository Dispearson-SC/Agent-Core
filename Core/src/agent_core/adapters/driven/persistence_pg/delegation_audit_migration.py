"""Migration 0048: both identities of a delegated turn on the audit trail (S8).

Phase:   Phase 5 (fast communication), P9
Tests:   Core/tests/integration/test_route_audit.py, Core/tests/integration/test_migrations.py

A routed turn runs under the peer identity acting on behalf of the person (`domain/turn.py::
delegated_identity`). The subject it ran as was already on every row (`audit_tool_calls.caller`);
the person it acted for was not. Nullable columns, NULL on every row that acted for nobody (a
person's own turn, a peer turn, rows written before it):

    audit_tool_calls.on_behalf_of                     the person a delegated call acted for
    audit_turn_routes.actor, .on_behalf_of            the route that delegated, both identities
    audit_turn_intents.actor, .on_behalf_of           the plan that ran delegated, both identities

`on_behalf_of` is also what the approval requester reads first (composition
`_REQUESTER_LOOKUP_SQL`), so four-eyes still compares the approver with the PERSON.
Additive and idempotent.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

DELEGATION_AUDIT_MIGRATION = Migration(
    id="0048_audit_delegated_identities",
    sql="""
    ALTER TABLE audit_tool_calls ADD COLUMN IF NOT EXISTS on_behalf_of TEXT;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS actor TEXT;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS on_behalf_of TEXT;
    ALTER TABLE audit_turn_intents ADD COLUMN IF NOT EXISTS actor TEXT;
    ALTER TABLE audit_turn_intents ADD COLUMN IF NOT EXISTS on_behalf_of TEXT;
    """,
)


async def apply_delegation_audit_migration(app_conninfo: str) -> None:
    """Apply `DELEGATION_AUDIT_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, DELEGATION_AUDIT_MIGRATION)
