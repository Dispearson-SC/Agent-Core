"""The tenant dimension of ToolPolicy - a SILENT-BUG AREA. This file is its only alarm.

Phase:   D2 - Multi-tenancy
Tasks:   docs/TASKS.md#t-d2-06
Status:  RED FIRST, THEN GREEN

WHAT THIS PINS
    `RuleSet` already refuses to answer a question about another caller's roles or
    channel, because it stores the narrowing it was loaded for. Until t-d2-06 it stored
    no tenant, so a snapshot loaded for tenant A was structurally capable of answering
    about tenant B and only the discipline of the SQL stood between the two.

    A cross-tenant grant leaves no exception and no failing test behind - a missing
    predicate returns MORE rows, not fewer - so the guard has to be in the shape:
    the tenant seat on `RuleSet` has NO default, exactly as `subject_roles` and `channel`
    have none, and exactly as `TenantKnowledgePolicy.tenant_id` has none one boundary over.
"""

from __future__ import annotations

import pytest

from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.turn import CallerIdentity, TenantId

TENANT_A = TenantId("tenant-a")
TENANT_B = TenantId("tenant-b")


def _caller(tenant: TenantId) -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-treasurer",
        channel="http",
        tenant_id=tenant,
        roles=frozenset({"treasurer"}),
    )


def _rule(rule_id: str, tenant: TenantId | None) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        tool_pattern="transfer_funds",
        effect=Effect.ALLOW,
        reason="Treasury operations.",
        tenant_id=tenant,
    )


@pytest.mark.silent
def test_a_snapshot_for_one_tenant_cannot_answer_about_another() -> None:
    """The narrowing is STRUCTURAL: the snapshot carries the tenant it was loaded for.

    `for_caller` takes the tenant from the identity the rules were loaded for, so the two
    can never disagree. A tenant passed BESIDE the snapshot - the alternative - type-checks
    perfectly while naming a tenant the snapshot was never loaded for, and in a policy
    engine that is one business's rule granting another business's tool.
    """
    b_rule = _rule("r-b", TENANT_B)

    for_a = RuleSet.for_caller(_caller(TENANT_A), rules=(b_rule,))
    assert for_a.tenant_id == TENANT_A

    # Tenant B's row is physically present in the snapshot - a leaky query is exactly the
    # case the domain must survive - and it still grants nothing to tenant A.
    assert for_a.applicable("transfer_funds") == ()

    # The same rows, narrowed for the tenant they belong to, do apply.
    for_b = RuleSet.for_caller(_caller(TENANT_B), rules=(b_rule,))
    assert for_b.applicable("transfer_funds") == (b_rule,)

    # The wrong-tenant question is inexpressible: `applicable` takes the tool name only.
    with pytest.raises(TypeError):
        for_a.applicable("transfer_funds", TENANT_B)  # type: ignore[call-arg]

    # And the tenant narrowing cannot be left off at construction, any more than the roles
    # or the channel can. A seat with a default is a seat a caller may forget.
    with pytest.raises(TypeError):
        RuleSet(  # type: ignore[call-arg]
            subject_roles=frozenset({"treasurer"}),
            channel="http",
            rules=(b_rule,),
        )


@pytest.mark.silent
def test_for_caller_takes_the_tenant_from_the_caller() -> None:
    """`for_caller` is the one intended construction path, and it cannot be lied to.

    There is no tenant parameter to disagree with the caller: the narrowing comes from the
    identity the rules were loaded for, not from a value copied by hand at the call site -
    which is where it would eventually be copied wrong. Shaped exactly like
    `TenantKnowledgePolicy.for_caller`, one security boundary over."""
    caller = _caller(TENANT_A)

    assert RuleSet.for_caller(caller).tenant_id == caller.tenant_id

    with pytest.raises(TypeError):
        RuleSet.for_caller(caller, tenant_id=TENANT_B)  # type: ignore[call-arg]


@pytest.mark.silent
def test_a_rule_with_no_tenant_applies_to_every_tenant() -> None:
    """`tenant_id is None` means ALL TENANTS - the platform-wide rule.

    Same reading as an empty `subject_roles`: absent constraint means "any", not "none".
    Getting this backwards is how the global `DENY browser_*` silently stops applying to
    everybody, and nothing fails."""
    global_rule = _rule("r-global", None)

    for tenant in (TENANT_A, TENANT_B):
        snapshot = RuleSet.for_caller(_caller(tenant), rules=(global_rule,))
        assert snapshot.applicable("transfer_funds") == (global_rule,)

    # A platform rule and a tenant rule coexist; each snapshot sees its own plus the global.
    a_rule = _rule("r-a", TENANT_A)
    mixed = (global_rule, a_rule)
    assert RuleSet.for_caller(_caller(TENANT_A), rules=mixed).applicable("transfer_funds") == (
        global_rule,
        a_rule,
    )
    assert RuleSet.for_caller(_caller(TENANT_B), rules=mixed).applicable("transfer_funds") == (
        global_rule,
    )


@pytest.mark.silent
def test_matches_without_a_tenant_fails_closed() -> None:
    """`PolicyRule.matches` keeps its three-argument shape, and the tenant it defaults to
    is NO TENANT - which a tenant-scoped rule refuses.

    The tenant argument is the one constraint a caller could still omit, so omitting it
    must over-deny rather than over-grant. A forgotten narrowing that fails open is the
    whole defect; a forgotten narrowing that fails closed is an inconvenience."""
    scoped = _rule("r-a", TENANT_A)
    platform = _rule("r-global", None)
    roles = frozenset({"treasurer"})

    assert scoped.matches("transfer_funds", roles, "http") is False
    assert scoped.matches("transfer_funds", roles, "http", TENANT_A) is True
    assert scoped.matches("transfer_funds", roles, "http", TENANT_B) is False

    assert platform.matches("transfer_funds", roles, "http") is True
    assert platform.matches("transfer_funds", roles, "http", TENANT_A) is True
    assert platform.matches("transfer_funds", roles, "http", TENANT_B) is True
