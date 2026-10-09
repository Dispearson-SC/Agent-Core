"""Three things the schema claimed and the database did not do.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-20, docs/TASKS.md#t-f11-21, docs/TASKS.md#t-f11-22
Covers:  adapters/driven/persistence_pg/migrations.py
         adapters/driven/persistence_pg/audit_repository.py
         adapters/driven/persistence_pg/audit_tenant_migration.py (migration 0023)

WHY THESE THREE BELONG IN ONE FILE
    Each is the same defect in a different place: a schema step that LOOKS like the one
    production performs and is not. A database created under a name nobody opens, an
    applier that applies a third of the migrations, and an audit table that cannot say
    which tenant a row belongs to. None of the three fails anything today, because in
    every case a fixture or a runtime supplies what the step forgot - which is the
    pattern docs/STATE.md has now recorded seven times.

    So the assertions below are deliberately stated against the PRODUCTION objects:
    `composition.dbos_config` for the name DBOS will actually open, the real
    `PgToolPolicy` for the query a turn actually runs, the real `PgAuditSink` for the row
    an audit trail actually gets.

NOTHING HERE PRINTS, LOGS OR ASSERTS ON A CREDENTIAL VALUE.
    The connection strings are the admin URL the rest of the integration suite already
    uses, and no assertion message formats one.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, cast

import psycopg
import pytest
from dbos._dbos_config import get_system_database_url
from psycopg.conninfo import conninfo_to_dict

from agent_core import composition
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.audit_repository import PgAuditSink
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TURN_ID = TurnId("1f6c4e21-7b0d-4a55-9b3e-2c8d5a6f7e10")
_TENANT = TenantId("t-audited")

_DECISION = PolicyDecision(effect=Effect.ALLOW, reason="operator may freeze", rule_id="r-7")


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="cli",
        tenant_id=_TENANT,
        roles=frozenset({"operator"}),
    )


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _conninfo_for(database: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{database}\1", _ADMIN_CONNINFO)


def _database_name(conninfo: str) -> str:
    dbname = conninfo_to_dict(conninfo).get("dbname")
    assert isinstance(dbname, str) and dbname
    return dbname


def _fresh_database(database: str) -> str:
    """A database with nothing in it - not even `schema_migrations`.

    Dropped rather than truncated, and dropped BEFORE it is created: a leftover row in
    `schema_migrations` from an earlier, fuller run would make the applier skip the very
    migration these tests assert it applies, and the schema would look correct because
    somebody else had already built it.

    Created with raw SQL rather than through `ensure_databases`, so that the two tests
    below fail on what they are about - the applier and the column - and never on the
    bootstrap the test above is about.
    """
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{database}"')
    return _conninfo_for(database)


def _columns_of(conninfo: str, table: str) -> set[str]:
    with psycopg.connect(conninfo) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            ).fetchall()
        }


def test_the_bootstrap_creates_the_database_the_runtime_actually_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """t-f11-20. No Postgres required: the connector is faked, the NAMES are the subject.

    `ensure_databases` created `<app>_dbos`, the repo-wide convention. DBOS 2.31.1 derives
    its system database from the app URL it is handed - `<app>_dbos_sys` - and opens that.
    So the bootstrap made an empty database nobody opens, and the one the runtime uses was
    left to whoever got there first.

    The expected name is not spelled out here. It is asked of DBOS itself, through the
    very config `composition.dbos_config` hands the runtime, so this test cannot agree
    with a stale copy of the suffix - if DBOS changes how it derives the name, this fails
    rather than quietly pinning yesterday's answer.
    """
    created: list[str] = []

    def fake_ensure_database_sync(admin_conninfo: str, database: str) -> None:
        created.append(database)

    app_conninfo = "postgresql://user@localhost:5432/agent_core_app"
    settings = composition.Settings(app_conninfo=app_conninfo)
    # dbos's own derivation, over production's own config. `ConfigFile` is a TypedDict and
    # `DbosConfig` is a plain mapping, so the cast is a typing formality, not a claim.
    system_database = _database_name(
        get_system_database_url(cast("Any", composition.dbos_config(settings)))
    )

    monkeypatch.setattr(
        "agent_core.adapters.driven.persistence_pg.migrations._ensure_database_sync",
        fake_ensure_database_sync,
    )

    # Exactly the call `composition.start_container` makes today, legacy argument and
    # all, so this asserts what a real cold start creates rather than a shape invented
    # here. The argument names a database nothing opens; the assertion below says so.
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO,
            app_database="agent_core_app",
            dbos_database="agent_core_app_dbos",
        )
    )

    assert created == ["agent_core_app", system_database], (
        "the bootstrap does not create the database DBOS opens. DBOS derives its system "
        f"database as {system_database!r} from the app URL and creates it itself, with "
        "the APPLICATION's credentials against `postgres` - best effort, and silently "
        "skipped when that role may not CREATE DATABASE. Anything else this creates is a "
        "database nothing ever opens (docs/TASKS.md#t-f11-20)."
    )


@_needs_postgres
def test_a_database_built_by_run_migrations_can_answer_a_policy_query() -> None:
    """t-f11-22. The applier that looks like the real one, against the real query.

    `run_migrations` applied F1's `APP_MIGRATIONS` only, so a database built with it had
    `policy_rules` without migration 0012's `tenant_id` column - and every policy SELECT
    raised `UndefinedColumn`. The adapter then reported, correctly and uselessly, that the
    policy store was unreachable.

    The subject is the real `PgToolPolicy` against a real, EMPTY `policy_rules`. An empty
    table is a legitimate state (t-f11-06) and must answer "no rule matched"; a broken
    schema must not be able to hide inside that same DENY.
    """
    conninfo = _fresh_database("agent_core_schema_truths_policy_test")
    asyncio.run(migrations.run_migrations(conninfo))

    failures: list[BaseException] = []
    policy = PgToolPolicy(
        lambda: psycopg.connect(conninfo), on_error=failures.append
    )
    rules = asyncio.run(policy.load_rules(_caller()))
    decision = policy.decide(rules, "freeze_account", {})

    assert not failures, (
        "a database built by run_migrations cannot answer the policy query the adapter "
        f"emits; it failed with {type(failures[0]).__name__ if failures else ''}. "
        "A second, partial applier that looks like the real one is a trap, and "
        "`test_policy_tenant_sql.py` builds its fixture this way "
        "(docs/TASKS.md#t-f11-22)."
    )
    assert decision.effect is Effect.DENY, "an empty policy table must still fail closed"
    assert "unreachable" not in decision.reason, (
        "a reachable, empty policy_rules on a correctly migrated database is being "
        "reported as an unreachable store, which sends a human to check a database that "
        "is fine (docs/TASKS.md#t-f11-06)."
    )


@_needs_postgres
def test_an_audit_row_records_the_tenant_it_belongs_to() -> None:
    """t-f11-21. The column, written by the sink from `caller.tenant_id`.

    `audit_tool_calls` carried no tenant, so `AuditReader` could be scoped only to a turn.
    The fix is NOT a join to `turns`: the audit write is outside the domain transaction ON
    PURPOSE (CLAUDE.md non-negotiable #6), so a turn whose domain writes rolled back has
    audit rows and no `turns` row - and an inner join would hide exactly the rows that
    separation exists to preserve.

    This test therefore writes the audit row and NEVER writes the `turns` row. If the
    tenant can only be read back through a join, there is nothing to join to and this
    fails - which is the whole point.
    """
    conninfo = _fresh_database("agent_core_schema_truths_audit_test")
    asyncio.run(migrations.apply_all_migrations(conninfo))

    assert "tenant_id" in _columns_of(conninfo, "audit_tool_calls"), (
        "audit_tool_calls has no tenant_id, so an audit trail can be scoped only to a "
        "turn and a tenant's evidence cannot be read back without joining `turns` - the "
        "join that erases the rows of a rolled-back turn (docs/TASKS.md#t-f11-21)."
    )

    sink = PgAuditSink(lambda: psycopg.connect(conninfo, autocommit=True))
    asyncio.run(
        sink.record_tool_call(_TURN_ID, _caller(), "freeze_account", {}, _DECISION)
    )

    with psycopg.connect(conninfo) as conn:
        turns = conn.execute(
            "SELECT 1 FROM turns WHERE turn_id = %s", (str(_TURN_ID),)
        ).fetchall()
        rows = conn.execute(
            "SELECT tenant_id FROM audit_tool_calls WHERE turn_id = %s", (str(_TURN_ID),)
        ).fetchall()

    assert turns == [], "the fixture wrote a turns row; the no-join property is untested"
    assert [row[0] for row in rows] == [str(_TENANT)], (
        "the audit row does not record the tenant it belongs to, and there is no `turns` "
        "row to recover it from - which is the state every rolled-back turn leaves "
        "(CLAUDE.md non-negotiable #6)."
    )
