"""ToolPolicy - a SILENT-BUG AREA. These tests are the only alarm that exists.

Each test below encodes a decision from domain/policy.py. If one starts failing, the
question is which behaviour changed, not how to make it pass.
"""

from itertools import permutations

import pytest

from agent_core.domain.policy import EFFECT_PRECEDENCE, Effect, PolicyRule, RuleSet
from agent_core.domain.turn import CallerIdentity
from tests.fakes.ports import FakeToolPolicy

TREASURER = CallerIdentity(
    subject_id="u-treasurer",
    channel="http",
    tenant_id="t-1",  # type: ignore[arg-type]
    roles=frozenset({"treasurer"}),
)

INTERN = CallerIdentity(
    subject_id="u-intern",
    channel="webhook",
    tenant_id="t-1",  # type: ignore[arg-type]
    roles=frozenset({"intern"}),
)


@pytest.mark.silent
def test_no_matching_rule_denies(caller: CallerIdentity) -> None:
    """An unknown tool name is a typo or an unregistered addition. Both deserve refusal.

    ALLOW-by-default means every newly imported tool is silently world-usable.
    """
    allow_shell = PolicyRule(
        rule_id="r-shell",
        tool_pattern="run_shell",
        effect=Effect.ALLOW,
        reason="Operators run shell commands.",
    )
    rules = RuleSet(
        subject_roles=caller.roles,
        channel=caller.channel,
        rules=(allow_shell,),
    )

    # A typo and an unregistered tool both fall through to the default.
    assert rules.applicable("run_shel") == ()
    assert rules.applicable("never_registered") == ()
    assert rules.default_effect is Effect.DENY

    # An empty snapshot - the fail-closed shape load_rules() returns when the store is
    # unreachable - matches nothing and still defaults to DENY.
    empty = RuleSet(subject_roles=caller.roles, channel=caller.channel)
    assert empty.applicable("run_shell") == ()
    assert empty.default_effect is Effect.DENY


@pytest.mark.silent
def test_deny_beats_needs_approval_beats_allow() -> None:
    """EFFECT_PRECEDENCE, regardless of rule order or specificity. The only ordering that
    fails safe.

    The domain deliberately does not reduce - `applicable()` hands back every match
    unreduced and the reduction happens at the enforcement point - so this test pins the
    two halves the domain does own, and then reduces.

        1. EFFECT_PRECEDENCE itself, literally. Index 0 wins, so DENY outranks everything.
        2. `applicable()` returns the SAME matches whichever order the rules were declared
           in. A snapshot that dropped or shadowed one here would make the verdict depend
           on insertion order with no reducer being wrong.
        3. The documented reduction over every permutation of the same three rules.

    `FakeToolPolicy` is that reduction, and it is what every use-case test in this suite
    already decides through, so a precedence bug in it is a precedence bug in the fixtures
    the rest of the suite trusts.
    """
    deny = PolicyRule(
        rule_id="r-deny",
        tool_pattern="transfer_*",  # the LEAST specific of the three, and it still wins
        effect=Effect.DENY,
        reason="Wire transfers are frozen for the duration of the audit.",
    )
    needs_approval = PolicyRule(
        rule_id="r-approve",
        tool_pattern="transfer_funds",
        effect=Effect.NEEDS_APPROVAL,
        reason="A human signs off on money leaving.",
    )
    allow = PolicyRule(
        rule_id="r-allow",
        tool_pattern="transfer_funds",  # exact, and it must not overturn the broad DENY
        effect=Effect.ALLOW,
        reason="Treasury operations are part of the job.",
    )

    assert EFFECT_PRECEDENCE == (Effect.DENY, Effect.NEEDS_APPROVAL, Effect.ALLOW)

    for ordering in permutations((deny, needs_approval, allow)):
        rules = RuleSet.for_caller(TREASURER, rules=ordering)

        assert {rule.rule_id for rule in rules.applicable("transfer_funds")} == {
            "r-deny",
            "r-approve",
            "r-allow",
        }, "a match was dropped, so the reduction below is deciding over a different set"

        decision = FakeToolPolicy(rules).decide(rules, "transfer_funds", {})
        assert decision.effect is Effect.DENY
        assert decision.rule_id == "r-deny", (
            "the verdict must cite the rule that actually refused - an auditor reading "
            "this row six months later is answering 'why was this blocked?'"
        )

    # Specificity is not a tie-break in EITHER direction. Asserting only the case above
    # would leave "deny wins" holding just when the DENY happens to be the broad rule.
    exact_deny = PolicyRule(
        rule_id="r-deny-exact",
        tool_pattern="transfer_funds",
        effect=Effect.DENY,
        reason="This particular tool is frozen.",
    )
    wildcard_allow = PolicyRule(
        rule_id="r-allow-wide",
        tool_pattern="transfer_*",
        effect=Effect.ALLOW,
        reason="Treasury tooling is open to the treasury.",
    )
    for ordering in permutations((exact_deny, wildcard_allow)):
        rules = RuleSet.for_caller(TREASURER, rules=ordering)
        decision = FakeToolPolicy(rules).decide(rules, "transfer_funds", {})
        assert decision.effect is Effect.DENY
        assert decision.rule_id == "r-deny-exact"

    # With no DENY in play, NEEDS_APPROVAL still outranks ALLOW. The middle value is the
    # one a boolean engine cannot hold, and losing it turns an ask into a silent yes.
    for ordering in permutations((needs_approval, allow)):
        rules = RuleSet.for_caller(TREASURER, rules=ordering)
        decision = FakeToolPolicy(rules).decide(rules, "transfer_funds", {})
        assert decision.effect is Effect.NEEDS_APPROVAL
        assert decision.rule_id == "r-approve"
        assert decision.blocks_execution is True, (
            "NEEDS_APPROVAL is not ALLOW: it must stop the call until a human answers"
        )


@pytest.mark.silent
def test_empty_role_set_means_any_role_not_no_roles() -> None:
    """The asymmetry that bites: empty `subject_roles` on a PolicyRule means ANY role,
    while empty `accepted_kinds` on a MediaPolicy means NOTHING.

    Getting the policy one backwards makes a rule silently stop applying to everybody.
    """
    unrestricted = PolicyRule(
        rule_id="r-any",
        tool_pattern="read_file",
        effect=Effect.ALLOW,
        reason="Reading is safe for anyone.",
    )
    # ANY role, including a caller carrying no role at all.
    assert unrestricted.matches("read_file", frozenset(), "http") is True
    assert unrestricted.matches("read_file", frozenset({"operator"}), "http") is True
    assert unrestricted.matches("read_file", frozenset({"intern", "auditor"}), "cron") is True

    restricted = PolicyRule(
        rule_id="r-admins",
        tool_pattern="read_file",
        effect=Effect.ALLOW,
        reason="Admins only.",
        subject_roles=frozenset({"admin"}),
    )
    assert restricted.matches("read_file", frozenset(), "http") is False
    assert restricted.matches("read_file", frozenset({"operator"}), "http") is False
    assert restricted.matches("read_file", frozenset({"operator", "admin"}), "http") is True

    # Same reading for `channels`: empty means every channel, populated means only those.
    http_only = PolicyRule(
        rule_id="r-http",
        tool_pattern="read_file",
        effect=Effect.ALLOW,
        reason="Interactive channels only.",
        channels=frozenset({"http"}),
    )
    assert unrestricted.matches("read_file", frozenset({"operator"}), "webhook") is True
    assert http_only.matches("read_file", frozenset({"operator"}), "webhook") is False
    assert http_only.matches("read_file", frozenset({"operator"}), "http") is True


@pytest.mark.silent
def test_trailing_wildcard_matches_a_prefix_case_insensitively() -> None:
    """`browser_*` is a PREFIX, not a substring, and case never decides a policy verdict.

    A rule stored as `browser_*` that stops applying because a provider advertises
    `Browser_Click` is a policy hole that no other test would notice.
    """
    browser = PolicyRule(
        rule_id="r-browser",
        tool_pattern="browser_*",
        effect=Effect.NEEDS_APPROVAL,
        reason="A human confirms browser actions.",
    )
    assert browser.matches("browser_click", frozenset(), "http") is True
    assert browser.matches("BROWSER_CLICK", frozenset(), "http") is True
    assert browser.matches("Browser_Click", frozenset(), "http") is True
    assert browser.matches("browser_", frozenset(), "http") is True
    # A prefix, not a substring: a hostile name must not inherit the rule by containing it.
    assert browser.matches("mcp_browser_click", frozenset(), "http") is False
    assert browser.matches("browse", frozenset(), "http") is False

    # The pattern itself is matched case-insensitively too, in both directions.
    exact = PolicyRule(
        rule_id="r-exact",
        tool_pattern="Run_Shell",
        effect=Effect.DENY,
        reason="Never from this channel.",
    )
    assert exact.matches("run_shell", frozenset(), "http") is True
    assert exact.matches("RUN_SHELL", frozenset(), "http") is True
    # Exact means exact - no accidental prefix behaviour without the wildcard.
    assert exact.matches("run_shell_v2", frozenset(), "http") is False


@pytest.mark.silent
def test_a_ruleset_cannot_answer_for_a_different_caller() -> None:
    """A RuleSet IS the answer for exactly one caller's narrowing.

    Passing `caller` alongside the snapshot let a caller-A snapshot answer a question
    about caller B - silently, with no type error and no failing test, which in a policy
    engine means a tool gets allowed for someone who should not have it. The snapshot
    carries the roles and the channel it was loaded for, so the wrong-caller question is
    not answered wrongly: it cannot be asked.
    """
    transfer = PolicyRule(
        rule_id="r-transfer",
        tool_pattern="transfer_funds",
        effect=Effect.ALLOW,
        reason="Treasury operations from an interactive channel.",
        subject_roles=frozenset({"treasurer"}),
        channels=frozenset({"http"}),
    )

    for_treasurer = RuleSet.for_caller(TREASURER, rules=(transfer,))
    assert for_treasurer.subject_roles == TREASURER.roles
    assert for_treasurer.channel == TREASURER.channel
    assert for_treasurer.applicable("transfer_funds") == (transfer,)

    # The intern's snapshot over the SAME rows answers differently, on its own narrowing.
    for_intern = RuleSet.for_caller(INTERN, rules=(transfer,))
    assert for_intern.applicable("transfer_funds") == ()

    # The wrong-caller question is inexpressible: `applicable` takes the tool name only.
    with pytest.raises(TypeError):
        for_intern.applicable(  # type: ignore[call-arg]
            "transfer_funds", TREASURER.roles, TREASURER.channel
        )

    # And the narrowing cannot be left off at construction time either.
    with pytest.raises(TypeError):
        RuleSet(rules=(transfer,))  # type: ignore[call-arg]


# "store unreachable -> DENY, and surface the error" is NOT tested in this module, and its
# absence here is deliberate rather than a gap. Unreachability is an ADAPTER event: the
# domain has no store to lose, so the only shape it can express is the empty snapshot -
# already asserted at the end of `test_no_matching_rule_denies` above, which is exactly
# what `load_rules` returns when Postgres is down. Everything the property adds on top of
# that shape - the failure being caught rather than raised, the error reaching `on_error`
# instead of being swallowed, the empty snapshot then refusing a tool the reachable store
# would have allowed - needs the adapter to be observable, and is asserted against it in
# tests/unit/test_policy_repository.py. Restating it here with a hand-built RuleSet would
# assert the fixture, not the fail-closed path, and read as coverage it does not have.


@pytest.mark.silent
@pytest.mark.skip(reason="F6")
def test_mcp_tool_obeys_the_same_policy_as_a_local_tool() -> None:
    """CLAUDE.md non-negotiable #4. An MCP server is third-party code."""


@pytest.mark.silent
@pytest.mark.skip(reason="F6")
def test_mcp_server_cannot_shadow_a_local_tool_name() -> None:
    """Register a hostile server advertising `write_file`. The local tool must win."""
