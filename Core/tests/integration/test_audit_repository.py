"""Integration tests for the Postgres `AuditSink`.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-14
Covers:  adapters/driven/persistence_pg/audit_repository.py

WHAT IS ACTUALLY BEING GUARDED (CLAUDE.md non-negotiable #6)
    The audit write must not ride the domain transaction. If it does, a failed turn rolls
    back its own evidence and the one turn you most need to explain is the one that left
    no trace. Nothing about that is visible in a green suite written against a single
    pool: every assertion still passes, right up to the incident.

    So the sink is given a connection SOURCE of its own and never a connection belonging
    to the caller. Two of the tests below run that property against a faked pair of
    connections, so they hold on a laptop with no Postgres at all; the third runs the same
    scenario against a real instance and is skipped when none is reachable - mirroring
    tests/integration/test_migrations.py.

REDACTION IS THE SECOND PROPERTY
    Arguments carry credentials and personal data. The sink stores a per-tool ALLOWLIST of
    fields, never a denylist of key names: a denylist misses the field somebody adds next
    month and misses it silently. The assertion is therefore about what is ABSENT from the
    stored jsonb, and about the secret value appearing in no column at all.
"""

from __future__ import annotations

import asyncio
import os
import re
from types import TracebackType
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import audit_repository, migrations
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TURN_ID = TurnId("6f2b0d3a-2f2f-4a3c-9c2e-1f9a5b7c8d90")

_ALLOWLIST = {"freeze_account": frozenset({"account_id"})}

_ARGUMENTS: dict[str, object] = {
    "account_id": "acc-1",
    "password": "hunter2",  # noqa: S105 - a fixture value that must never be stored
}

_DECISION = PolicyDecision(effect=Effect.ALLOW, reason="operator may freeze", rule_id="r-7")


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _sink_class() -> Any:
    """The adapter class, asserted rather than imported at module level.

    An `ImportError` at collection time is not a red test - it is a broken one - so the
    absence of the implementation has to surface here, as a failed assertion inside a test
    that ran.
    """
    sink = getattr(audit_repository, "PgAuditSink", None)
    assert sink is not None, (
        "adapters/driven/persistence_pg/audit_repository.py exposes no PgAuditSink; "
        "docs/TASKS.md#t-f1-14"
    )
    return sink


class _FakeDatabase:
    """Rows that have been committed, plus every statement that reached this database."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []
        self.statements: list[str] = []


class _FakeConnection:
    """A connection with just enough transaction behaviour to tell the two pools apart.

    `autocommit` flushes each statement immediately, which is what an audit write outside
    the domain transaction looks like. Without it, statements are held until `commit()`
    and discarded by `rollback()` - the domain transaction's behaviour.
    """

    def __init__(self, database: _FakeDatabase, *, autocommit: bool = False) -> None:
        self._database = database
        self._pending: list[tuple[str, tuple[Any, ...]]] = []
        self.autocommit = autocommit

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeConnection:
        self._database.statements.append(sql)
        self._pending.append((sql, tuple(params)))
        if self.autocommit:
            self.commit()
        return self

    def commit(self) -> None:
        self._database.rows.extend(self._pending)
        self._pending.clear()

    def rollback(self) -> None:
        self._pending.clear()


def _stored_arguments(row: tuple[str, tuple[Any, ...]]) -> dict[str, object]:
    """The jsonb payload out of a captured INSERT, unwrapped from psycopg's Jsonb."""
    for param in row[1]:
        candidate = getattr(param, "obj", param)
        if isinstance(candidate, dict):
            return candidate
    raise AssertionError(f"no jsonb argument payload in the recorded row: {row!r}")


def test_a_rolled_back_domain_transaction_still_leaves_the_audit_row() -> None:
    """CLAUDE.md non-negotiable #6, with no Postgres required.

    The domain transaction is rolled back after the sink has recorded. The audit row must
    still be there, and the domain connection must never have seen the audit statement -
    a sink that borrowed the caller's connection would fail both halves.
    """
    domain_db = _FakeDatabase()
    audit_db = _FakeDatabase()
    domain = _FakeConnection(domain_db)  # the domain pool: transactional, rollback-able
    sink = _sink_class()(
        lambda: _FakeConnection(audit_db, autocommit=True),  # a pool of its own
        argument_allowlist=_ALLOWLIST,
    )

    domain.execute("INSERT INTO turns (turn_id) VALUES (%s)", (_TURN_ID,))
    asyncio.run(
        sink.record_tool_call(_TURN_ID, _caller(), "freeze_account", _ARGUMENTS, _DECISION)
    )
    domain.rollback()

    assert domain_db.rows == [], "the domain write survived a rollback; the fixture is wrong"
    assert len(audit_db.rows) == 1, "the audit row did not survive the domain rollback"
    assert "audit_tool_calls" in audit_db.rows[0][0]
    assert not any("audit_" in sql for sql in domain_db.statements), (
        "the audit write went through the domain connection; it must use its own pool"
    )


def test_a_non_allowlisted_argument_field_is_absent_from_the_stored_jsonb() -> None:
    """Allowlist, not denylist. `password` was never opted in, so it is not stored - and
    its value must appear in no column of the row either."""
    audit_db = _FakeDatabase()
    sink = _sink_class()(
        lambda: _FakeConnection(audit_db, autocommit=True),
        argument_allowlist=_ALLOWLIST,
    )

    asyncio.run(
        sink.record_tool_call(_TURN_ID, _caller(), "freeze_account", _ARGUMENTS, _DECISION)
    )

    stored = _stored_arguments(audit_db.rows[0])
    assert stored == {"account_id": "acc-1"}
    assert "password" not in stored
    assert "hunter2" not in repr(audit_db.rows), "the secret leaked into another column"


def test_an_unknown_tool_stores_no_arguments_at_all() -> None:
    """Fail closed: a tool with no allowlist entry contributes no argument fields. The
    row still exists, because the intent and the verdict are the evidence."""
    audit_db = _FakeDatabase()
    sink = _sink_class()(
        lambda: _FakeConnection(audit_db, autocommit=True),
        argument_allowlist=_ALLOWLIST,
    )

    asyncio.run(
        sink.record_tool_call(_TURN_ID, _caller(), "unregistered_tool", _ARGUMENTS, _DECISION)
    )

    assert len(audit_db.rows) == 1
    assert _stored_arguments(audit_db.rows[0]) == {}


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_audit_row_outlives_a_real_rolled_back_domain_transaction() -> None:
    """The same two properties against a real instance: separate connection, rollback of
    the domain transaction, audit row still present, secret absent from the jsonb."""
    app_db = "agent_core_audit_test"
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO, app_database=app_db, dbos_database=f"{app_db}_dbos"
        )
    )
    app_conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(app_conninfo))

    with psycopg.connect(app_conninfo) as domain:  # the domain pool's connection
        domain.execute("DELETE FROM audit_tool_calls WHERE turn_id = %s", (_TURN_ID,))
        domain.commit()

        domain.execute(
            "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state) "
            "VALUES (%s, %s, %s, %s, %s)",
            (_TURN_ID, "s-1", "t-1", "p-1", "running"),
        )

        sink = _sink_class()(
            lambda: psycopg.connect(app_conninfo, autocommit=True),
            argument_allowlist=_ALLOWLIST,
        )
        asyncio.run(
            sink.record_tool_call(_TURN_ID, _caller(), "freeze_account", _ARGUMENTS, _DECISION)
        )

        domain.rollback()

        turns = domain.execute(
            "SELECT 1 FROM turns WHERE turn_id = %s", (_TURN_ID,)
        ).fetchall()
        rows = domain.execute(
            "SELECT arguments, rule_id FROM audit_tool_calls WHERE turn_id = %s", (_TURN_ID,)
        ).fetchall()

    assert turns == [], "the domain write survived its own rollback; the fixture is wrong"
    assert len(rows) == 1, "the audit row was rolled back with the domain transaction"
    assert rows[0][0] == {"account_id": "acc-1"}
    assert rows[0][1] == "r-7"
