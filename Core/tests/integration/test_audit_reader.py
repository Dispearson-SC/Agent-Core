"""The audit trail records what the winning rule SAID, and a port can read it back.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-07, docs/TASKS.md#t-f11-08
Covers:  adapters/driven/persistence_pg/audit_repository.py
         adapters/driven/persistence_pg/audit_reason_migration.py  (migration 0022)
         ports/audit_reader.py
         adapters/driven/persistence_pg/audit_read_repository.py

THE TWO HALVES ARE ONE CAPABILITY, WHICH IS WHY THEY ARE ONE TEST MODULE
    An audit trail nobody can read is a write-only log, and one that records only a rule
    id cannot explain itself. `audit_tool_calls` carried turn_id, caller, tool, arguments,
    effect, rule_id and at - so the trail could tell an auditor WHICH rule fired and never
    WHAT IT SAID. The sentence is the whole content of the refusal: it is what the model
    is handed as the tool result on DENY and what the human is asked on NEEDS_APPROVAL,
    and a rule's text changes, so the sentence at the time is not reconstructible from
    today's rules.

    The reading half was missing in the mirror-image way. `AuditSink` is write-only BY
    DESIGN and that design is right; `TranscriptReader` projects `transcript_entries`,
    which nothing in the tree writes. So the operator console declared its OWN
    `ToolCallLog` protocol and `main.py` bound a third driven adapter by hand, because
    `Container` had no seat for one. A consumer that has to declare the protocol it
    consumes is the definition of a missing port.

WHY NOTHING HERE IS IMPORTED AT MODULE LEVEL
    An `ImportError` or a `ModuleNotFoundError` at collection time is not a red test, it
    is a broken one. Every module under test is reached through a helper that turns its
    absence into a failed assertion inside a test that actually ran - the same discipline
    `tests/integration/test_audit_repository.py` uses for the same reason.

    `TYPE_CHECKING` is the exception and is not a runtime import: it is what lets
    `_render_as_an_operator_would` below be annotated against the PORT, so mypy - not this
    file - proves the claim that a caller can be typed against it.

NON-NEGOTIABLES THIS MODULE IS GUARDING
    #6  the audit write is append-only and outside the domain transaction. The `reason`
        rides the same INSERT and adds no UPDATE path.
    #9  `AdminIdentity` is never derived from `CallerIdentity`. Reading the trail is an
        administrative act, so the read seat takes the identity type a chat caller's
        cannot satisfy.
    #11 transcripts store everything and filter on read. This port has no USER audience
        at all: it must not become the route by which a chat caller learns tool names.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
from types import TracebackType
from typing import TYPE_CHECKING, Any, get_type_hints

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import audit_repository, migrations
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId
from agent_core.ports.knowledge_admin import AdminIdentity, AdminSubjectId

if TYPE_CHECKING:  # not a runtime import - see the module docstring
    from agent_core.ports.audit_reader import AuditReader

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TURN_ID = TurnId("b1c9f0de-7a4e-4c1b-9f3d-2a6e5c4b8017")
_OTHER_TURN_ID = TurnId("c2d0a1ef-8b5f-4d2c-a04e-3b7f6d5c9128")

_MIGRATION_ID = "0022_audit_tool_calls_reason"
_MIGRATION_MODULE = "agent_core.adapters.driven.persistence_pg.audit_reason_migration"
_READER_MODULE = "agent_core.adapters.driven.persistence_pg.audit_read_repository"
_PORT_MODULE = "agent_core.ports.audit_reader"

# The sentence the rule gave. It is NOT the rule id and it is NOT the arguments: an audit
# row is not a place to spill a payload, and the arguments already have their own column.
_DENY_SENTENCE = "freezing an account needs a case reference; this call carried none"
_ALLOW_SENTENCE = "operators may read an account"

_ALLOWLIST = {"freeze_account": frozenset({"account_id"})}

_ARGUMENTS: dict[str, object] = {
    "account_id": "acc-1",
    "password": "hunter2",  # noqa: S105 - a fixture value that must never be stored
}

_DENIAL = PolicyDecision(effect=Effect.DENY, reason=_DENY_SENTENCE, rule_id="r-deny-7")
_PERMISSION = PolicyDecision(effect=Effect.ALLOW, reason=_ALLOW_SENTENCE, rule_id="r-allow-1")


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="cli",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _admin() -> AdminIdentity:
    """An administrator, minted here the only way one may be minted: from nothing.

    There is deliberately no `_admin_from(caller)` helper in this file. Non-negotiable #9
    is that no code path widens a `CallerIdentity` into an `AdminIdentity`, and a test
    fixture that did it would be the first such path.
    """
    return AdminIdentity(subject_id=AdminSubjectId("ops-alice"), is_superuser=True)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _module(dotted: str, anchor: str, path: str) -> Any:
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:  # pragma: no cover - the pre-implementation path
        raise AssertionError(f"no {path}; docs/TASKS.md#{anchor}") from exc


def _migration_module() -> Any:
    return _module(
        _MIGRATION_MODULE,
        "t-f11-07",
        "adapters/driven/persistence_pg/audit_reason_migration.py",
    )


def _port_module() -> Any:
    return _module(_PORT_MODULE, "t-f11-08", "ports/audit_reader.py")


def _reader_class() -> Any:
    module = _module(
        _READER_MODULE,
        "t-f11-08",
        "adapters/driven/persistence_pg/audit_read_repository.py",
    )
    reader = getattr(module, "PgAuditReadRepository", None)
    assert reader is not None, (
        "adapters/driven/persistence_pg/audit_read_repository.py exposes no "
        "PgAuditReadRepository; docs/TASKS.md#t-f11-08"
    )
    return reader


class _FakeDatabase:
    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []
        self.statements: list[str] = []


class _FakeConnection:
    """Autocommit, i.e. an audit write outside the domain transaction. See #6."""

    def __init__(self, database: _FakeDatabase) -> None:
        self._database = database

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeConnection:
        self._database.statements.append(sql)
        self._database.rows.append((sql, tuple(params)))
        return self


def _record(decision: PolicyDecision, tool: str = "freeze_account") -> _FakeDatabase:
    database = _FakeDatabase()
    sink = audit_repository.PgAuditSink(
        lambda: _FakeConnection(database),
        argument_allowlist=_ALLOWLIST,
    )
    asyncio.run(sink.record_tool_call(_TURN_ID, _caller(), tool, _ARGUMENTS, decision))
    return database


# ---------------------------------------------------------------------------
# t-f11-07 - the sentence, not only the id
# ---------------------------------------------------------------------------


def test_a_denial_records_the_sentence_the_winning_rule_gave_not_only_its_id() -> None:
    """The whole of t-f11-07's claim, with no Postgres required.

    `PolicyDecision.reason` is the sentence the model is handed back as the tool result on
    DENY. If it is not on the row, the trail says a rule fired and cannot say what it
    said - and six months later the rule's text has changed, so nothing can reconstruct
    it. The rule id alone names a rule that no longer reads the way it read that day.
    """
    database = _record(_DENIAL)

    assert len(database.rows) == 1, "the denial left no audit row at all"
    sql, params = database.rows[0]
    assert "audit_tool_calls" in sql
    assert "reason" in sql, (
        "the INSERT names no reason column, so the sentence the rule gave is discarded; "
        "docs/TASKS.md#t-f11-07"
    )
    assert _DENY_SENTENCE in params, (
        "the row records the rule id and not what the rule SAID; a rule's text changes, "
        "so the sentence at the time is the only record of why the refusal made sense"
    )
    assert "r-deny-7" in params, "the rule id was lost while adding the sentence"


def test_an_approval_request_records_its_sentence_too() -> None:
    """NEEDS_APPROVAL is the other audience for the same field: the sentence is the ASK a
    human answered. A column filled only on DENY would leave every approved call in the
    trail with no record of what the human was asked."""
    ask = "freezing an account needs a second operator"
    database = _record(PolicyDecision(effect=Effect.NEEDS_APPROVAL, reason=ask, rule_id="r-2"))

    assert ask in database.rows[0][1], "a NEEDS_APPROVAL row records no ask"


def test_the_sentence_is_the_rules_grounds_and_never_a_dump_of_the_arguments() -> None:
    """An audit row is not a place to spill a payload, and the arguments already have
    their own column - redacted through the per-tool allowlist. A `reason` assembled from
    the call would route a credential around that allowlist into a free-text column no
    redaction ever looks at."""
    database = _record(_DENIAL)

    assert "hunter2" not in repr(database.rows), (
        "a non-allowlisted argument value reached the row; the reason column must carry "
        "the rule's grounds, never the payload"
    )


def test_migration_0022_is_its_own_module_forward_only_and_discovered() -> None:
    """The id is pre-allocated in docs/TASKS.md and the object lives in its own module, so
    `migrations.py` is never a write two anchors share.

    Discovery is what applies it: `discover_app_migrations()` imports every module of the
    package, so there is no registration line for the next agent to forget. And it is
    forward-only - an ALTER that ADDS a nullable column, never a DROP against an audit
    table (`migrations.py`'s own RULE)."""
    module = _migration_module()
    migration = next(
        (value for value in vars(module).values() if isinstance(value, migrations.Migration)),
        None,
    )
    assert migration is not None, "the module defines no Migration object"
    assert migration.id == _MIGRATION_ID, (
        f"migration id {migration.id!r}; docs/TASKS.md pre-allocated {_MIGRATION_ID!r} to "
        "t-f11-07, and an id spent twice is a migration that silently never runs"
    )
    sql = migration.sql.upper()
    assert "ALTER TABLE" in sql and "AUDIT_TOOL_CALLS" in sql and "REASON" in sql
    assert "DROP" not in sql, "a migration that can drop an audit table is not forward-only"
    assert _MIGRATION_ID in {found.id for found in migrations.discover_app_migrations()}, (
        "the migration is not discovered, so production never applies it"
    )


# ---------------------------------------------------------------------------
# t-f11-08 - the port, and a caller that does not declare its own protocol
# ---------------------------------------------------------------------------


async def _render_as_an_operator_would(
    reader: AuditReader, admin: AdminIdentity, turn_id: TurnId
) -> tuple[str, ...]:
    """The console's job, typed against the PORT and against nothing else.

    THIS FUNCTION IS THE ASSERTION t-f11-08 IS ABOUT. It declares no protocol of its own,
    names no adapter, and touches no SQL: everything it needs is on `AuditReader`. Before
    the port existed, `adapters/driving/cli/console.py` had to declare `ToolCallLog`
    itself and `main.py` had to bind a driven adapter by hand, because `Container` had no
    seat. `mypy --strict` is what checks this body - if a field or a member is missing
    from the port, this stops type-checking.
    """
    return tuple(
        f"{call.tool_name} {call.effect.value} [{call.rule_id}] {call.reason}"
        for call in await reader.tool_calls_for_turn(admin, turn_id)
    )


def test_the_read_seat_takes_an_admin_identity_a_chat_caller_cannot_satisfy() -> None:
    """Non-negotiables #9 and #11, on the port itself.

    Reading a tool call's name and arguments back is an ADMIN-audience act. If the seat
    took a `CallerIdentity`, the console's own caller - and any chat client sharing that
    type - would be holding the key to the trail, and #9's separation would be a naming
    convention again rather than something mypy refuses.
    """
    port = _port_module()
    reader = getattr(port, "AuditReader", None)
    assert reader is not None, "ports/audit_reader.py declares no AuditReader Protocol"

    hints = get_type_hints(reader.tool_calls_for_turn)
    identity = [
        name
        for name in inspect.signature(reader.tool_calls_for_turn).parameters
        if name != "self"
    ][0]
    assert hints[identity] is AdminIdentity, (
        f"the read seat is annotated {hints[identity]!r}; it must take AdminIdentity, "
        "which a CallerIdentity cannot be widened into (CLAUDE.md #9)"
    )
    assert not any(hint is CallerIdentity for hint in hints.values()), (
        "a CallerIdentity reaches the audit read; #11 says a user must not learn WHICH "
        "tool is pending, and this port would be the way around that"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_caller_typed_against_the_port_reads_a_turns_tool_calls_back() -> None:
    """The round trip, through the real sink and the real reader against real Postgres.

    Two calls are written under this turn and one under another, so the read is proved to
    be scoped to the turn it was asked about rather than returning the table. They come
    back in the order they were appended, which is the order an operator reads an exchange
    in, and each one carries the sentence its rule gave.
    """
    app_db = "agent_core_audit_reader_test"
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO, app_database=app_db, dbos_database=f"{app_db}_dbos"
        )
    )
    app_conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(_migration_module().apply_audit_reason_migration(app_conninfo))

    with psycopg.connect(app_conninfo, autocommit=True) as setup:
        setup.execute(
            "DELETE FROM audit_tool_calls WHERE turn_id = ANY(%s)",
            ([str(_TURN_ID), str(_OTHER_TURN_ID)],),
        )

    def connect() -> Any:
        return psycopg.connect(app_conninfo, autocommit=True)

    sink = audit_repository.PgAuditSink(connect, argument_allowlist=_ALLOWLIST)
    asyncio.run(
        sink.record_tool_call(_TURN_ID, _caller(), "freeze_account", _ARGUMENTS, _DENIAL)
    )
    asyncio.run(
        sink.record_tool_call(_TURN_ID, _caller(), "read_account", _ARGUMENTS, _PERMISSION)
    )
    asyncio.run(
        sink.record_tool_call(_OTHER_TURN_ID, _caller(), "read_account", {}, _PERMISSION)
    )

    reader = _reader_class()(connect)
    rendered = asyncio.run(_render_as_an_operator_would(reader, _admin(), _TURN_ID))

    assert rendered == (
        f"freeze_account deny [r-deny-7] {_DENY_SENTENCE}",
        f"read_account allow [r-allow-1] {_ALLOW_SENTENCE}",
    ), (
        "the trail did not read back as it was written; a denial whose sentence is absent "
        "is the write-only log t-f11-07 and t-f11-08 exist to close"
    )

    calls = asyncio.run(reader.tool_calls_for_turn(_admin(), _TURN_ID))
    assert calls[0].arguments == {"account_id": "acc-1"}, (
        "the reader re-redacted or dropped the stored arguments; the sink's allowlist is "
        "the one redaction path and two paths drift"
    )
    assert "hunter2" not in repr(calls), "a non-allowlisted value came back out of the trail"
    assert calls[0].caller_subject_id == "u-1", "the row does not say who asked"


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_adapter_satisfies_the_port_structurally() -> None:
    """The console binds an `AuditReader`, not a class name.

    `runtime_checkable` here is the same argument `ports/tool_provider.py` and
    `ports/skill_registry.py` make: whoever wires the seat must be able to check that what
    they were handed answers the question, without importing the adapter.
    """
    port = _port_module()
    assert isinstance(_reader_class()(lambda: None), port.AuditReader), (
        "PgAuditReadRepository does not satisfy AuditReader; the seat composition.py is "
        "about to gain would have to be typed against the adapter instead"
    )
