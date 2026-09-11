"""Integration tests for Postgres schema migrations.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-16
Covers:  adapters/driven/persistence_pg/migrations.py

One assertion here needs no database at all: it is a static check over the migration SQL
text, because an audit_* table must stay append-only no matter what Postgres is reachable.
The database-creation-separation check runs against a faked connector, also with no real
Postgres required. The idempotency check needs a real Postgres instance and is skipped
when one is not reachable - see migrations.py's module docstring RULE.
"""

from __future__ import annotations

import asyncio
import os
import re

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_DESTRUCTIVE_ON_AUDIT = re.compile(r"\b(DROP|ALTER)\b[^;]*\baudit_\w+", re.IGNORECASE)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def test_no_migration_is_destructive_against_an_audit_table() -> None:
    """Static check, no database required.

    An audit table that can be DROPped or ALTERed by a migration is not an audit table -
    see the RULE in migrations.py's module docstring. This must hold even when no
    Postgres instance is reachable at all, so it asserts over the raw SQL text.
    """
    for migration in migrations.APP_MIGRATIONS:
        assert not _DESTRUCTIVE_ON_AUDIT.search(migration.sql), (
            f"migration {migration.id!r} is destructive against an audit table"
        )


def test_ensure_databases_creates_app_and_dbos_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app and dbos logical databases are created through two separate calls -
    never a single statement naming both. No real Postgres required: the connector is
    faked.

    THE SECOND NAME IS DERIVED, NOT PASSED (t-f11-20). `ensure_databases` ignores
    `dbos_database` and creates `<app>_dbos_sys`, which is the name DBOS itself opens; the
    repo convention `<app>_dbos` had exactly one consumer and nothing ever connected to
    it. The parameter is still accepted so callers written against the old convention keep
    working, so it is still passed here - and deliberately not expected back."""
    created: list[str] = []

    def fake_ensure_database_sync(admin_conninfo: str, database: str) -> None:
        created.append(database)

    monkeypatch.setattr(
        "agent_core.adapters.driven.persistence_pg.migrations._ensure_database_sync",
        fake_ensure_database_sync,
    )

    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO,
            app_database="agent_core_app",
            dbos_database="agent_core_dbos",
        )
    )

    assert created == ["agent_core_app", "agent_core_app_dbos_sys"]


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_migrations_are_idempotent_on_rerun() -> None:
    """Running the same migration set twice must not error and must not apply any
    migration a second time."""
    app_db = "agent_core_migrations_test"
    dbos_db = "agent_core_migrations_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )

    app_conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)

    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(migrations.run_migrations(app_conninfo))  # must not raise, must not duplicate

    with psycopg.connect(app_conninfo) as conn:
        rows = conn.execute("SELECT id FROM schema_migrations").fetchall()
        applied_ids = [row[0] for row in rows]

    assert len(applied_ids) == len(set(applied_ids)), "a migration id was applied twice"
    # `discover_app_migrations()`, not `APP_MIGRATIONS`: `run_migrations` is now an alias
    # for `apply_all_migrations` (see its own docstring - the partial applier was a trap
    # twenty fixtures reached for), so every sibling module's migration is applied too.
    # Comparing against the eight in APP_MIGRATIONS could not pass on any database.
    assert set(applied_ids) == {
        migration.id for migration in migrations.discover_app_migrations()
    }
