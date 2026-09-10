"""Tool policy vocabulary - may this caller run this tool, and does a human decide?

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-02
Status:  MATCH SEMANTICS IMPLEMENTED (t-f1-02) / REDUCTION LIVES AT THE ENFORCEMENT POINT

WHY THIS IS OURS AND NOT A DEPENDENCY
    The one subsystem no framework can supply, because it encodes the business's own
    rules about who may do what. It is also a SILENT-BUG AREA: a policy hole never fails
    a test, it just never fires. See CLAUDE.md.

THE THREE-VALUED RESULT IS THE POINT
    A boolean allow/deny cannot express "yes, but a human confirms first", which is the
    single most valuable state in an agent system. Keep it three-valued.

ORDERING RULE - settle this in F1, do not leave it implicit
    DENY beats NEEDS_APPROVAL beats ALLOW. A rule that grants must never overturn a rule
    that forbids, regardless of specificity or insertion order. OpenClaw states the same
    rule as "deny wins"; it is the only ordering that fails safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_core.domain.turn import CallerIdentity


class Effect(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


# Resolution order for conflicting matches. Index 0 wins. Do not reorder without a test.
EFFECT_PRECEDENCE: tuple[Effect, ...] = (Effect.DENY, Effect.NEEDS_APPROVAL, Effect.ALLOW)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The verdict for ONE tool call.

    `reason` is not decoration and has two audiences. On DENY it goes back to the model
    as the tool result, so the agent can adapt instead of retrying blindly. On
    NEEDS_APPROVAL it goes to the human as the ask. Write it for both.

    `rule_id` is what an auditor reads six months later to answer "why was this
    allowed?". A decision with no rule_id is unauditable."""

    effect: Effect
    reason: str
    rule_id: str | None = None

    @property
    def blocks_execution(self) -> bool:
        return self.effect is not Effect.ALLOW


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One stored rule.

    `tool_pattern` supports a trailing wildcard (`browser_*`, `mcp_*`), case-insensitive.

    Semantics are pinned by tests/unit/test_policy.py, written before any rule was stored.
    Changing them after rules exist silently changes what is allowed in production, with
    no failing test to warn you - so change the tests first, deliberately, or not at all.

    TODO(D2): add `tenant_id: TenantId | None`, where None means "all tenants". Leaving
    the column out now means an ALTER on a table that is already authoritative.
    """

    rule_id: str
    tool_pattern: str
    effect: Effect
    reason: str
    subject_roles: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset()

    def matches(self, tool_name: str, roles: frozenset[str], channel: str) -> bool:
        """True only when every POPULATED constraint on this rule matched.

        1. `tool_name` and `tool_pattern` are compared lowercased, so case never decides a
           verdict: a provider advertising `Browser_Click` still meets `browser_*`.
        2. A pattern ending in '*' is a PREFIX, never a substring - `browser_*` must not
           reach `mcp_browser_click`. Any other pattern is exact.
        3. An EMPTY `subject_roles` means "any role", NOT "no roles". A populated set
           requires a non-empty intersection with `roles`. Getting this backwards is how a
           rule silently stops applying to everybody.
        4. Same reading for `channels`.

        NOTE the deliberate asymmetry with `MediaPolicy.accepts`, where empty means
        "nothing". Different defaults because the failure modes differ: a permissive
        policy rule is inconvenient, permissive media handling is an incident.
        """
        if not self._pattern_matches(tool_name):
            return False
        if self.subject_roles and self.subject_roles.isdisjoint(roles):
            return False
        if self.channels and channel not in self.channels:
            return False
        return True

    def _pattern_matches(self, tool_name: str) -> bool:
        pattern = self.tool_pattern.lower()
        name = tool_name.lower()
        if pattern.endswith("*"):
            return name.startswith(pattern[:-1])
        return name == pattern


@dataclass(frozen=True, slots=True)
class RuleSet:
    """Every rule applicable to one caller, loaded ONCE per turn.

    WHY THIS TYPE EXISTS (D13)
        `decide()` runs inside `before_tool_execute`, i.e. on EVERY tool call. A database
        round trip per call adds network latency to every tool the agent uses.

        So the I/O and the decision are split: `ToolPolicy.load_rules()` is async and runs
        once per turn; `decide()` is synchronous and pure over this frozen snapshot.

    A frozen snapshot also makes the turn's decisions internally consistent: a rule edited
    mid-turn cannot change the verdict between the third tool call and the fourth.

    IT CARRIES THE NARROWING IT WAS LOADED FOR - THAT IS THE SAFETY PROPERTY
        `load_rules(caller)` narrows by the caller, so the snapshot stores the subject
        roles and the channel it was narrowed for. Everything downstream then needs only a
        tool name: a snapshot IS the answer for exactly one caller's narrowing and cannot
        be pointed at another one.

        The alternative - keeping `rules` alone and passing the caller again to every
        query - type-checks perfectly while answering caller A's rules about caller B, and
        in a policy engine that means a tool gets allowed for someone who should not have
        it. Nothing detects it: no exception, no failing test. Closing it structurally is
        cheaper than any convention that asks callers to pass the matching identity."""

    subject_roles: frozenset[str]
    channel: str
    rules: tuple[PolicyRule, ...] = ()
    default_effect: Effect = Effect.DENY

    @classmethod
    def for_caller(
        cls,
        caller: CallerIdentity,
        rules: tuple[PolicyRule, ...] = (),
        default_effect: Effect = Effect.DENY,
    ) -> RuleSet:
        """Build a snapshot narrowed for `caller`. The one intended construction path.

        The adapter reads the caller's rows and hands them here, so the narrowing stored on
        the snapshot always comes from the identity the query was run for, rather than
        being copied by hand at each call site.

        The fail-closed snapshot from an unreachable store is this with no rules: it
        matches nothing and defaults to DENY."""
        return cls(
            subject_roles=caller.roles,
            channel=caller.channel,
            rules=rules,
            default_effect=default_effect,
        )

    def applicable(self, tool_name: str) -> tuple[PolicyRule, ...]:
        """Every rule matching this tool for THIS snapshot's caller, unreduced.

        No roles and no channel parameter on purpose: they are the narrowing this snapshot
        was loaded for. `decide()` reduces the result by EFFECT_PRECEDENCE and falls back
        to `default_effect` when this returns nothing."""
        return tuple(
            rule
            for rule in self.rules
            if rule.matches(tool_name, self.subject_roles, self.channel)
        )
