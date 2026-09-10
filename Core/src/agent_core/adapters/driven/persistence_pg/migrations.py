"""Schema migrations.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-16

TWO LOGICAL DATABASES ON ONE INSTANCE (day 1)
    app   - turns, messages, checkpoints, policy_rules, audit_*
    dbos  - DBOS's own workflow and step state; it owns this, we never touch it

Day 2 adds a THIRD database on the SAME instance for LiteLLM. A second Postgres instance
is never required by the design - splitting instances is an operations decision about
blast radius and backups, worth taking when an incident justifies it.

RULE
    Migrations are forward-only and never destructive on audit tables. An audit table that
    can be dropped by a migration is not an audit table.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Migration:
    """One forward-only, idempotent unit of schema change against the app database."""

    id: str
    sql: str


# Forward-only. Idempotent by construction (IF NOT EXISTS) and tracked in
# schema_migrations so re-running the set never re-applies one. Never add a DROP or
# ALTER against an audit_* table here - see the module RULE above.
APP_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="0001_turns",
        sql="""
        CREATE TABLE IF NOT EXISTS turns (
            turn_id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0002_messages",
        sql="""
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            role TEXT NOT NULL,
            content JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_messages_session_seq ON messages (session_id, seq);
        """,
    ),
    Migration(
        id="0003_checkpoints",
        sql="""
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            covers_through_message BIGINT NOT NULL,
            supersedes UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0004_policy_rules",
        sql="""
        CREATE TABLE IF NOT EXISTS policy_rules (
            rule_id TEXT PRIMARY KEY,
            definition JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0005_audit_tool_calls",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_tool_calls (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            caller TEXT NOT NULL,
            tool TEXT NOT NULL,
            arguments JSONB NOT NULL,
            effect TEXT NOT NULL,
            rule_id TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0006_audit_human_decisions",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_human_decisions (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            tool_call_id TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            approved BOOLEAN NOT NULL,
            note TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0007_audit_media",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_media (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            media_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            direction TEXT NOT NULL,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0008_audit_turn_costs",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_turn_costs (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            input_tokens BIGINT NOT NULL,
            output_tokens BIGINT NOT NULL,
            cached BOOLEAN NOT NULL,
            cost_usd NUMERIC NOT NULL,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
)

_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _ensure_database_sync(admin_conninfo: str, database: str) -> None:
    """Create one logical database if it does not already exist. Idempotent.

    Runs autocommit: CREATE DATABASE cannot run inside a transaction block.
    """
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if exists is None:
            conn.execute(f'CREATE DATABASE "{database}"')


async def ensure_databases(admin_conninfo: str, *, app_database: str, dbos_database: str) -> None:
    """Create the app and dbos logical databases - two separate calls, never one
    statement naming both.

    dbos owns its own database; this only makes sure it exists, it never migrates its
    schema. Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_ensure_database_sync, admin_conninfo, app_database)
    await asyncio.to_thread(_ensure_database_sync, admin_conninfo, dbos_database)


def _run_migrations_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        conn.execute(_TRACKING_TABLE_SQL)
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}
        for migration in APP_MIGRATIONS:
            if migration.id in applied:
                continue
            conn.execute(migration.sql)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (migration.id,))


async def run_migrations(app_conninfo: str) -> None:
    """Apply every pending migration against the app database.

    Safe to call repeatedly: already-applied migrations (tracked in schema_migrations)
    are skipped, and every migration's own SQL is additionally idempotent (IF NOT
    EXISTS), so a re-run is always a no-op.
    """
    await asyncio.to_thread(_run_migrations_sync, app_conninfo)
