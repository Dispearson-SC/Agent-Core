"""Driven adapter: ToolPolicy over Postgres.

Phase:   F1 / D2 (tenant dimension)
Tasks:   docs/TASKS.md#t-f1-15, docs/TASKS.md#t-d2-03
Status:  DONE (t-f1-15) / TENANT PREDICATE AND MIGRATION 0012 LANDED (t-d2-03)
Implements: ports/tool_policy.py

SILENT-BUG AREA. A hole here never fails a test.

TABLE
    policy_rules (rule_id pk, tenant_id null, tool_pattern, effect,
                  subject_roles text[], channels text[], reason)

    Add `tenant_id` NOW even though it is unused until D2. NULL means "all tenants".
    Altering a table that is already the authority on permissions is a migration nobody
    enjoys.

decide() PSEUDO-CODE
    1. Load the caller's applicable rules ONCE PER TURN and cache. Never query per tool
       name - this is on the hot path of every turn.
    2. Match, then reduce by EFFECT_PRECEDENCE: DENY > NEEDS_APPROVAL > ALLOW.
    3. No match -> DENY.

TWO DEFAULTS THAT MUST NOT DRIFT
    - No matching rule -> DENY. An unknown tool is a typo or an unregistered addition;
      both deserve refusal. ALLOW-by-default means every newly imported tool is silently
      world-usable and nothing warns you.
    - Store unreachable -> DENY (fail closed) and surface the error. An agent running
      unrestricted because Postgres blipped is worse than an agent that stops.

HOW THE TABLE ABOVE ACTUALLY LANDED (migration 0004, owned by t-f1-16)
    `policy_rules (rule_id pk, definition jsonb, updated_at)`. Every field named above -
    the tenant dimension included - lives inside `definition`, so the tenant narrowing is
    carried from day 1 as the docstring demands, just as a jsonb key rather than a column.
    An absent `tenant_id` means "all tenants", exactly as a NULL column would.

AND HOW THE TENANT BECAME A COLUMN (migration 0012, t-d2-03)
    `policy_tenant_migration.py` adds the nullable `tenant_id` column this docstring asked
    for, backfills it from the jsonb key, and indexes the expression the predicate below
    uses. Read that module for what an existing row means and why NULL was chosen over
    NOT NULL; the short version is that the migration RECORDS the reach those rows already
    have rather than granting them a new one.

    Two halves make the tenant dimension real, and each is inert without the other. The
    SQL predicate stops another tenant's rows being transferred at all; `_to_rule`
    populating `PolicyRule.tenant_id` is what lets `RuleSet.applicable` re-check them, so
    a leaky query still grants nothing. The predicate alone would leave the domain check
    reading a field nothing writes - which looks safe and is not.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from agent_core.domain.policy import (
    EFFECT_PRECEDENCE,
    Effect,
    PolicyDecision,
    PolicyRule,
    RuleSet,
)
from agent_core.domain.turn import CallerIdentity, TenantId

_LOG = logging.getLogger(__name__)

# A source of connections that belong to THIS repository. `psycopg_pool.ConnectionPool
# .connection` satisfies it directly; so does `lambda: psycopg.connect(conninfo)`.
ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# What a store failure is reported through when the caller supplies no hook of its own.
ErrorHook = Callable[[BaseException], None]

# THE TENANT A RULE BELONGS TO, READ FROM BOTH CARRIERS.
#
# The `tenant_id` COLUMN (migration 0012, policy_tenant_migration.py) is authoritative.
# The jsonb key is the pre-0012 carrier and is still read, because dropping it would widen
# every row still written in the old shape into a platform-wide rule - the exact privilege
# widening 0012 exists to avoid, arriving through the reader instead of the migration.
# One constant, used by the predicate and by what the query hands back, so the rows that
# survive the WHERE clause and the tenant stamped on them can never come from different
# expressions.
_RULE_TENANT = "COALESCE(tenant_id, definition->>'tenant_id')"

# The row shape is (rule_id, EFFECTIVE definition): the stored jsonb with the rule's
# tenant merged over it, rather than a third column. `_to_rule` then reads every field of
# a rule from one mapping, and the column-versus-jsonb question is answered once, in SQL,
# instead of at each field. `jsonb_build_object` with a NULL value yields a JSON null, so
# a rule belonging to no tenant arrives as `tenant_id: null` and reads as ALL TENANTS -
# the same reading `domain/policy.py` gives `PolicyRule.tenant_id = None`.
_SELECT_RULES = (
    f"SELECT rule_id, definition || jsonb_build_object('tenant_id', {_RULE_TENANT}) "
    "FROM policy_rules "
    f"WHERE {_RULE_TENANT} IS NULL OR {_RULE_TENANT} = %s"
)

_NO_RULE_REASON = (
    "no policy rule matches this tool for this caller; denied by default. "
    "An unregistered tool is refused until a rule grants it."
)
_STORE_DOWN_REASON = (
    "the policy store was unreachable, so this turn holds no rules and denies every tool."
)


class _UnreachableStore(RuleSet):
    """The fail-closed snapshot, marked by its TYPE rather than by its emptiness.

    WHY A TYPE AND NOT A COUNT (t-f11-06)
        `decide` used to read `if rules.rules` to choose between the two sentences above,
        which makes EMPTINESS the evidence of unreachability. It is not evidence of
        anything: a reachable `policy_rules` with no rows - the state of every fresh
        clone, before anyone has inserted a rule - produces exactly the same empty
        snapshot, and was therefore told, and told its operator, that the database was
        down. The verdict was right and the explanation was false, which is worse than a
        wrong verdict in one specific way: it sends a human to check a database that is
        fine, and hands the model a sentence about the world that is not true.

        So the one place that KNOWS - `load_rules`'s exception handler, where the query
        actually failed - records it, and `decide` reads it back. A snapshot built any
        other way, by this adapter or by a caller, is a store that answered.

    WHY IT ADDS NO FIELD, AND WHY IT IS NOT A FLAG ON THIS CLASS
        `RuleSet` is domain and frozen (`t-d2-06` carries the tenant through it), so this
        refinement lives in the adapter. It declares no state: an `_UnreachableStore` is
        equal in every field to the snapshot this code already returned - no rules,
        `default_effect=DENY`, narrowed for the same caller - so every fail-closed
        property asserted elsewhere still holds, and only the diagnosis moved.

        Storing the failure on `PgToolPolicy` instead would put per-turn state on an
        adapter shared by every concurrent turn: one blipped load would explain every
        other caller's honest DENY, and only under concurrency. The snapshot is already
        the per-turn object, so the fact travels with it.

        ONE CONSEQUENCE WORTH KNOWING: `RuleSet` is a dataclass, so its `__eq__` compares
        classes as well as fields, and this snapshot is therefore NOT `==` to a plain
        `RuleSet` holding the same values. Assert on the fields - `rules`,
        `default_effect`, the narrowing - as the existing fail-closed tests do, rather
        than on whole-object equality.
    """

    __slots__ = ()


class PgToolPolicy:
    """`ToolPolicy` over Postgres. One query per turn, then pure decisions.

    D13 shapes this class: `load_rules` is the only awaitable method, because it is the
    only one that touches I/O. `filter_toolset` and `decide` are sync and pure over the
    snapshot it returned - `decide` runs inside `before_tool_execute`, on every single
    tool call, and a round trip there would put database latency on every tool the agent
    uses.

    FAIL CLOSED IS THE POINT, AND IT LIVES IN EXACTLY ONE PLACE
        A store failure is caught here, in `load_rules`, and turned into an empty
        snapshot with `default_effect=DENY`. `decide` then refuses everything from that
        snapshot alone, with no unreachability branch of its own to drift out of step.

        The VERDICT is that snapshot's alone. The EXPLANATION needs one more fact, which
        `load_rules` is the only code that holds: whether the query failed or simply
        matched nothing. It travels as the snapshot's type (`_UnreachableStore`), so
        `decide` still reads one object and cannot infer a failed query from an empty
        table - t-f11-06, and the reason a fresh clone used to be told its database was
        down.
    """

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        on_error: ErrorHook | None = None,
    ) -> None:
        self._connect = connect
        self._on_error: ErrorHook = _log_error if on_error is None else on_error

    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        """Every rule applicable to `caller`, as one frozen snapshot. Once per turn.

        Postgres access is synchronous (D13), so the blocking query runs in a thread.

        On ANY store failure - a refused connection, a pool timeout, a driver error
        against a drifted schema - this returns the fail-closed snapshot and hands the
        exception to `on_error`. It never re-raises: a caller that has to remember to
        catch is a caller that eventually forgets, and forgetting here kills the turn
        instead of denying the tool.
        """
        try:
            rows = await asyncio.to_thread(self._load_sync, caller)
        except Exception as error:  # fail closed over the whole class of store failures
            self._on_error(error)
            # The ONE place that knows the query failed, so it is the one place that says
            # so. Same fields as before, a different type - see `_UnreachableStore`.
            return _UnreachableStore.for_caller(caller)
        return RuleSet.for_caller(caller, rules=self._to_rules(rows, caller))

    def filter_toolset(self, rules: RuleSet, tool_names: tuple[str, ...]) -> tuple[str, ...]:
        """Sync and pure. Drop what would be denied, keep what a human could approve.

        Defined as `decide` with empty arguments so the two can never disagree: a name
        kept here and denied there only ever wastes the model's budget.
        """
        return tuple(
            name for name in tool_names if self.decide(rules, name, {}).effect is not Effect.DENY
        )

    def decide(
        self,
        rules: RuleSet,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PolicyDecision:
        """Sync and pure - the hot path, one call per tool call.

        `arguments` is accepted because the port's contract is argument-aware while F1's
        stored rule shape is not yet: nothing here branches on a value, so nothing reads
        it. Dropping the parameter would turn the first argument-matching rule into a
        signature change across every call site.

        Reduction is by EFFECT_PRECEDENCE - DENY beats NEEDS_APPROVAL beats ALLOW - so a
        rule that grants can never overturn a rule that forbids, whatever its specificity
        or insertion order. No match at all falls back to the snapshot's `default_effect`,
        which is DENY, the fail-closed snapshot included.
        """
        matched = rules.applicable(tool_name)
        if not matched:
            # WHICH refusal this is comes from the snapshot's TYPE, never from how many
            # rules it holds: a reachable, empty `policy_rules` and a store whose query
            # failed both arrive here with nothing to match, and they are not the same
            # state. "Unreachable" means the query failed; "no rule matched" means it
            # succeeded and matched nothing. Both deny - t-f1-15 froze that and it is
            # right - and each says which, in words the other does not use, because these
            # sentences are what the model receives as the tool result and what a human
            # reads to diagnose the refusal.
            store_down = isinstance(rules, _UnreachableStore)
            reason = _STORE_DOWN_REASON if store_down else _NO_RULE_REASON
            return PolicyDecision(effect=rules.default_effect, reason=reason, rule_id=None)
        winner = min(matched, key=lambda rule: EFFECT_PRECEDENCE.index(rule.effect))
        return PolicyDecision(effect=winner.effect, reason=winner.reason, rule_id=winner.rule_id)

    def _load_sync(self, caller: CallerIdentity) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """The one blocking query, narrowed by tenant IN SQL - never afterwards in Python.

        A post-retrieval filter returns the same list on a good day and the wrong one the
        day a second code path forgets it, and the failure is silent: a missing predicate
        returns MORE rows, and more rows look like a more capable agent. So the narrowing
        is in the WHERE clause, where a statement without it is visible in the statement
        itself - see test_policy_tenant_sql.py, which asserts on the emitted SQL and not
        only on the rows.
        """
        with self._connect() as connection:
            rows = connection.execute(_SELECT_RULES, (str(caller.tenant_id),)).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    def _to_rules(
        self,
        rows: Sequence[tuple[str, Mapping[str, Any]]],
        caller: CallerIdentity,
    ) -> tuple[PolicyRule, ...]:
        """Rows to domain rules, narrowed to the ones that could apply to this caller.

        A row this method cannot parse is DROPPED and surfaced. Dropping is the
        fail-closed reading: a malformed ALLOW must not grant, and a malformed DENY costs
        nothing, because no match already means DENY.

        The role and channel narrowing below is a bandwidth optimisation, never the
        safety boundary - `RuleSet.applicable` re-checks both against the snapshot's own
        stored narrowing on every decision.
        """
        rules: list[PolicyRule] = []
        for rule_id, definition in rows:
            rule = self._to_rule(rule_id, definition)
            if rule is None:
                continue
            if rule.subject_roles and rule.subject_roles.isdisjoint(caller.roles):
                continue
            if rule.channels and caller.channel not in rule.channels:
                continue
            rules.append(rule)
        return tuple(rules)

    def _to_rule(self, rule_id: str, definition: Mapping[str, Any]) -> PolicyRule | None:
        """One row to one domain rule.

        `tenant_id` IS POPULATED HERE AND THAT IS NOT COSMETIC. `RuleSet.applicable`
        re-checks the tenant on every decision so the domain survives a leaky query, and
        that re-check reads exactly this field. Leaving it None - which is what this
        method did before t-d2-03 - makes every stored rule read as platform-wide and
        turns the re-check into a no-op: a predicate and a domain check both consulting a
        field nothing writes is worse than neither, because the shape looks safe.

        Absent or JSON-null means ALL TENANTS, matching `PolicyRule.tenant_id`.
        """
        tenant = definition.get("tenant_id")
        try:
            return PolicyRule(
                rule_id=rule_id,
                tool_pattern=str(definition["tool_pattern"]),
                effect=Effect(definition["effect"]),
                reason=str(definition.get("reason", "")),
                subject_roles=frozenset(definition.get("subject_roles") or ()),
                channels=frozenset(definition.get("channels") or ()),
                tenant_id=None if tenant is None else TenantId(str(tenant)),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._on_error(error)
            return None


def _log_error(error: BaseException) -> None:
    """The default error channel. A fail-closed turn that explains itself nowhere is how
    a blipped database gets diagnosed as a broken agent."""
    _LOG.error("policy store unavailable, failing closed: %s", error, exc_info=error)
