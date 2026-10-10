"""Integration tests for what the audit trail records about a tool call's arguments.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-35
Covers:  adapters/driven/persistence_pg/audit_repository.py - the redaction half only

WHAT IS BEING GUARDED
    `ARGUMENT_ALLOWLIST` shipped empty, so `_redact` kept nothing and every tool call in
    the trail rendered "arguments: none recorded". t-f11-11 asks the console to show a
    call WHOLE - name, arguments, and what the rule said - and two of those three parts
    could never appear.

    An allowlist is the right shape: a denylist misses the field somebody adds next month
    and misses it silently. An EMPTY allowlist is not a safe default, it is an absent
    decision. Deny-by-default is correct for a permission; for a RECORD it means the trail
    is blank exactly where an incident needs it, and "we do not know what this agent did"
    is the one answer an audit trail must never give.

    So these tests assert the decision EXISTS for every tool this process serves, that the
    sink production builds - no constructor override, the module constant - actually
    stores it, and that a field opted in by PRESENCE stores no value anywhere on the row.

WHY THE SINK IS BUILT WITH NO `argument_allowlist` HERE
    `composition.build_container` constructs `PgAuditSink(PoolConnections(audit_pool))`
    and passes no allowlist, so the module constant IS the shipped redaction policy. A
    test that supplied its own would be the fixture-hides-the-gap shape docs/STATE.md
    records seven times: it would have stayed green through every wave that shipped `{}`.

NO POSTGRES REQUIRED
    The property under test is what the sink puts in the INSERT's jsonb parameter, not
    what Postgres does with it, so these run against a faked connection - the same choice
    tests/integration/test_audit_repository.py makes for its redaction assertions.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any

from agent_core.adapters.driven.persistence_pg import audit_repository
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId

_TURN_ID = TurnId("3d1f8c2b-77aa-4f0e-9b31-0c4e6a2d55f1")

_DECISION = PolicyDecision(
    effect=Effect.NEEDS_APPROVAL,
    reason="a price change above 15% is approved by a human",
    rule_id="r-pricing-apply",
)

# A note body that must never reach a column. Fraud case notes are free text the model
# wrote about a person under investigation; `_NOTE` stands in for that, and every
# assertion below checks the whole captured row for it, not just the jsonb.
_NOTE = "subject seen at 14 Willow Lane, partner name Marta"


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-9",
        channel="console",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


class _FakeDatabase:
    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []


class _FakeConnection:
    """Just enough of a connection to capture the statement and its parameters."""

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
        self._database.rows.append((sql, tuple(params)))
        return self


def _production_sink(database: _FakeDatabase) -> Any:
    """The sink the way `composition.build_container` builds it: no allowlist override."""
    return audit_repository.PgAuditSink(lambda: _FakeConnection(database))


def _stored_arguments(row: tuple[str, tuple[Any, ...]]) -> dict[str, object]:
    """The jsonb payload out of a captured INSERT, unwrapped from psycopg's Jsonb."""
    for param in row[1]:
        candidate = getattr(param, "obj", param)
        if isinstance(candidate, dict):
            return candidate
    raise AssertionError(f"no jsonb argument payload in the recorded row: {row!r}")


def _shipped_tool_names() -> set[str]:
    """Every tool name this process actually serves, read from the shipped registry.

    `composition.TOOL_PACKAGES` is the list, so a vertical registered there without an
    argument decision fails the first test below instead of shipping a blank trail.

    Imported INSIDE the helper, not at module scope. `composition.py` is the one file
    every other seat in this phase also edits, and a module-level import would turn a
    neighbour's half-finished edit into a collection error for the four tests here that
    never touch it - and a collection error is not a red test, it is a broken one.
    """
    from agent_core.composition import TOOL_PACKAGES

    return {name for build in TOOL_PACKAGES.values() for name in build().tools}


def test_every_tool_this_process_serves_has_a_deliberate_argument_decision() -> None:
    """Each shipped tool opts at least one argument in, by VALUE or by PRESENCE.

    The failure this closes is not a crash: it is a trail that renders "arguments: none
    recorded" for every call ever made, which reads to an operator as "the model passed
    nothing" rather than "nobody decided".
    """
    by_value = audit_repository.ARGUMENT_ALLOWLIST
    by_presence = getattr(audit_repository, "ARGUMENT_PRESENCE_ONLY", {})

    undecided = sorted(
        name
        for name in _shipped_tool_names()
        if name not in by_value and name not in by_presence
    )
    assert not undecided, (
        "these shipped tools record no argument at all, so every call to them renders "
        f"'arguments: none recorded' in the trail: {undecided}"
    )


def test_the_sink_production_builds_stores_what_the_refused_call_was_about() -> None:
    """`pricing_apply` is the call the acceptance run watches get refused.

    Its `order_id` says which order, its `new_price` is the number the approval rule was
    evaluated against. Neither is a secret and neither can be reconstructed from any other
    column, so an operator who cannot see them cannot judge the refusal.
    """
    database = _FakeDatabase()
    arguments: dict[str, object] = {"order_id": "ord-1", "new_price": "12.00"}

    asyncio.run(
        _production_sink(database).record_tool_call(
            _TURN_ID, _caller(), "pricing_apply", arguments, _DECISION
        )
    )

    assert _stored_arguments(database.rows[0]) == {
        "order_id": "ord-1",
        "new_price": "12.00",
    }


def test_a_presence_only_argument_is_recorded_without_its_value() -> None:
    """`case_notes_append` writes free text about a person under investigation.

    An operator needs to know a note was appended and to which case. The note BODY belongs
    in the case file, not duplicated into an append-only trail that outlives it, so the
    field is opted in by presence: the key is there, the text is nowhere on the row.
    """
    database = _FakeDatabase()
    arguments: dict[str, object] = {"case_id": "case-4", "note": _NOTE}

    asyncio.run(
        _production_sink(database).record_tool_call(
            _TURN_ID, _caller(), "case_notes_append", arguments, _DECISION
        )
    )

    stored = _stored_arguments(database.rows[0])
    assert stored["case_id"] == "case-4"
    assert "note" in stored, (
        "the note field vanished entirely, so the trail cannot say a note was appended"
    )
    assert stored["note"] != _NOTE
    assert _NOTE not in repr(database.rows), "the note body leaked onto the row"


def test_an_absent_presence_only_argument_stays_absent() -> None:
    """Presence means presence. A field the model never passed must not be invented."""
    database = _FakeDatabase()
    arguments: dict[str, object] = {"case_id": "case-4"}

    asyncio.run(
        _production_sink(database).record_tool_call(
            _TURN_ID, _caller(), "case_notes_append", arguments, _DECISION
        )
    )

    assert _stored_arguments(database.rows[0]) == {"case_id": "case-4"}


def test_a_tool_nobody_decided_for_says_so_out_loud(
    caplog: Any,
) -> None:
    """A new tool records nothing until somebody decides - and does not do it silently.

    Fail-closed is the right behaviour; fail-closed-and-quiet is the state this anchor
    exists to fix. The row is still written, the arguments are still dropped, and the
    process log names the tool so the gap is findable before an incident asks for it.
    """
    database = _FakeDatabase()
    arguments: dict[str, object] = {"api_key": "hunter2"}  # noqa: S106 - never stored

    with caplog.at_level(logging.WARNING, logger=audit_repository.__name__):
        asyncio.run(
            _production_sink(database).record_tool_call(
                _TURN_ID, _caller(), "a_brand_new_tool", arguments, _DECISION
            )
        )

    assert _stored_arguments(database.rows[0]) == {}
    assert "hunter2" not in repr(database.rows)
    assert any("a_brand_new_tool" in record.message for record in caplog.records), (
        "an unregistered tool dropped every argument and left nothing in the log saying so"
    )
