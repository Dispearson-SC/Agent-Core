"""Integration tests for the tenant dimension of the Postgres `ToolPolicy`.

Phase:   D2 - Multi-tenancy
Tasks:   docs/TASKS.md#t-d2-03
Covers:  adapters/driven/persistence_pg/policy_repository.py
         adapters/driven/persistence_pg/policy_tenant_migration.py

SILENT-BUG AREA (CLAUDE.md). A missing tenant predicate returns MORE rows, not fewer, so
nothing fails: the agent simply becomes allowed to do more. There is no exception, no
error log and no red test. That is why this module asserts three separate things rather
than one.

    1. THE PREDICATE IS IN THE SQL, NOT IN PYTHON. The statement the adapter hands to
       psycopg must narrow by the tenant column, with the caller's tenant among the
       parameters. Rows cannot tell a narrowed query apart from a query narrowed
       afterwards in Python: both return the same list on a good day, and only one of them
       is still correct the day somebody adds a second code path. The structural half
       needs no infrastructure and always runs, because it is the assertion that must
       never be allowed to lapse quietly.

    2. TENANT B'S GRANT NEVER REACHES TENANT A. Asserted through `decide`, not through the
       row list, because the row list is not what authorises a tool. The seeded rules are
       identical in every other respect - same shape, same effect, same roles, same
       channels - so nothing but the tenant can be doing the work.

       It is asserted as a PAIR, the way `test_knowledge_repository.py` learned to: an
       adapter that returns nothing at all satisfies "B never leaks" on its own. So the
       platform-wide rule must still reach A, and B's own rule must still reach B.

    3. THE SNAPSHOT AND ITS RULES ARE STAMPED WITH THE TENANT THEY WERE LOADED FOR.
       `t-d2-06` gave `RuleSet` and `PolicyRule` a tenant, and `RuleSet.applicable`
       re-checks it so the domain survives a leaky query. That defence is INERT while the
       adapter builds every rule with `tenant_id=None`: a predicate and a domain check
       both reading a field nothing writes is worse than neither, because the shape looks
       safe. So a tenant-scoped row must come back carrying its tenant.

WHAT AN EXISTING ROW MEANS - THE MIGRATION DECIDES NOTHING, IT PRESERVES
    Migration 0012 adds a NULLABLE `tenant_id` column, and NULL means ALL TENANTS, the
    reading `domain/policy.py` already uses. That looks like a privilege widening - every
    stored rule becoming platform-wide in one ALTER - and it is not, because those rows
    are ALREADY platform-wide today: nothing has ever written a tenant into `definition`,
    and both the pre-D2 predicate and `_to_rule` read a row without one as reaching every
    tenant. The column records what the rows already are; it does not grant them anything.

    The alternative - a NOT NULL column, so a row that names no tenant matches no caller -
    is fail-closed in the abstract and an outage in practice: no match already means DENY,
    so it would refuse every tool for every tenant on the next turn after the migration.
    That is the assertion at the bottom of this module, and it is written down as a test
    rather than a comment because it is the kind of decision a later migration re-opens.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
from collections.abc import Iterator, Sequence
from types import ModuleType, TracebackType
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import agent_core.adapters.driven.persistence_pg.policy_repository as policy_repository
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.domain.policy import Effect
from agent_core.domain.turn import CallerIdentity, TenantId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TENANT_A = TenantId("t-policy-a")
_TENANT_B = TenantId("t-policy-b")

# A rule with no tenant at all, stored the pre-D2 way: every field inside `definition`
# jsonb and no column value. This is the "existing row" the migration must not change the
# reach of.
_PLATFORM_RULE = "r-policy-tenant-platform"
_PLATFORM_TOOL = "browser_click"

# Tenant A's own rule. Its job is the stamping assertion: it must come back carrying A.
_TENANT_A_RULE = "r-policy-tenant-a"
_TENANT_A_TOOL = "run_shell"

# Tenant B's grant. Tenant A has NO rule for this tool, so a leak is unambiguous: A would
# go from DENY-with-no-rule to ALLOW citing a rule id that belongs to another business.
_TENANT_B_RULE = "r-policy-tenant-b"
_TENANT_B_TOOL = "issue_refund"

_SHARED_ROLE = "operator"
_SHARED_CHANNEL = "http"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


def _caller(tenant_id: TenantId) -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel=_SHARED_CHANNEL,
        tenant_id=tenant_id,
        roles=frozenset({_SHARED_ROLE}),
    )


class _RecordingConnection:
    """A psycopg connection that keeps every statement and parameter set it is handed.

    It exists so the tenant assertion can be made about the SQL rather than about the
    rows, for the reason in point 1 of the module docstring.
    """

    def __init__(self, conninfo: str, log: list[tuple[str, Any]]) -> None:
        self._conninfo = conninfo
        self._log = log
        self._connection: psycopg.Connection[Any] | None = None

    def __enter__(self) -> _RecordingConnection:
        connection = psycopg.connect(self._conninfo)
        connection.__enter__()
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.__exit__(exc_type, exc, traceback)

    def execute(self, sql: str, params: Any = None) -> Any:
        self._log.append((sql, params))
        assert self._connection is not None
        return self._connection.execute(sql, params)


def _recording_factory(conninfo: str, log: list[tuple[str, Any]]) -> Any:
    def connect() -> _RecordingConnection:
        return _RecordingConnection(conninfo, log)

    return connect


def _tenant_migration_module() -> ModuleType:
    """The migration module, looked up rather than imported at module level.

    A `ModuleNotFoundError` at collection time is a broken test, not a red one, so the
    absence of the implementation has to surface as a failed assertion inside a test that
    actually ran.
    """
    name = "agent_core.adapters.driven.persistence_pg.policy_tenant_migration"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        module: ModuleType | None = None
    assert module is not None, (
        f"{name} does not exist; migration 0012 is pre-allocated to docs/TASKS.md#t-d2-03 "
        "and must define its own Migration in its own module, never in migrations.py"
    )
    return module


def _sql_constants() -> list[str]:
    """Every module-level SQL string the policy adapter can hand to Postgres."""
    return [
        value
        for name, value in vars(policy_repository).items()
        if isinstance(value, str) and not name.startswith("__") and "policy_rules" in value
    ]


def _statements_touching_policy(log: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [(sql, params) for sql, params in log if "policy_rules" in sql]


def test_the_migration_id_is_the_one_pre_allocated_to_this_anchor() -> None:
    """No infrastructure needed. An id spent twice is a migration that silently never
    runs against a database that already recorded the first one - docs/TASKS.md."""
    module = _tenant_migration_module()
    migration = getattr(module, "POLICY_TENANT_MIGRATION", None)

    assert migration is not None, (
        "policy_tenant_migration.py exposes no POLICY_TENANT_MIGRATION; the object has to "
        "exist here and not in migrations.py, so migrations.py is never a shared write"
    )
    assert migration.id.startswith("0012"), (
        f"migration id {migration.id!r} is not the 0012 pre-allocated to t-d2-03 in "
        "docs/TASKS.md; ids are spent when written down, never recycled"
    )
    assert "policy_rules" in migration.sql and "tenant_id" in migration.sql, (
        "migration 0012 is the tenant column on policy_rules and its SQL names neither"
    )


def test_every_statement_the_policy_adapter_can_emit_narrows_by_the_tenant_column() -> None:
    """No infrastructure needed, and that is the point: this must never lapse quietly.

    A read against `policy_rules` that does not narrow by tenant hands one business's
    grants to another, and it fails no other test - the query returns MORE rows, and more
    rows look like a more capable agent.
    """
    constants = _sql_constants()

    assert constants, (
        "the policy adapter exposes no SQL constant against `policy_rules`; the tenant "
        "predicate cannot be asserted against SQL that does not exist"
    )
    for sql in constants:
        where = sql.upper().partition("WHERE")[2]
        assert where, f"a statement against `policy_rules` carries no WHERE clause:\n{sql}"
        assert "TENANT_ID" in where and "%S" in where, (
            "a statement against `policy_rules` does not narrow by a parameterised "
            f"tenant predicate:\n{sql}\n"
            "Filtering by tenant after the rows are fetched leaks every other tenant's "
            "grants into the snapshot the agent is authorised from."
        )
        # The jsonb key was the pre-0012 carrier. After migration 0012 the tenant is a
        # COLUMN, and a predicate that reads only the jsonb key ignores every value the
        # migration wrote there - so the column has to appear in the WHERE clause in its
        # own right, not merely as the text inside `definition->>'tenant_id'`.
        assert "TENANT_ID" in where.replace("DEFINITION->>'TENANT_ID'", ""), (
            "the tenant predicate reads only the pre-0012 jsonb key and never the "
            f"`tenant_id` column migration 0012 adds:\n{sql}"
        )


@pytest.fixture(scope="module")
def app_conninfo() -> Iterator[str]:
    if not _postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    app_db = "agent_core_policy_tenant_test"
    dbos_db = "agent_core_policy_tenant_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(conninfo))
    yield conninfo


def _definition(tool_pattern: str, effect: Effect, reason: str) -> dict[str, Any]:
    """Identical in every field but the tool pattern and the effect, so nothing except the
    tenant can explain a difference in what comes back."""
    return {
        "tool_pattern": tool_pattern,
        "effect": effect.value,
        "reason": reason,
        "subject_roles": [_SHARED_ROLE],
        "channels": [_SHARED_CHANNEL],
    }


def _seed(conninfo: str) -> None:
    """One pre-D2 row and two tenant-scoped ones.

    The platform row is inserted the OLD way - columns `rule_id` and `definition` only -
    because that is exactly the row shape the migration inherits, and its reach after the
    migration is the decision this module pins.
    """
    module = _tenant_migration_module()
    apply_migration = getattr(module, "apply_policy_tenant_migration", None)
    assert apply_migration is not None, (
        "policy_tenant_migration.py exposes no apply_policy_tenant_migration; migration "
        "0012 has to be appliable after migrations.run_migrations, the same precedent as "
        "conversation_repository.PROFILE_SNAPSHOT_MIGRATION"
    )

    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM policy_rules WHERE rule_id IN (%s, %s, %s)",
            (_PLATFORM_RULE, _TENANT_A_RULE, _TENANT_B_RULE),
        )
        connection.execute(
            "INSERT INTO policy_rules (rule_id, definition) VALUES (%s, %s)",
            (
                _PLATFORM_RULE,
                Jsonb(
                    _definition(
                        _PLATFORM_TOOL,
                        Effect.ALLOW,
                        "read-only browsing is open to operators everywhere",
                    )
                ),
            ),
        )

    asyncio.run(apply_migration(conninfo))

    with psycopg.connect(conninfo, autocommit=True) as connection:
        for rule_id, tenant_id, tool, effect, reason in (
            (
                _TENANT_A_RULE,
                _TENANT_A,
                _TENANT_A_TOOL,
                Effect.NEEDS_APPROVAL,
                "a human signs off before a shell runs",
            ),
            (
                _TENANT_B_RULE,
                _TENANT_B,
                _TENANT_B_TOOL,
                Effect.ALLOW,
                "this business lets its operators refund without asking",
            ),
        ):
            connection.execute(
                "INSERT INTO policy_rules (rule_id, tenant_id, definition) VALUES (%s, %s, %s)",
                (rule_id, str(tenant_id), Jsonb(_definition(tool, effect, reason))),
            )


@pytest.fixture
def seeded(app_conninfo: str) -> Iterator[str]:
    _seed(app_conninfo)
    yield app_conninfo


def test_tenant_b_grant_never_authorises_tenant_a_while_b_still_holds_it(seeded: str) -> None:
    """The breach, and the proof that the assertion is not vacuous.

    `_TENANT_B_TOOL` is granted to B and to nobody else. For A the verdict must be DENY
    with no rule id at all - a DENY that cited another tenant's rule would be the same
    leak wearing a different effect.
    """
    log: list[tuple[str, Any]] = []
    policy = PgToolPolicy(_recording_factory(seeded, log))

    for_a = asyncio.run(policy.load_rules(_caller(_TENANT_A)))
    for_b = asyncio.run(policy.load_rules(_caller(_TENANT_B)))

    decision_a = policy.decide(for_a, _TENANT_B_TOOL, {})
    assert decision_a.effect is Effect.DENY, (
        f"tenant A was granted {_TENANT_B_TOOL!r} by rule {decision_a.rule_id!r}, which "
        "belongs to another business"
    )
    assert decision_a.rule_id is None, (
        f"tenant A's verdict cites rule {decision_a.rule_id!r}; a rule from another tenant "
        "reached the snapshot the agent is authorised from"
    )
    assert _TENANT_B_RULE not in {rule.rule_id for rule in for_a.rules}, (
        "tenant B's rule is in tenant A's snapshot; the domain re-check happens to be "
        "catching it, but the row must never have been transferred at all"
    )

    assert policy.decide(for_b, _TENANT_B_TOOL, {}).effect is Effect.ALLOW, (
        "tenant B cannot use its own rule either, so the DENY above proves nothing"
    )
    assert policy.decide(for_b, _TENANT_B_TOOL, {}).rule_id == _TENANT_B_RULE


def test_the_snapshot_and_its_rules_are_stamped_with_the_tenant_they_were_loaded_for(
    seeded: str,
) -> None:
    """The inert-field defect. A rule loaded without its tenant reads as platform-wide,
    and `RuleSet.applicable`'s re-check then has nothing to re-check."""
    policy = PgToolPolicy(_recording_factory(seeded, []))

    for_a = asyncio.run(policy.load_rules(_caller(_TENANT_A)))

    assert for_a.tenant_id == _TENANT_A, (
        "the snapshot does not record the tenant it was loaded for; a RuleSet that is "
        "structurally able to answer about another tenant is the hole t-d2-06 closed"
    )

    stamped = {rule.rule_id: rule.tenant_id for rule in for_a.rules}
    assert stamped.get(_TENANT_A_RULE) == _TENANT_A, (
        f"the stored rule {_TENANT_A_RULE!r} came back with tenant_id "
        f"{stamped.get(_TENANT_A_RULE)!r}; a tenant-scoped rule read as platform-wide "
        "makes the domain narrowing inert"
    )
    assert policy.decide(for_a, _TENANT_A_TOOL, {}).rule_id == _TENANT_A_RULE, (
        "tenant A's own rule stopped applying to tenant A"
    )


def test_the_emitted_statement_carries_the_tenant_predicate_and_the_callers_tenant(
    seeded: str,
) -> None:
    """The structural assertion again, this time against what actually reached psycopg -
    a module constant proves nothing if the adapter builds its statement somewhere else."""
    log: list[tuple[str, Any]] = []
    policy = PgToolPolicy(_recording_factory(seeded, log))

    asyncio.run(policy.load_rules(_caller(_TENANT_A)))

    statements = _statements_touching_policy(log)
    assert statements, "the adapter issued no statement against policy_rules"
    for sql, params in statements:
        where = sql.upper().partition("WHERE")[2]
        assert "TENANT_ID" in where, f"no tenant predicate in the emitted statement:\n{sql}"
        assert params is not None and str(_TENANT_A) in tuple(params), (
            f"the caller's tenant is not among the parameters of:\n{sql}\nparams={params!r}"
        )


def test_a_row_that_predates_the_migration_keeps_the_reach_it_already_had(seeded: str) -> None:
    """The migration preserves, it does not decide.

    A pre-D2 row names no tenant and is already read as reaching every tenant. NULL keeps
    that; a NOT NULL column would have made it reach nobody, and since no match already
    means DENY, that is every tool refused for every tenant on the next turn.
    """
    policy = PgToolPolicy(_recording_factory(seeded, []))

    for tenant in (_TENANT_A, _TENANT_B):
        rules = asyncio.run(policy.load_rules(_caller(tenant)))
        decision = policy.decide(rules, _PLATFORM_TOOL, {})
        assert decision.effect is Effect.ALLOW and decision.rule_id == _PLATFORM_RULE, (
            f"the pre-migration platform-wide rule stopped reaching {tenant}; migration "
            "0012 changed the reach of a row instead of recording it"
        )

        platform = {rule.rule_id: rule.tenant_id for rule in rules.rules}
        assert platform.get(_PLATFORM_RULE) is None, (
            "the platform-wide rule came back stamped with a tenant; NULL means ALL "
            "TENANTS in domain/policy.py, and inventing one here narrows it silently"
        )
