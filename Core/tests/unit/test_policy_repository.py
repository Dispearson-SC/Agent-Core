"""Unit tests for the Postgres `ToolPolicy`.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-15
Covers:  adapters/driven/persistence_pg/policy_repository.py

SILENT-BUG AREA (CLAUDE.md). A policy hole never fails a test; it just never fires. The
two defaults below are the ones that must not drift, so they are asserted directly:

    - no matching rule    -> DENY. An unknown tool name is a typo or a tool somebody
      registered without registering a rule. ALLOW-by-default makes every newly imported
      tool world-usable, silently.
    - store unreachable   -> DENY, and the error is SURFACED. An agent running
      unrestricted because Postgres blipped is worse than an agent that stops.

Effect precedence is asserted here too, at the end of the module. `decide` reduces
independently of `tests.fakes.ports.FakeToolPolicy`, so the reduction pinned in
tests/unit/test_policy.py says nothing about the object that actually runs inside
`before_tool_execute`.

No Postgres is required here and none may be: the connection source is faked, exactly as
`ToolPolicy` intends - `load_rules` is the only method that touches I/O, so an unreachable
store is reproducible with a factory that raises. There is therefore no skipped
database-backed case in this module.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import permutations
from types import TracebackType
from typing import Any

import psycopg
import pytest

import agent_core.adapters.driven.persistence_pg.policy_repository as policy_repository
from agent_core.domain.policy import Effect
from agent_core.domain.turn import CallerIdentity, TenantId

# A store that knows about the browser toolset and about nothing else. Every "unknown
# tool" below is unknown against these rows, not against an empty database - a DENY from
# an empty store would prove far less.
_ROWS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "r-browser",
        {
            "tool_pattern": "browser_*",
            "effect": "allow",
            "reason": "read-only browsing is open to operators",
            "subject_roles": ["operator"],
            "channels": [],
        },
    ),
    (
        "r-refund",
        {
            "tool_pattern": "issue_refund",
            "effect": "needs_approval",
            "reason": "a human signs off on money leaving",
            "subject_roles": [],
            "channels": [],
        },
    ),
)


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _policy_class() -> Any:
    """The adapter class, asserted rather than imported at module level.

    An `ImportError` at collection time is not a red test - it is a broken one - so the
    absence of the implementation has to surface here, as a failed assertion inside a
    test that actually ran.
    """
    policy = getattr(policy_repository, "PgToolPolicy", None)
    assert policy is not None, (
        "adapters/driven/persistence_pg/policy_repository.py exposes no PgToolPolicy; "
        "docs/TASKS.md#t-f1-15"
    )
    return policy


class _FakeCursor:
    def __init__(self, rows: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._rows)


class _FakeConnection:
    """Just enough of a psycopg connection to answer one SELECT."""

    def __init__(self, rows: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        self._rows = rows
        self.statements: list[str] = []

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        self.statements.append(sql)
        return _FakeCursor(self._rows)


def _reachable_store(rows: tuple[tuple[str, dict[str, Any]], ...]) -> Any:
    return lambda: _FakeConnection(rows)


def _unreachable_store(error: BaseException) -> Any:
    def connect() -> _FakeConnection:
        raise error

    return connect


def test_an_unknown_tool_resolves_to_deny() -> None:
    """The first default that must not drift.

    The store answered, the rules loaded, and the tool simply matches none of them. That
    is the common case for a typo or an unregistered tool, and it must refuse.
    """
    policy = _policy_class()(_reachable_store(_ROWS))

    rules = asyncio.run(policy.load_rules(_caller()))
    decision = policy.decide(rules, "definitely_not_a_registered_tool", {})

    assert decision.effect is Effect.DENY
    assert decision.rule_id is None, "a DENY with no matching rule cannot cite one"
    assert policy.decide(rules, "browser_click", {}).effect is Effect.ALLOW, (
        "the store's own rules stopped applying; the DENY above would then prove nothing"
    )


def test_an_unknown_tool_is_dropped_from_the_advertised_toolset() -> None:
    """`filter_toolset` must agree with `decide`: a name `decide` always denies is a name
    the model must never be offered."""
    policy = _policy_class()(_reachable_store(_ROWS))

    rules = asyncio.run(policy.load_rules(_caller()))
    kept = policy.filter_toolset(rules, ("browser_click", "issue_refund", "run_shell"))

    assert kept == ("browser_click", "issue_refund"), (
        "NEEDS_APPROVAL tools stay advertised; only the unmatched name is dropped"
    )


def test_an_unreachable_store_fails_closed_and_surfaces_the_error() -> None:
    """The second default that must not drift.

    The snapshot comes back empty with `default_effect=DENY`, it still remembers the
    caller it was narrowed for, nothing is raised at the call site - and the failure is
    surfaced rather than swallowed.
    """
    surfaced: list[BaseException] = []
    down = psycopg.OperationalError("connection refused")
    policy = _policy_class()(_unreachable_store(down), on_error=surfaced.append)

    caller = _caller()
    rules = asyncio.run(policy.load_rules(caller))

    assert rules.rules == ()
    assert rules.default_effect is Effect.DENY
    assert rules.subject_roles == caller.roles
    assert rules.channel == caller.channel
    assert surfaced == [down], "the store failure was swallowed instead of surfaced"


def test_the_fail_closed_snapshot_denies_every_tool() -> None:
    """The empty snapshot is not merely empty: it must refuse, including for a tool the
    reachable store would have allowed."""
    policy = _policy_class()(
        _unreachable_store(psycopg.OperationalError("connection refused")),
        on_error=lambda _error: None,
    )

    rules = asyncio.run(policy.load_rules(_caller()))

    assert policy.decide(rules, "browser_click", {}).effect is Effect.DENY
    assert policy.decide(rules, "issue_refund", {}).effect is Effect.DENY
    assert policy.filter_toolset(rules, ("browser_click", "issue_refund")) == ()


def test_any_store_failure_fails_closed_not_just_a_connection_error() -> None:
    """Fail closed on the whole class of store failures. A driver that raises something
    other than `OperationalError` - a decode error, a pool timeout, a programming error
    against a drifted schema - must not become an unrestricted agent."""
    surfaced: list[BaseException] = []
    broken = RuntimeError("the driver raised something nobody enumerated")
    policy = _policy_class()(_unreachable_store(broken), on_error=surfaced.append)

    rules = asyncio.run(policy.load_rules(_caller()))

    assert rules.rules == ()
    assert rules.default_effect is Effect.DENY
    assert surfaced == [broken]


def test_the_default_error_channel_logs_when_no_hook_is_given(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`on_error` is optional; the failure is still surfaced without it. Silence here
    would mean a fail-closed agent with nothing to explain why."""
    policy = _policy_class()(_unreachable_store(psycopg.OperationalError("down")))

    with caplog.at_level(logging.ERROR):
        rules = asyncio.run(policy.load_rules(_caller()))

    assert rules.default_effect is Effect.DENY
    assert caplog.records, "the store failure was neither raised, logged nor handed to a hook"


# --------------------------------------------------------------------------------------
# EFFECT PRECEDENCE, AGAINST THE REAL REDUCER
#
# tests/unit/test_policy.py::test_deny_beats_needs_approval_beats_allow pins the same
# property, but it reduces through `tests.fakes.ports.FakeToolPolicy`. `PgToolPolicy
# .decide` reduces independently - it takes a `min` over `EFFECT_PRECEDENCE.index` - so
# the fake being right proves nothing about the object that actually runs inside
# `before_tool_execute`. Two reducers with one test between them is how the enforcement
# point drifts away from the fixture the rest of the suite trusts, and a policy hole never
# fails a test on its own.
#
# The tests below are deliberately shaped as siblings of that one: the same three rules,
# the same permutation sweep, the same both-directions specificity check, the same
# insistence that the cited `rule_id` is the rule that actually decided. The difference is
# the path in - these go through `load_rules`, so the rows travel the adapter's own
# parsing, caller narrowing and snapshot construction before anything is reduced.
#
# EFFECT_PRECEDENCE's literal contents are NOT re-asserted here on purpose. test_policy.py
# owns that constant; restating it would let a perturbation of the tuple fail this module
# on the constant rather than on the reduction, which is the half that is untested.
# --------------------------------------------------------------------------------------

_TREASURER = CallerIdentity(
    subject_id="u-treasurer",
    channel="http",
    tenant_id=TenantId("t-1"),
    roles=frozenset({"treasurer"}),
)


def _row(rule_id: str, tool_pattern: str, effect: str, reason: str) -> tuple[str, dict[str, Any]]:
    """One `policy_rules` row in the shape migration 0004 actually stores."""
    return (
        rule_id,
        {
            "tool_pattern": tool_pattern,
            "effect": effect,
            "reason": reason,
            "subject_roles": [],
            "channels": [],
        },
    )


def _decide_over(
    rows: tuple[tuple[str, dict[str, Any]], ...],
    tool_name: str,
) -> Any:
    """Load `rows` through the real adapter and decide, exactly as a turn would."""
    policy = _policy_class()(_reachable_store(rows))
    rules = asyncio.run(policy.load_rules(_TREASURER))
    return rules, policy.decide(rules, tool_name, {})


# The broad rule is the DENY, so specificity and precedence point in opposite directions:
# a reducer that quietly preferred the exact match would return ALLOW here.
_DENY_WIDE = _row(
    "r-deny",
    "transfer_*",
    "deny",
    "Wire transfers are frozen for the duration of the audit.",
)
_APPROVE_EXACT = _row(
    "r-approve",
    "transfer_funds",
    "needs_approval",
    "A human signs off on money leaving.",
)
_ALLOW_EXACT = _row(
    "r-allow",
    "transfer_funds",
    "allow",
    "Treasury operations are part of the job.",
)


@pytest.mark.silent
def test_the_adapter_reduces_deny_over_needs_approval_over_allow() -> None:
    """DENY wins over NEEDS_APPROVAL wins over ALLOW, in `PgToolPolicy.decide` itself.

    Every permutation of the same three rows is asserted, because row order is whatever
    Postgres felt like returning: `_SELECT_RULES` carries no ORDER BY, so a reducer that
    depended on position would pass or fail by luck of the plan, per deployment, and only
    ever in production.
    """
    for ordering in permutations((_DENY_WIDE, _APPROVE_EXACT, _ALLOW_EXACT)):
        rules, decision = _decide_over(ordering, "transfer_funds")

        assert {rule.rule_id for rule in rules.applicable("transfer_funds")} == {
            "r-deny",
            "r-approve",
            "r-allow",
        }, "a match was dropped in load_rules, so the reduction below saw a different set"

        assert decision.effect is Effect.DENY, (
            f"row order {[row[0] for row in ordering]} changed the verdict"
        )
        assert decision.rule_id == "r-deny", (
            "the verdict must cite the rule that actually refused. An effect without the "
            "right rule_id makes the audit trail lie, and the audit trail is the only "
            "record of why a tool was refused."
        )
        assert decision.reason == _DENY_WIDE[1]["reason"], (
            "the reason travels with the winning rule; a mismatched pair tells the model "
            "and the auditor two different stories"
        )


@pytest.mark.silent
def test_the_adapter_lets_needs_approval_outrank_allow() -> None:
    """With no DENY in play, the middle value must still win.

    NEEDS_APPROVAL is the state a boolean engine cannot hold, and losing it does not
    surface as a refusal - it turns an ask into a silent yes.
    """
    for ordering in permutations((_APPROVE_EXACT, _ALLOW_EXACT)):
        _, decision = _decide_over(ordering, "transfer_funds")

        assert decision.effect is Effect.NEEDS_APPROVAL
        assert decision.rule_id == "r-approve"
        assert decision.blocks_execution is True, (
            "NEEDS_APPROVAL is not ALLOW: it must stop the call until a human answers"
        )


@pytest.mark.silent
def test_specificity_never_tie_breaks_in_the_adapter_in_either_direction() -> None:
    """Neither an exact match nor a wildcard buys a rule any rank.

    Asserting only one arrangement would leave "deny wins" holding just when the DENY
    happens to be the broad rule - or just when it happens to be the narrow one. Both
    arrangements are asserted so the property cannot be satisfied by a specificity
    tie-break that merely lines up with precedence in the case that was tested.
    """
    exact_deny = _row("r-deny-exact", "transfer_funds", "deny", "This particular tool is frozen.")
    wildcard_allow = _row(
        "r-allow-wide",
        "transfer_*",
        "allow",
        "Treasury tooling is open to the treasury.",
    )
    for ordering in permutations((exact_deny, wildcard_allow)):
        _, decision = _decide_over(ordering, "transfer_funds")
        assert decision.effect is Effect.DENY
        assert decision.rule_id == "r-deny-exact"

    # And the mirror image: the DENY is now the wildcard and the ALLOW is exact.
    for ordering in permutations((_DENY_WIDE, _ALLOW_EXACT)):
        _, decision = _decide_over(ordering, "transfer_funds")
        assert decision.effect is Effect.DENY
        assert decision.rule_id == "r-deny"


@pytest.mark.silent
def test_filter_toolset_agrees_with_the_reduced_verdict() -> None:
    """`filter_toolset` is defined as `decide`, so a rule set that reduces to DENY must
    drop the name rather than advertise a tool the model will only be refused."""
    policy = _policy_class()(_reachable_store((_DENY_WIDE, _APPROVE_EXACT, _ALLOW_EXACT)))
    rules = asyncio.run(policy.load_rules(_TREASURER))

    assert policy.filter_toolset(rules, ("transfer_funds",)) == (), (
        "a name that decide() denies was still offered to the model"
    )
