"""Integration tests for the Postgres `AuditSink`.

Phase:   F1, extended in F3
Tasks:   docs/TASKS.md#t-f1-14, docs/TASKS.md#t-f3-14
Covers:  adapters/driven/persistence_pg/audit_repository.py
         adapters/driven/persistence_pg/audit_rejected_migration.py
         application/decide_approval.py - the refusal path only

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
import importlib
import os
import re
from types import TracebackType
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import audit_repository, migrations
from agent_core.application.decide_approval import DecideApproval, FourEyesError
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId, ToolCallId, TurnId

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
            # No `dbos_database=`: the name is DERIVED (t-f11-20). Passing one names a
            # database nothing creates and nothing opens.
            _ADMIN_CONNINFO,
            app_database=app_db,
        )
    )
    app_conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(app_conninfo))

    with psycopg.connect(app_conninfo) as domain:  # the domain pool's connection
        domain.execute("DELETE FROM audit_tool_calls WHERE turn_id = %s", (_TURN_ID,))
        domain.commit()

        # D20's two columns are NOT NULL (migration 0009), so a hand-written turn row
        # has to carry them. This INSERT used to omit both and pass anyway - against a
        # scratch database created before 0009 existed, where the columns were still
        # nullable. On a database migrated from empty, which is what CI and every fresh
        # clone get, it fails.
        domain.execute(
            "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
            "profile_version, profile_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (_TURN_ID, "s-1", "t-1", "p-1", "running", 1, "{}"),
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


# ---------------------------------------------------------------------------
# t-f3-14b - the refused decision. docs/TASKS.md#t-f3-14, docs/DECISIONS.md#d25
#
# Two halves were missing after the port was widened: there was no table for
# `record_rejected_decision` to write into, and nothing called it. Both are asserted
# here, because either one alone reproduces the silence D25 called a gap - a control
# nobody can show fired is indistinguishable from one that was never wired.
# ---------------------------------------------------------------------------

_CORRELATION = "c-unguessable"
_TOOL_CALL_ID = ToolCallId("call_abc123")
_REQUESTER = "u-alice"
_SECOND_PERSON = "u-bob"

# A note is a payload. It travels to `record_human_decision` on the paths where a human
# really decided, and it must NOT be copied into the refusal row: an audit row is not a
# place to spill what the attempt carried.
_NOTE = "spillable-payload-marker"


class _FixedGateway:
    """Correlates from a fixed table; publishes nowhere."""

    def __init__(self, table: dict[str, tuple[TurnId, ToolCallId]]) -> None:
        self._table = table

    async def publish(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return self._table.get(correlation_id)


class _RecordingSignal:
    """The wake-up. A refused attempt must never reach it."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(
        self, turn_id: TurnId, tool_call_id: ToolCallId, approved: bool, note: str | None
    ) -> None:
        self.calls.append((turn_id, tool_call_id, approved, note))


def _requester_is(answer: str | None) -> Any:
    async def _lookup(turn_id: TurnId) -> str | None:
        return answer

    return _lookup


def _refuse(sink: Any, signal: _RecordingSignal, requester: str | None, approver: str) -> None:
    """Drive one four-eyes refusal through the real use case and the real sink."""
    use_case = DecideApproval(
        gateway=_FixedGateway({_CORRELATION: (_TURN_ID, _TOOL_CALL_ID)}),
        audit=sink,
        signal=signal,
        requester=_requester_is(requester),
    )
    with pytest.raises(FourEyesError):
        asyncio.run(use_case.execute(_CORRELATION, approver, approved=True, note=_NOTE))


def _rejected_migration_module() -> Any:
    """The 0019 module, asserted rather than imported at module level.

    A `ModuleNotFoundError` at collection time is not a red test - it is a broken one -
    so its absence has to surface as a failed assertion inside a test that ran.
    """
    try:
        return importlib.import_module(
            "agent_core.adapters.driven.persistence_pg.audit_rejected_migration"
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - the pre-implementation path
        raise AssertionError(
            "no adapters/driven/persistence_pg/audit_rejected_migration.py; migration "
            "0019 is pre-allocated to docs/TASKS.md#t-f3-14"
        ) from exc


def test_a_refused_approval_leaves_a_row_saying_who_tried_on_what_and_why() -> None:
    """The whole of t-f3-14b's claim, with no Postgres required.

    A four-eyes refusal must append a row of its own - WHO attempted it, on WHAT request,
    and WHY it was refused - and that row must survive the turn failing. The domain
    transaction is rolled back after the refusal, exactly as a failing turn rolls back its
    own writes, and the refusal row must still be there: it is the ONLY evidence the
    control fired (CLAUDE.md non-negotiable #6).

    The refusal must also never reach the wake-up signal, and must not copy the attempt's
    note into the trail. An audit row is grounds, not a payload dump.
    """
    domain_db = _FakeDatabase()
    audit_db = _FakeDatabase()
    domain = _FakeConnection(domain_db)  # the domain pool: transactional, rollback-able
    sink = _sink_class()(lambda: _FakeConnection(audit_db, autocommit=True))
    signal = _RecordingSignal()

    domain.execute("INSERT INTO turns (turn_id) VALUES (%s)", (_TURN_ID,))
    _refuse(sink, signal, requester=_REQUESTER, approver=_REQUESTER)
    domain.rollback()

    assert domain_db.rows == [], "the domain write survived a rollback; the fixture is wrong"
    assert len(audit_db.rows) == 1, (
        "a refused four-eyes approval left no trace; docs/DECISIONS.md#d25"
    )
    sql, params = audit_db.rows[0]
    assert "audit_rejected_decisions" in sql, (
        "the refusal was filed somewhere other than its own table; record_human_decision "
        "would assert a decision the human never made"
    )
    assert params[0] == str(_TURN_ID), "the row does not say which turn"
    assert params[1] == str(_TOOL_CALL_ID), "the row does not say which request"
    assert params[2] == _REQUESTER, "the row does not say who attempted it"
    assert isinstance(params[3], str) and params[3].strip(), (
        "the row carries no grounds; 'refused' with no reason cannot be told apart from "
        "a clerical error"
    )
    assert _NOTE not in repr(audit_db.rows), "the attempt's payload leaked into the trail"
    assert signal.calls == [], "a refused attempt woke the turn"
    assert not any("audit_" in statement for statement in domain_db.statements), (
        "the refusal row went through the domain connection; it must use its own pool"
    )


def test_the_two_refusal_paths_are_told_apart_by_their_grounds() -> None:
    """D25 refuses for two different reasons - the approver IS the requester, and nobody
    is on record - and the reader six months later is asking which control fired. One
    shared string would answer neither question."""
    self_approval = _FakeDatabase()
    unknown_requester = _FakeDatabase()

    _refuse(
        _sink_class()(lambda: _FakeConnection(self_approval, autocommit=True)),
        _RecordingSignal(),
        requester=_REQUESTER,
        approver=_REQUESTER,
    )
    _refuse(
        _sink_class()(lambda: _FakeConnection(unknown_requester, autocommit=True)),
        _RecordingSignal(),
        requester=None,
        approver=_SECOND_PERSON,
    )

    assert len(self_approval.rows) == 1 and len(unknown_requester.rows) == 1
    assert unknown_requester.rows[0][1][2] == _SECOND_PERSON
    assert self_approval.rows[0][1][3] != unknown_requester.rows[0][1][3], (
        "both refusal paths file the same grounds; the trail cannot say which fired"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_rejected_decision_row_outlives_a_real_rolled_back_transaction() -> None:
    """The same property against a real instance, which is also the only place the missing
    table shows up: without migration 0019 this INSERT raises `UndefinedTable`."""
    app_db = "agent_core_audit_test"
    asyncio.run(
        migrations.ensure_databases(
            # No `dbos_database=`: the name is DERIVED (t-f11-20). Passing one names a
            # database nothing creates and nothing opens.
            _ADMIN_CONNINFO,
            app_database=app_db,
        )
    )
    app_conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(_rejected_migration_module().apply_audit_rejected_migration(app_conninfo))

    reason = "four-eyes: the approver started this turn"
    with psycopg.connect(app_conninfo) as domain:
        domain.execute("DELETE FROM audit_rejected_decisions WHERE turn_id = %s", (_TURN_ID,))
        domain.commit()

        # D20's two columns are NOT NULL (migration 0009), so a hand-written turn row
        # has to carry them. This INSERT used to omit both and pass anyway - against a
        # scratch database created before 0009 existed, where the columns were still
        # nullable. On a database migrated from empty, which is what CI and every fresh
        # clone get, it fails.
        domain.execute(
            "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
            "profile_version, profile_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (_TURN_ID, "s-1", "t-1", "p-1", "running", 1, "{}"),
        )

        sink = _sink_class()(lambda: psycopg.connect(app_conninfo, autocommit=True))
        asyncio.run(
            sink.record_rejected_decision(_TURN_ID, _TOOL_CALL_ID, _REQUESTER, reason)
        )

        domain.rollback()

        turns = domain.execute(
            "SELECT 1 FROM turns WHERE turn_id = %s", (_TURN_ID,)
        ).fetchall()
        rows = domain.execute(
            "SELECT tool_call_id, subject_id, reason FROM audit_rejected_decisions "
            "WHERE turn_id = %s",
            (_TURN_ID,),
        ).fetchall()

    assert turns == [], "the domain write survived its own rollback; the fixture is wrong"
    assert rows == [(str(_TOOL_CALL_ID), _REQUESTER, reason)], (
        "the refusal row was rolled back with the transaction it refused"
    )
