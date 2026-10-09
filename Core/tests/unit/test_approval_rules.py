"""`AgentProfile.requires_approval_for()` - and the one way it is allowed to be wrong.

Tasks: docs/TASKS.md#t-f4-02

THE ASSERTION THIS FILE EXISTS FOR
    A condition nobody can parse must mean APPROVE.

    An approval rule is written by hand in a YAML file by whoever governs the vertical, so
    a typo in it is not a hypothetical. The failure that matters is the quiet one: a rule
    the evaluator cannot read gets skipped, `requires_approval_for` returns `None`, and a
    tool call that a human was supposed to see executes unattended. Nothing raises,
    nothing is logged, and the profile still looks like it has an approval rule in it.

    So every unreadable condition - bad syntax, unknown operator, a field the call did not
    pass, a comparison between values that cannot be ordered - returns the RULE, not
    `None`. A typo makes the system more cautious, never less. `domain/profile.py` says the
    same thing in `requires_approval_for`'s docstring; this file is what holds it there.

`profile_module` is imported as a MODULE so a name missing at import time fails inside the
test that needs it rather than at collection, exactly as `test_profile.py` does it.
"""

from __future__ import annotations

from typing import Any

import pytest

import agent_core.domain.profile as profile_module

pytestmark = [pytest.mark.phase("F4"), pytest.mark.silent]


def _profile(*rules: Any) -> Any:
    """A minimal profile carrying `rules` and nothing else that could interfere."""
    return profile_module.AgentProfile(
        id="delivery_optimizer",
        persona="p",
        model="m",
        approval_rules=tuple(rules),
    )


def _rule(condition: str | None, *, tool_name: str = "pricing_apply") -> Any:
    return profile_module.ApprovalRule(
        tool_name=tool_name,
        reason="Price change above 15% needs a human.",
        condition=condition,
    )


# --------------------------------------------------------------------------------------
# The fail-safe. This is the anchor.
# --------------------------------------------------------------------------------------

UNREADABLE_CONDITIONS = [
    "",  # empty
    "   ",  # whitespace only
    "pct_change",  # no operator at all
    "pct_change >",  # no value
    "> 15",  # no field
    "pct_change <> 15",  # operator that does not exist
    "pct_change === 15",  # operator that does not exist
    "abs(pct_change > 15",  # unbalanced parenthesis
    "pct_change > fifteen",  # value that is not a literal
    "__import__('os') > 0",  # not a field, and never to be executed
    "pct_change > 15 and region == 'north'",  # a grammar this rule set does not have
    "max(pct_change) > 15",  # a function that is not `abs`
]


@pytest.mark.parametrize("condition", UNREADABLE_CONDITIONS)
def test_unparseable_condition_requires_approval(condition: str) -> None:
    """An unreadable condition returns the rule. Never `None`, never an exception.

    Returning `None` here is the silent bug: the tool runs with nobody watching."""
    rule = _rule(condition)
    profile = _profile(rule)

    decision = profile.requires_approval_for("pricing_apply", {"pct_change": 3})

    assert decision is not None, (
        f"unreadable condition {condition!r} returned None - "
        "an approval rule nobody can parse became a rule nobody enforces"
    )
    assert decision is rule


def test_unknown_field_requires_approval() -> None:
    """The condition parses, but the call did not pass the field it names.

    Same class of failure: the rule cannot be decided, so it is not decided AGAINST the
    human. Reading a missing field as "absent, therefore not greater than 15" is exactly
    the permissive default this task exists to forbid."""
    rule = _rule("abs(pct_change) > 15")
    profile = _profile(rule)

    assert profile.requires_approval_for("pricing_apply", {}) is rule
    assert profile.requires_approval_for("pricing_apply", {"other": 99}) is rule


def test_uncomparable_types_require_approval() -> None:
    """`"north" > 15` has no answer, so the answer is: ask a human."""
    rule = _rule("pct_change > 15")
    profile = _profile(rule)

    assert profile.requires_approval_for("pricing_apply", {"pct_change": "north"}) is rule
    assert profile.requires_approval_for("pricing_apply", {"pct_change": None}) is rule


def test_a_number_sent_as_text_requires_approval() -> None:
    """No silent coercion. `"3"` is not decided to be below the threshold.

    Coercing it would turn a caller's formatting choice into an unattended tool call.
    Refusing turns it into a prompt a human can answer in a second."""
    rule = _rule("abs(pct_change) > 15")
    profile = _profile(rule)

    assert profile.requires_approval_for("pricing_apply", {"pct_change": "3"}) is rule
    assert profile.requires_approval_for("pricing_apply", {"pct_change": True}) is rule


def test_a_readable_false_condition_is_the_only_way_past() -> None:
    """The contrast that gives the fail-safe its meaning.

    If nothing ever returned `None` the fail-safe would be trivially satisfied by a method
    that always approves, and the rule set would stop being configuration."""
    profile = _profile(_rule("abs(pct_change) > 15"))

    assert profile.requires_approval_for("pricing_apply", {"pct_change": 3}) is None
    assert profile.requires_approval_for("pricing_apply", {"pct_change": -3}) is None


# --------------------------------------------------------------------------------------
# The ordinary behaviour the fail-safe sits on top of.
# --------------------------------------------------------------------------------------


def test_condition_true_matches() -> None:
    profile = _profile(_rule("abs(pct_change) > 15"))

    assert profile.requires_approval_for("pricing_apply", {"pct_change": 20}) is not None
    assert profile.requires_approval_for("pricing_apply", {"pct_change": -20}) is not None


def test_no_condition_always_matches() -> None:
    """`condition: null` is "every call to this tool needs a human"."""
    rule = _rule(None)
    profile = _profile(rule)

    assert profile.requires_approval_for("pricing_apply", {}) is rule


def test_a_tool_with_no_rule_needs_no_approval() -> None:
    profile = _profile(_rule(None))

    assert profile.requires_approval_for("route_plan", {}) is None


def test_trailing_wildcard_matches_the_tool_name() -> None:
    """Same matching rule `PolicyRule.tool_pattern` uses: trailing `*`, case-insensitive."""
    rule = _rule(None, tool_name="pricing_*")
    profile = _profile(rule)

    assert profile.requires_approval_for("pricing_apply", {}) is rule
    assert profile.requires_approval_for("PRICING_APPLY", {}) is rule
    assert profile.requires_approval_for("route_plan", {}) is None


def test_first_matching_rule_wins() -> None:
    """Declaration order decides, so a profile reads top to bottom like the file it is."""
    first = _rule(None, tool_name="pricing_*")
    second = _rule(None, tool_name="pricing_apply")
    profile = _profile(first, second)

    assert profile.requires_approval_for("pricing_apply", {}) is first


def test_an_unreadable_rule_does_not_hide_a_later_one() -> None:
    """The fail-safe returns the unreadable rule itself - approval is still required."""
    broken = _rule("pct_change !! 15", tool_name="pricing_apply")
    healthy = _rule(None, tool_name="pricing_apply")
    profile = _profile(broken, healthy)

    assert profile.requires_approval_for("pricing_apply", {"pct_change": 1}) is broken


def test_operators_and_literals_the_grammar_accepts() -> None:
    """The grammar is deliberately tiny: `field op literal`, optionally `abs(field)`.

    Every case here must be READABLE, otherwise the fail-safe above would pass it for the
    wrong reason - a grammar that parses nothing satisfies "unreadable means approve"
    perfectly and enforces nothing."""
    cases: list[tuple[str, dict[str, object], bool]] = [
        ("pct_change > 15", {"pct_change": 16}, True),
        ("pct_change >= 15", {"pct_change": 15}, True),
        ("pct_change < 15", {"pct_change": 14}, True),
        ("pct_change <= 15", {"pct_change": 15}, True),
        ("pct_change == 15", {"pct_change": 15}, True),
        ("pct_change != 15", {"pct_change": 16}, True),
        ("amount > 1000.5", {"amount": 1000.6}, True),
        ("region == 'north'", {"region": "north"}, True),
        ('region == "north"', {"region": "north"}, True),
        ("region != 'north'", {"region": "north"}, False),
        ("dry_run == false", {"dry_run": False}, True),
        ("dry_run == true", {"dry_run": False}, False),
    ]
    for condition, arguments, expected_match in cases:
        rule = _rule(condition)
        decision = _profile(rule).requires_approval_for("pricing_apply", arguments)
        assert (decision is rule) is expected_match, f"{condition!r} against {arguments!r}"


def test_the_condition_is_never_executed_as_python() -> None:
    """A profile is configuration. Configuration that executes code is an RCE vector.

    The class docstring says so; this makes it a test. If the evaluator ever grew an
    `eval`, this call would mutate `witness` and the assertion would catch it."""
    witness: list[str] = []

    class Probe:
        def __gt__(self, other: object) -> bool:
            witness.append("compared via python")
            return True

    rule = _rule("probe > 15")
    decision = _profile(rule).requires_approval_for("pricing_apply", {"probe": Probe()})

    # A non-numeric operand is uncomparable, so the fail-safe returns the rule - WITHOUT
    # ever handing the value to Python's own comparison machinery.
    assert decision is rule
    assert witness == []
