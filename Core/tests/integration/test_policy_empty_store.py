"""An empty policy table is not a broken database, and the refusal must say so.

Phase:   F11 - a clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-06
Covers:  adapters/driven/persistence_pg/policy_repository.py

SILENT-BUG AREA (CLAUDE.md). Nothing here fails on its own: the VERDICT was already right
and stays right - default DENY, fail closed, frozen by `t-f1-15`. What was wrong is the
EXPLANATION, and an explanation is not decoration in this engine. It has two readers:

    - the MODEL, which receives it as the tool result on a DENY and adapts to it;
    - the HUMAN, who reads it to diagnose why the agent refused.

`decide` used to infer unreachability from emptiness - `_NO_RULE_REASON if rules.rules
else _STORE_DOWN_REASON` - so a perfectly healthy `policy_rules` with no rows in it told
both readers the database was down. That is how an operator ends up checking a database
that is fine, on the exact first run F11 exists to make survivable: a fresh clone, an
empty Postgres, and nobody has inserted a rule yet.

THE DISTINCTION THIS MODULE PINS
    "unreachable" must mean THE QUERY FAILED.
    "no rule matched" must mean THE QUERY SUCCEEDED AND MATCHED NOTHING.
    Both DENY. Both say which, and they must not share a sentence, because two refusals
    that overlap in wording are two refusals an operator cannot tell apart at a glance.

Emptiness cannot carry that distinction, and the tests below are shaped so that no
implementation reading `len(rules.rules)` can pass them: a reachable EMPTY store and a
reachable POPULATED store whose rules simply do not match the tool are the same state, so
they are asserted to produce the SAME sentence - while the unreachable store, whose
snapshot is also empty, is asserted to produce a different one.

WHY THIS LIVES IN tests/integration/
    The fake-connection half needs no infrastructure and always runs; it is the assertion
    that must never lapse quietly. The Postgres half is here because the defect was found
    against a REAL migrated database with a real, empty `policy_rules` table, and a fake
    that returns `[]` is only a claim about that database until one has been asked.

The last test is the mutation guard. Changing what a DENY says must not disturb what a
MATCHED rule says: the audit trail's `rule_id` and the winning rule's own reason travel
together, and a perturbation that keeps every effect correct while renaming the winner
leaves the trail lying with every other test still green.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from types import TracebackType
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TENANT = TenantId("t-policy-empty")
_ROLE = "operator"
_CHANNEL = "http"

# The tool nothing has a rule for, in every scenario below. It is the name an operator
# types on a fresh clone, before a single rule exists.
_UNMATCHED_TOOL = "issue_refund"

# A store that is reachable and answers with rules - none of which match `_UNMATCHED_TOOL`.
# Its DENY is the reference sentence: the store was reachable, the query succeeded, and
# nothing matched. A reachable EMPTY store is the same state and must say the same thing.
_UNRELATED_RULES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "r-browser",
        {
            "tool_pattern": "browser_*",
            "effect": "allow",
            "reason": "read-only browsing is open to operators",
            "subject_roles": [_ROLE],
            "channels": [],
        },
    ),
)


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel=_CHANNEL,
        tenant_id=_TENANT,
        roles=frozenset({_ROLE}),
    )


class _FakeCursor:
    def __init__(self, rows: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._rows)


class _FakeConnection:
    """Enough of a psycopg connection to answer one SELECT. Reachable by construction."""

    def __init__(self, rows: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        return _FakeCursor(self._rows)


def _reachable(rows: tuple[tuple[str, dict[str, Any]], ...]) -> Any:
    return lambda: _FakeConnection(rows)


def _unreachable() -> Any:
    def connect() -> _FakeConnection:
        raise psycopg.OperationalError("connection refused")

    return connect


def _decide(connect: Any, tool_name: str = _UNMATCHED_TOOL) -> PolicyDecision:
    """One turn's worth of policy: load the snapshot once, then decide, exactly as
    `start_turn` does."""
    policy = PgToolPolicy(connect, on_error=lambda _error: None)
    rules = asyncio.run(policy.load_rules(_caller()))
    decision: PolicyDecision = policy.decide(rules, tool_name, {})
    return decision


def _sentences(reason: str) -> set[str]:
    """The reason split into comparable sentences.

    Compared case-insensitively and without trailing punctuation, so "the two refusals do
    not share a sentence" cannot be satisfied by a full stop or a capital letter.
    """
    return {
        piece.strip().rstrip(".").lower()
        for piece in re.split(r"(?<=[.!?])\s+", reason)
        if piece.strip()
    }


@pytest.mark.silent
def test_a_reachable_but_empty_policy_store_does_not_blame_the_database() -> None:
    """The defect itself, in the smallest shape that reproduces it.

    The connection was handed over, the SELECT ran, and it returned no rows. That is an
    empty table - the state of every fresh clone - and it must not be reported as an
    unreachable store. The verdict stays DENY: `t-f1-15` froze that and it is right.
    """
    decision = _decide(_reachable(()))

    assert decision.effect is Effect.DENY, (
        "the verdict is frozen by t-f1-15: no matching rule denies, fail closed"
    )
    assert decision.rule_id is None, "a DENY with no matching rule cannot cite one"
    assert "unreachable" not in decision.reason.lower(), (
        "a reachable, empty `policy_rules` reported the store as unreachable. That "
        "sentence goes to the model as the tool result and to the operator as the "
        "diagnosis, and it sends them both to check a database that is fine.\n"
        f"reason: {decision.reason!r}"
    )


@pytest.mark.silent
def test_an_empty_store_and_an_unmatched_tool_are_the_same_refusal() -> None:
    """Emptiness is not the question the explanation answers.

    A store with no rows and a store whose rules simply do not cover this tool are the
    same state: the query succeeded and matched nothing. They must say the same thing, so
    that no implementation can recover the distinction from `len(rules.rules)` again.
    """
    empty = _decide(_reachable(()))
    populated = _decide(_reachable(_UNRELATED_RULES))

    assert populated.effect is Effect.DENY and populated.rule_id is None
    assert empty.reason == populated.reason, (
        "an empty store and a populated store with no matching rule gave two different "
        "explanations for one state - the query succeeded and nothing matched.\n"
        f"empty:     {empty.reason!r}\npopulated: {populated.reason!r}"
    )
    assert _decide(_reachable(_UNRELATED_RULES), "browser_click").effect is Effect.ALLOW, (
        "the populated store's own rule stopped applying, so the DENY above proves nothing"
    )


@pytest.mark.silent
def test_an_unreachable_store_still_says_the_store_is_unreachable() -> None:
    """The other half of the distinction, and the one that must not be lost while fixing
    the first. A store that genuinely could not be queried denies AND says so - an agent
    refusing every tool because Postgres blipped has to be diagnosable as exactly that."""
    decision = _decide(_unreachable())

    assert decision.effect is Effect.DENY
    assert decision.rule_id is None
    assert "unreachable" in decision.reason.lower(), (
        "a store whose query FAILED must say the store was unreachable; without it the "
        "fail-closed snapshot is indistinguishable from a table nobody has filled in.\n"
        f"reason: {decision.reason!r}"
    )


@pytest.mark.silent
def test_the_two_refusals_do_not_share_a_sentence() -> None:
    """Two diagnoses that overlap in wording are two diagnoses an operator reads as one.

    Both deny. Both must say WHICH, in words the other does not use.
    """
    reachable = _decide(_reachable(()))
    unreachable = _decide(_unreachable())

    assert reachable.reason != unreachable.reason
    shared = _sentences(reachable.reason) & _sentences(unreachable.reason)
    assert not shared, (
        "the empty-store refusal and the unreachable-store refusal share a sentence, so "
        f"neither identifies its own state: {sorted(shared)}"
    )


@pytest.mark.silent
def test_a_matching_rule_is_still_named_by_the_decision_it_won() -> None:
    """The mutation guard. Changing what a DENY *says* must not disturb what a MATCH says.

    `rule_id` is the audit trail's only answer to "why was this refused?", and the reason
    the model receives must be the winning rule's own, not either default sentence. A
    perturbation that keeps every effect correct while citing the wrong rule leaves the
    trail lying with nothing red.
    """
    refusal = (
        "r-refund",
        {
            "tool_pattern": _UNMATCHED_TOOL,
            "effect": "deny",
            "reason": "Refunds are frozen for the duration of the audit.",
            "subject_roles": [],
            "channels": [],
        },
    )
    decision = _decide(_reachable((*_UNRELATED_RULES, refusal)))

    assert decision.effect is Effect.DENY
    assert decision.rule_id == "r-refund", (
        "the decision cites a rule that did not decide it; the audit row then names the "
        "wrong rule while every effect stays correct"
    )
    assert decision.reason == refusal[1]["reason"], (
        "a matched rule's DENY was explained by a default sentence instead of by the rule "
        "that actually refused"
    )


# --------------------------------------------------------------------------------------
# THE SAME QUESTION, ASKED OF A REAL, MIGRATED, EMPTY DATABASE
#
# The fakes above are a claim about what Postgres does with an empty table. This is the
# claim being checked: the schema exists, `policy_rules` has no rows, and the adapter is
# holding a genuine connection. It is exactly the state of the fresh clone in F11's done
# criterion, and it is the state in which the false diagnosis was found by hand.
# --------------------------------------------------------------------------------------


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


@pytest.fixture(scope="module")
def app_conninfo() -> Iterator[str]:
    if not _postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    app_db = "agent_core_policy_empty_test"
    dbos_db = "agent_core_policy_empty_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    conninfo = _app_conninfo(app_db)
    # `apply_all_migrations`, not `run_migrations`: the latter applies only F1's
    # `APP_MIGRATIONS`, and the tenant predicate this adapter emits reads the `tenant_id`
    # COLUMN that sibling migration 0012 adds. A database missing it answers every SELECT
    # with `UndefinedColumn` - which is a genuinely failed query, so the adapter would
    # correctly report the store as unreachable and this module would be asserting against
    # the wrong state entirely.
    asyncio.run(migrations.apply_all_migrations(conninfo))
    yield conninfo


@pytest.mark.silent
def test_a_real_empty_policy_rules_table_denies_without_blaming_the_database(
    app_conninfo: str,
) -> None:
    """A migrated database whose `policy_rules` is empty is a healthy database."""
    with psycopg.connect(app_conninfo) as connection:
        connection.execute("DELETE FROM policy_rules")
        connection.commit()

    decision = _decide(lambda: psycopg.connect(app_conninfo))

    assert decision.effect is Effect.DENY
    assert decision.rule_id is None
    assert "unreachable" not in decision.reason.lower(), (
        "a migrated database with an empty `policy_rules` was reported as unreachable; "
        "this is the state of every fresh clone in F11's done criterion.\n"
        f"reason: {decision.reason!r}"
    )
    assert decision.reason == _decide(_reachable(())).reason, (
        "the real empty table and the faked empty result explain themselves differently, "
        "so one of the two is not the state it claims to be"
    )


@pytest.mark.silent
def test_a_seeded_rule_in_the_real_table_still_decides_and_is_named(
    app_conninfo: str,
) -> None:
    """The pair assertion. An adapter that read nothing at all would satisfy every
    empty-table assertion above on its own, so the same table must still be able to
    refuse by NAME once a row exists."""
    rule_id = "r-policy-empty-refund"
    definition = {
        "tool_pattern": _UNMATCHED_TOOL,
        "effect": Effect.DENY.value,
        "reason": "Refunds are frozen for the duration of the audit.",
        "subject_roles": [_ROLE],
        "channels": [_CHANNEL],
        "tenant_id": str(_TENANT),
    }
    with psycopg.connect(app_conninfo) as connection:
        connection.execute("DELETE FROM policy_rules")
        connection.execute(
            "INSERT INTO policy_rules (rule_id, definition) VALUES (%s, %s)",
            (rule_id, Jsonb(definition)),
        )
        connection.commit()

    try:
        decision = _decide(lambda: psycopg.connect(app_conninfo))

        assert decision.effect is Effect.DENY
        assert decision.rule_id == rule_id, (
            "the seeded rule decided nothing, so the empty-table assertions above were "
            "satisfied by an adapter that cannot read this table at all"
        )
        assert decision.reason == definition["reason"]
    finally:
        with psycopg.connect(app_conninfo) as connection:
            connection.execute("DELETE FROM policy_rules WHERE rule_id = %s", (rule_id,))
            connection.commit()
