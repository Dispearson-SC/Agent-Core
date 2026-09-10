"""`AuditSink` is append-only evidence, and the protocol is the thing that says so.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-09

WHY THIS TEST EXISTS
    CLAUDE.md non-negotiable #6 states the rule: audit rows are appended, never updated.
    That rule is only real if the object handed to a use case has no method capable of
    breaking it - the absence IS the defence, exactly as with the missing knowledge-write
    tool. The day someone adds `update_tool_call(...)` "just to fix a bad row", this fails
    and the conversation happens before the evidence table stops being evidence.

    The second property is the "why was this allowed?" test from the port's own docstring.
    Six months on, the answer has to be reconstructible from these rows, and the piece
    that answers it is the `rule_id` on the `PolicyDecision`. So the port takes the whole
    decision rather than a flattened effect and reason: a call site cannot drop a field it
    never gets to unpack.

    Nothing here drives behaviour. It is a lock on a contract that other waves build
    adapters and hooks against, and every failure it can produce is one that would
    otherwise surface only during an audit.
"""

import inspect
from typing import get_type_hints

import pytest

from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, ToolCallId, TurnId
from agent_core.ports.audit_sink import AuditSink

MUTATING_PREFIXES = ("update_", "delete_", "amend_", "redact_", "purge_", "set_")


def _protocol_members() -> frozenset[str]:
    """The names `AuditSink` itself declares, without object/Protocol noise."""
    declared = getattr(AuditSink, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(name for name in vars(AuditSink) if not name.startswith("_"))


def _hints(name: str) -> dict[str, object]:
    return dict(get_type_hints(getattr(AuditSink, name)))


def _parameters(name: str) -> list[str]:
    signature = inspect.signature(getattr(AuditSink, name))
    return [p for p in signature.parameters if p != "self"]


@pytest.mark.silent
def test_protocol_has_no_mutating_member() -> None:
    """No `update_*`, no `delete_*`. An append-only sink that also offers a way to rewrite
    a row is not append-only; it is a table with a convention nobody enforces."""
    offenders = sorted(name for name in _protocol_members() if name.startswith(MUTATING_PREFIXES))

    assert offenders == [], (
        "AuditSink is append-only (CLAUDE.md non-negotiable #6). "
        f"Remove: {offenders}. Correct a wrong row by appending another one."
    )


@pytest.mark.silent
def test_every_member_records_and_returns_nothing() -> None:
    """Every member is an append: it is named for what it records and hands nothing back.

    A member returning a row is a read path, and a read path on this port is how the
    "just read it back and fix it" version of an update gets built."""
    members = sorted(_protocol_members())

    assert members, "AuditSink declares no members at all"
    assert all(name.startswith("record_") for name in members), members
    for name in members:
        assert _hints(name)["return"] is type(None), f"AuditSink.{name} returns a value"


def test_every_member_is_awaitable() -> None:
    """D13: each one is an insert on its own connection, and `record_tool_call` runs on
    the hot path of every tool call. A blocking write there stalls every other turn."""
    for name in sorted(_protocol_members()):
        assert inspect.iscoroutinefunction(getattr(AuditSink, name)), (
            f"AuditSink.{name} is sync; adapters/ is async end to end (D13)."
        )


@pytest.mark.silent
def test_record_tool_call_takes_the_policy_decision_itself() -> None:
    """The verdict travels whole, so `rule_id` cannot be dropped at the call site.

    Flattening this to `effect` and `reason` would type-check and still lose the only
    field that answers "why was this allowed?" - and nothing would ever fail because of
    it until an auditor asked."""
    assert _parameters("record_tool_call") == [
        "turn_id",
        "caller",
        "tool_name",
        "arguments",
        "decision",
    ]

    hints = _hints("record_tool_call")
    assert hints["turn_id"] is TurnId
    assert hints["caller"] is CallerIdentity
    assert hints["tool_name"] is str
    assert hints["arguments"] == dict[str, object]
    assert hints["decision"] is PolicyDecision

    assert "rule_id" in PolicyDecision.__dataclass_fields__, (
        "The recorded decision is only auditable while it carries its rule_id."
    )


def test_record_human_decision_names_the_domain_tool_call_id() -> None:
    """`ToolCallId` exists in the domain and `PendingRequest` already uses it.

    Taking a bare `str` here lets any string reach the one row a compliance reader asks
    for first, and an approval filed against an id no tool call ever had is indexed,
    queryable and wrong."""
    assert _parameters("record_human_decision") == [
        "turn_id",
        "tool_call_id",
        "subject_id",
        "approved",
        "note",
    ]

    hints = _hints("record_human_decision")
    assert hints["turn_id"] is TurnId
    assert hints["tool_call_id"] is ToolCallId
    assert hints["approved"] is bool
