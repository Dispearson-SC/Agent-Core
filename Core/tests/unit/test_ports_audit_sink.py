"""`AuditSink` is append-only evidence, and the protocol is the thing that says so.

Phase:   F1 - Real hexagonal core, widened in F3
Tasks:   docs/TASKS.md#t-f1-09, docs/TASKS.md#t-f3-14

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
import os
import subprocess
import sys
from pathlib import Path
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


# ---------------------------------------------------------------------------
# t-f3-14 - the refused decision. docs/DECISIONS.md#d25.
#
# D25 landed the four-eyes rule and left a hole its own text names: a rejected
# approval attempt writes NOTHING, because this port had no member for it. The
# check in `DecideApproval` deliberately runs BEFORE `record_human_decision`,
# since filing a refused attempt through that member would read in the trail as
# a refusal the human never made. Silence was chosen over a wrong row - and
# silence is still the one decision most worth auditing leaving no trace.
#
# The lock below is the t-f1-05 regression-lock pattern: the NEGATIVE case is
# the PRE-widening shape - a sink carrying exactly the four original members -
# and it must be REJECTED. A lock that only checked the new shape would accept
# the old one too, and the widening could be quietly undone.
# ---------------------------------------------------------------------------

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"

_FIVE_MEMBER_SINK = """
from __future__ import annotations

from decimal import Decimal

from agent_core.domain.media import MediaRef
from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, ToolCallId, TurnId, Usage
from agent_core.ports.audit_sink import AuditSink


class StubSink:
    async def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        raise NotImplementedError

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None,
    ) -> None:
        raise NotImplementedError

    async def record_rejected_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        reason: str,
    ) -> None:
        raise NotImplementedError

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        raise NotImplementedError

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        raise NotImplementedError


sink: AuditSink = StubSink()
"""

# The exact shape the port carried BEFORE t-f3-14: four members, no way at all to
# record that somebody tried to approve and was refused. This is the one the lock
# has to reject.
_PRE_WIDENING_SINK = _FIVE_MEMBER_SINK.replace(
    """    async def record_rejected_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        reason: str,
    ) -> None:
        raise NotImplementedError

""",
    "",
)


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module against the real port.

    Written outside the repository tree on purpose: a fixture that deliberately fails
    to type-check must never be picked up by the project-wide mypy run.
    """
    module = tmp_path / "snippet.py"
    module.write_text(source, encoding="utf-8")

    env = dict(os.environ)
    env["MYPYPATH"] = str(SRC_DIR)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=str(CORE_DIR),
        env=env,
        check=False,
    )


@pytest.mark.silent
def test_a_refused_decision_has_a_member_of_its_own() -> None:
    """D25's owed member exists, and it is not `record_human_decision` wearing a flag.

    The two must stay separate members. `record_human_decision` means "a human
    decided"; a rejected attempt means "a human was not allowed to decide", and
    collapsing them produces a row that claims a refusal nobody made.
    """
    members = _protocol_members()

    assert "record_rejected_decision" in members, (
        "A four-eyes rejection (docs/DECISIONS.md#d25) writes nothing: AuditSink has "
        "no member for a refused decision attempt. Declared members: "
        f"{sorted(members)}"
    )
    assert "record_human_decision" in members, (
        "The refused member must be ADDED alongside record_human_decision, not "
        "replace it."
    )


@pytest.mark.silent
def test_the_refused_member_records_who_tried_and_why_it_was_refused() -> None:
    """The row has to answer "who tried, against which request, and on what grounds".

    `reason` is not optional. A refusal row with no grounds is indistinguishable
    from a clerical error six months later, which is precisely when it is read.
    """
    assert "record_rejected_decision" in _protocol_members(), (
        "precondition: the member must exist before its shape can be pinned."
    )
    assert _parameters("record_rejected_decision") == [
        "turn_id",
        "tool_call_id",
        "subject_id",
        "reason",
    ]

    hints = _hints("record_rejected_decision")
    assert hints["turn_id"] is TurnId
    assert hints["tool_call_id"] is ToolCallId, (
        "The domain ToolCallId, as in record_human_decision - a bare str lets a "
        "refusal be filed against an id no tool call ever had."
    )
    assert hints["subject_id"] is str
    assert hints["reason"] is str, "A refused attempt without grounds explains nothing."

    assert "approved" not in hints, (
        "A rejected attempt carries no verdict: nothing was decided. An `approved` "
        "flag here is record_human_decision leaking back in."
    )


@pytest.mark.phase("F3")
def test_a_sink_carrying_the_widened_shape_type_checks(tmp_path: Path) -> None:
    result = _type_check(_FIVE_MEMBER_SINK, tmp_path)

    assert result.returncode == 0, (
        "A sink carrying all five settled members must satisfy AuditSink.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F3")
def test_a_sink_that_silently_drops_the_refusal_does_not_type_check(tmp_path: Path) -> None:
    """The negative case is the PRE-widening shape, and it must be rejected.

    This is the half that makes the widening permanent. A lock asserting only that
    the new member exists would go on passing if someone re-narrowed the port,
    because the old four-member sink still satisfies "has the members I named".
    """
    result = _type_check(_PRE_WIDENING_SINK, tmp_path)

    assert result.returncode != 0, (
        "AuditSink accepted a sink with no record_rejected_decision - the "
        "pre-widening shape. A four-eyes rejection would write nothing and "
        "nothing would fail."
    )
    assert "record_rejected_decision" in result.stdout, (
        "Expected the missing refusal member to be the reason mypy refused.\n"
        f"{result.stdout}{result.stderr}"
    )
