"""Driven adapter: ToolPolicy over Postgres.

Phase:   F1 / D2 (tenant dimension)
Tasks:   docs/TASKS.md#t-f1-15
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
from agent_core.domain.turn import CallerIdentity

_LOG = logging.getLogger(__name__)

# A source of connections that belong to THIS repository. `psycopg_pool.ConnectionPool
# .connection` satisfies it directly; so does `lambda: psycopg.connect(conninfo)`.
ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# What a store failure is reported through when the caller supplies no hook of its own.
ErrorHook = Callable[[BaseException], None]

_SELECT_RULES = (
    "SELECT rule_id, definition FROM policy_rules "
    "WHERE definition->>'tenant_id' IS NULL OR definition->>'tenant_id' = %s"
)

_NO_RULE_REASON = (
    "no policy rule matches this tool for this caller; denied by default. "
    "An unregistered tool is refused until a rule grants it."
)
_STORE_DOWN_REASON = (
    "the policy store was unreachable, so this turn holds no rules and denies every tool."
)


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
            return RuleSet.for_caller(caller)
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
            reason = _NO_RULE_REASON if rules.rules else _STORE_DOWN_REASON
            return PolicyDecision(effect=rules.default_effect, reason=reason, rule_id=None)
        winner = min(matched, key=lambda rule: EFFECT_PRECEDENCE.index(rule.effect))
        return PolicyDecision(effect=winner.effect, reason=winner.reason, rule_id=winner.rule_id)

    def _load_sync(self, caller: CallerIdentity) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """The one blocking query, narrowed by tenant in SQL so a tenant never pays to
        transfer another tenant's rules."""
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
        try:
            return PolicyRule(
                rule_id=rule_id,
                tool_pattern=str(definition["tool_pattern"]),
                effect=Effect(definition["effect"]),
                reason=str(definition.get("reason", "")),
                subject_roles=frozenset(definition.get("subject_roles") or ()),
                channels=frozenset(definition.get("channels") or ()),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._on_error(error)
            return None


def _log_error(error: BaseException) -> None:
    """The default error channel. A fail-closed turn that explains itself nowhere is how
    a blipped database gets diagnosed as a broken agent."""
    _LOG.error("policy store unavailable, failing closed: %s", error, exc_info=error)
