"""Tool policy vocabulary - may this caller run this tool, and does a human decide?

Phase:   F1 - Real hexagonal core / D2 - Multi-tenancy
Tasks:   docs/TASKS.md#t-f1-02, docs/TASKS.md#t-d2-06
Status:  MATCH SEMANTICS IMPLEMENTED (t-f1-02) / REDUCTION LIVES AT THE ENFORCEMENT POINT /
         TENANT NARROWING ADDED (t-d2-06) - the SQL predicate is t-d2-03 and is NOT here

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

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from agent_core.domain.turn import CallerIdentity, TenantId


class Effect(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


# C15 (D41). Which argument of a tool call names the THING it acts on, for the tools whose rules can
# be narrowed to it. Today only `ask_peer`: its `target` is the peer id. A tool that is not listed
# has no resource, and a rule that names one never matches it.
RESOURCE_ARGUMENTS: Final[Mapping[str, str]] = MappingProxyType({"ask_peer": "target"})


def resource_of(tool_name: str, arguments: Mapping[str, object]) -> str | None:
    """The resource a call acts on, or None when the tool has none or the call names none.

    Pure, and total over whatever a model wrote: a missing, empty or non-text argument is "no
    resource", never an error. A rule narrowed to a resource does not apply to a call without one
    (see `PolicyRule.matches`), so a malformed call cannot be matched to a peer by accident.
    """
    argument = RESOURCE_ARGUMENTS.get(tool_name.lower())
    if argument is None:
        return None
    value = arguments.get(argument)
    return value if isinstance(value, str) and value else None


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

    Semantics are pinned by tests/unit/test_policy.py, written before any rule was stored,
    and the tenant dimension by tests/unit/test_policy_tenant.py. Changing them after rules
    exist silently changes what is allowed in production, with no failing test to warn you -
    so change the tests first, deliberately, or not at all.

    `tenant_id` is the D2 seat, and None means ALL TENANTS: the platform-wide rule, the one
    an operator writes once to forbid `browser_*` everywhere. Same reading as an empty
    `subject_roles` - absent constraint means "any", not "none".
    """

    rule_id: str
    tool_pattern: str
    effect: Effect
    reason: str
    subject_roles: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset()
    tenant_id: TenantId | None = None
    # C15 (D41). Narrows the rule to the THING a call acts on (for `ask_peer`: the peer id), same
    # pattern grammar as `tool_pattern`. None = every resource, i.e. the rule is about the tool.
    resource_pattern: str | None = None

    def matches(
        self,
        tool_name: str,
        roles: frozenset[str],
        channel: str,
        tenant: TenantId | None = None,
        resource: str | None = None,
    ) -> bool:
        """True only when every POPULATED constraint on this rule matched.

        1. `tool_name` and `tool_pattern` are compared lowercased, so case never decides a
           verdict: a provider advertising `Browser_Click` still meets `browser_*`.
        2. A pattern ending in '*' is a PREFIX, never a substring - `browser_*` must not
           reach `mcp_browser_click`. Any other pattern is exact.
        3. An EMPTY `subject_roles` means "any role", NOT "no roles". A populated set
           requires a non-empty intersection with `roles`. Getting this backwards is how a
           rule silently stops applying to everybody.
        4. Same reading for `channels`, and for `tenant_id`: None on the RULE means every
           tenant. A populated `tenant_id` requires equality with `tenant`.
        5. `tenant` DEFAULTS TO None, and that default is the one asymmetry here: on the
           rule None widens, on the ARGUMENT it narrows to nothing. Omitting the tenant
           means "no tenant established", so a tenant-scoped rule refuses. It is the only
           constraint a caller can still leave off - `RuleSet.applicable` always supplies
           it from the snapshot's narrowing - so leaving it off has to over-deny. A
           forgotten narrowing that fails open is the whole defect this seat closes; a
           forgotten narrowing that fails closed is an inconvenience.

        6. (C15) A rule with a `resource_pattern` applies only to a call that NAMES a resource, and
           only to one the pattern matches (same grammar as the tool pattern: lowercased, a
           trailing '*' is a prefix). `resource` DEFAULTS TO None, which is "this decision is about
           the tool as a whole": a rule narrowed to one peer says nothing about it. A rule without
           a pattern is about the tool and matches every resource, so rules stored before C15 mean
           what they always meant.

        NOTE the deliberate asymmetry with `MediaPolicy.accepts`, where empty means
        "nothing". Different defaults because the failure modes differ: a permissive
        policy rule is inconvenient, permissive media handling is an incident.
        """
        if not self.applies_to_caller(tool_name, roles, channel, tenant):
            return False
        if self.resource_pattern is not None and (
            resource is None or not pattern_matches(self.resource_pattern, resource)
        ):
            return False
        return True

    def applies_to_caller(
        self,
        tool_name: str,
        roles: frozenset[str],
        channel: str,
        tenant: TenantId | None = None,
    ) -> bool:
        """Steps 1-5 of `matches`: the tool, the roles, the channel and the tenant, and nothing
        about the resource."""
        if not self._pattern_matches(tool_name):
            return False
        if self.subject_roles and self.subject_roles.isdisjoint(roles):
            return False
        if self.channels and channel not in self.channels:
            return False
        if self.tenant_id is not None and self.tenant_id != tenant:
            return False
        return True

    def _pattern_matches(self, tool_name: str) -> bool:
        return pattern_matches(self.tool_pattern, tool_name)


def pattern_matches(pattern: str, value: str) -> bool:
    """Exact match, or a PREFIX match when the pattern ends in '*'; both lowercased."""
    pattern = pattern.lower()
    value = value.lower()
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


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
        roles, the channel AND THE TENANT it was narrowed for. Everything downstream then
        needs only a tool name: a snapshot IS the answer for exactly one caller's narrowing
        and cannot be pointed at another one.

        The alternative - keeping `rules` alone and passing the caller again to every
        query - type-checks perfectly while answering caller A's rules about caller B, and
        in a policy engine that means a tool gets allowed for someone who should not have
        it. Nothing detects it: no exception, no failing test. Closing it structurally is
        cheaper than any convention that asks callers to pass the matching identity.

    WHY THE TENANT IS A SEAT HERE AND NOT A PARAMETER ON `applicable` (D2, t-d2-06)
        `t-d2-03` puts the tenant predicate in the SQL, and for a while that looked like
        the whole job. It is not: a snapshot that records no tenant is structurally capable
        of answering about another one, so the SQL would be the only thing standing between
        two businesses, and a missing predicate returns MORE rows rather than fewer. The
        domain has to survive a leaky query, not assume a tight one.

        So the tenant joins the roles and the channel, and it has NO DEFAULT for the same
        reason they have none - and the same reason `TenantKnowledgePolicy.tenant_id` has
        none one boundary over. Two security boundaries, one shape. A tenant passed BESIDE
        the snapshot type-checks perfectly while naming a tenant the snapshot was never
        loaded for; a seat with a default is a seat a caller may forget."""

    subject_roles: frozenset[str]
    channel: str
    tenant_id: TenantId = field(kw_only=True)
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

        There is deliberately NO tenant parameter to disagree with the caller: the tenant
        is read off the identity the query was run for, exactly as the roles and the channel
        are, so the three narrowings can never drift apart.

        The fail-closed snapshot from an unreachable store is this with no rules: it
        matches nothing and defaults to DENY."""
        return cls(
            subject_roles=caller.roles,
            channel=caller.channel,
            tenant_id=caller.tenant_id,
            rules=rules,
            default_effect=default_effect,
        )

    def applicable(
        self, tool_name: str, *, resource: str | None = None
    ) -> tuple[PolicyRule, ...]:
        """Every rule matching this tool for THIS snapshot's caller, unreduced.

        `resource` (C15, keyword-only so that a tenant or a role passed positionally is still a
        TypeError) is what the call acts on (`resource_of`): the rules narrowed to a resource
        match only when it is given and fits; without it only the rules about the tool as a whole
        do.

        No roles, no channel and NO TENANT parameter on purpose: they are the narrowing
        this snapshot was loaded for. `decide()` reduces the result by EFFECT_PRECEDENCE and
        falls back to `default_effect` when this returns nothing.

        The tenant is re-checked here rather than trusted from the query, so a snapshot
        that was handed another tenant's rows grants nothing on them."""
        return tuple(
            rule
            for rule in self.rules
            if rule.matches(
                tool_name, self.subject_roles, self.channel, self.tenant_id, resource
            )
        )

    def grants_some_resource(self, tool_name: str) -> bool:
        """Whether a rule narrowed to a resource could still ALLOW or ask for this tool (C15).

        For the one question asked without a call: should the tool be OFFERED at all
        (`ToolPolicy.filter_toolset`). A tool whose only grants name particular peers matches no
        rule until a peer is named, and would otherwise be dropped before the model could name one.
        """
        return any(
            rule.effect is not Effect.DENY
            for rule in self.rules
            if rule.resource_pattern is not None
            and rule.applies_to_caller(tool_name, self.subject_roles, self.channel, self.tenant_id)
        )
