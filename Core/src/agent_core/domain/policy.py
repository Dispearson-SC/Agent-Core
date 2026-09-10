"""Tool policy vocabulary - may this caller run this tool, and does a human decide?

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-02
Status:  TYPES DEFINED / BEHAVIOUR PENDING

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

    TODO(F1): implement `matches()` and pin its semantics with tests BEFORE any rule is
    stored. Changing match semantics after rules exist silently changes what is allowed
    in production, with no failing test to warn you.

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
        """PSEUDO-CODE - implement in F1.

        1. Lowercase `tool_name` and `self.tool_pattern`.
        2. Match: exact, or prefix match when the pattern ends in '*'.
        3. If `subject_roles` is non-empty, require a non-empty intersection with `roles`.
           An EMPTY set means "any role", NOT "no roles". Test both readings - getting it
           backwards is how a rule silently stops applying to everybody.
        4. Same reading for `channels`.
        5. True only when every populated constraint matched.

        NOTE the deliberate asymmetry with `MediaPolicy.accepts`, where empty means
        "nothing". Different defaults because the failure modes differ: a permissive
        policy rule is inconvenient, permissive media handling is an incident.
        """
        raise NotImplementedError("F1 - docs/TASKS.md#t-f1-02")


@dataclass(frozen=True, slots=True)
class RuleSet:
    """Every rule applicable to one caller, loaded ONCE per turn.

    WHY THIS TYPE EXISTS (D13)
        `decide()` runs inside `before_tool_execute`, i.e. on EVERY tool call. A database
        round trip per call adds network latency to every tool the agent uses.

        So the I/O and the decision are split: `ToolPolicy.load_rules()` is async and runs
        once per turn; `decide()` is synchronous and pure over this frozen snapshot.

    A frozen snapshot also makes the turn's decisions internally consistent: a rule edited
    mid-turn cannot change the verdict between the third tool call and the fourth."""

    rules: tuple[PolicyRule, ...] = ()
    default_effect: Effect = Effect.DENY

    def applicable(self, tool_name: str, roles: frozenset[str], channel: str) -> tuple[PolicyRule, ...]:
        """PSEUDO-CODE - F1. Every rule matching, unreduced. `decide()` reduces by
        EFFECT_PRECEDENCE."""
        raise NotImplementedError("F1 - docs/TASKS.md#t-f1-02")
