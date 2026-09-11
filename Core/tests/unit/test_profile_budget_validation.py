"""A profile's BOUNDS are refused at LOAD when they cannot bound anything.

Tasks: docs/TASKS.md#t-f11-32, docs/TASKS.md#t-f11-37

THREE FIELDS, ONE RULE
    `result_budget_chars` (t-f11-32) caps how much untrusted text from one MCP server
    reaches the model. `max_iterations` and `max_cost_usd` (t-f11-37) cap how long a turn
    runs and what it may spend. All three are ceilings, and a ceiling written in a way the
    comparison cannot use is not a low ceiling - it is no ceiling.

    They fail in opposite directions, and the second is the dangerous one:

        max_iterations: 0    ->  FAILS CLOSED. `used_iterations >= max_iterations` is
                                 `0 >= 0`, true before the first step, so the profile
                                 parses and cannot serve a single turn. Annoying, loud.
        max_cost_usd: nan    ->  FAILS OPEN. `Decimal("nan")` parses happily, and EVERY
                                 comparison against NaN is `False`, so
                                 `max_cost_usd > 0 and spent_usd >= max_cost_usd` is
                                 permanently `False`: `cost_exhausted` can never become
                                 true and the agent runs with NO SPEND LIMIT. Silent,
                                 unbounded, and surfacing only on the bill - which is the
                                 failure mode CLAUDE.md's silent-bug table exists to name.

    `domain/profile.py` already knew this rule and never applied it to itself: `_decimal`
    rejects a non-finite LITERAL in an approval condition because "a condition that
    silently never matches is precisely the fail-open this module exists to prevent". The
    same parser now guards the profile's own money field, rather than a second finiteness
    check growing beside it - two validators for one idea is how the strict one gets
    bypassed.

WHY THIS TEST EXISTS AT ALL
    The number was inert for six phases. `budget_for` decided from the tool-name prefix
    and nothing ever read the per-server value, so any integer a profile wrote was equally
    harmless - and validating it would have looked like ceremony over a field with no
    reader.

    `t-f11-25` gave it a reader. `budget_for` now asks `result_budget_for` for the
    server's number and `wrap_untrusted` does `body[:budget]` with it, which means every
    value the field can hold is suddenly reachable:

        budget = -100   ->  omitted = len(body) + 100, so the notice claims MORE
                            characters were removed than the body ever held, while
                            `body[:-100]` silently drops the last 100 characters of an
                            untrusted payload. The accounting we print outside the fence,
                            which exists precisely so a reader can trust our count over
                            the payload's, becomes a number with no relationship to what
                            was cut.
        budget = 0      ->  the fence is emitted empty on every call. Silencing a server
                            is `tool_exclude`'s job, done at discovery, where an operator
                            can see it.
        budget = "50k"  ->  `min(str, int)` raises TypeError inside a tool result, from a
                            typo in a YAML file, mid-turn.

    INERT CONFIGURATION IS UNVALIDATED CONFIGURATION WAITING FOR ITS FIRST READER.

WHY AT LOAD, AND NOT AT USE
    `t-f11-05` made an unservable profile fail at load rather than at turn three. Same
    argument here, and it is the cheaper end: a budget is only consulted when an MCP tool
    actually returns something, so a profile carrying a broken one starts, serves, and
    misreports an untrusted result the first time a third-party server replies. The load
    is where an operator is still watching.

These tests hand the domain a plain mapping and never touch a disk - `domain/profile.py`
imports nothing external and knows nothing about files (CLAUDE.md, "Layer rules").
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

# Imported as a MODULE so a name that does not exist yet fails inside the test that needs
# it, rather than failing this whole file at collection - a collection error is not a red
# test.
import agent_core.domain.profile as profile_module


def _profile_with_budget(budget: object) -> dict[str, Any]:
    """The smallest profile that carries one MCP server with `budget` on it."""
    server: dict[str, Any] = {
        "name": "files",
        "transport": "stdio",
        "command": "files-mcp",
        "result_budget_chars": budget,
    }
    return {
        "id": "budget-probe",
        "persona": "A profile that exists only to carry one MCP server reference.",
        "model": "minimax/MiniMax-M3",
        "mcp_servers": [server],
    }


@pytest.mark.parametrize(
    "budget",
    [
        pytest.param(-1, id="minus-one"),
        pytest.param(-100, id="the-tail-slicer"),
        pytest.param(0, id="zero"),
    ],
)
def test_a_budget_that_cannot_bound_anything_is_refused_at_load(budget: int) -> None:
    """A non-positive budget never reaches `wrap_untrusted`, because loading stops first.

    Zero is refused alongside the negatives on purpose. It is arithmetically harmless -
    `body[:0]` is honestly empty and the notice counts honestly - but it makes every
    result from that server an empty fence plus a truncation notice, which is an agent
    quietly blinded by configuration. A server nobody wants to hear from is excluded at
    discovery, where the profile says so out loud.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(_profile_with_budget(budget))


@pytest.mark.parametrize(
    "budget",
    [
        pytest.param("50000", id="quoted-integer"),
        pytest.param(1024.0, id="float"),
        pytest.param(True, id="bool-which-is-an-int-in-python"),
        pytest.param(None, id="explicit-null"),
        pytest.param([50_000], id="list"),
    ],
)
def test_a_budget_that_is_not_an_integer_is_refused_at_load(budget: object) -> None:
    """`min(budget, MCP_RESULT_BUDGET_CHARS)` has to be able to run, and so does the slice.

    Nothing coerces this field - unlike `max_iterations` and the rest, which go through
    `int(...)` - so `result_budget_chars: "50000"` from a quoted YAML scalar reaches
    `min()` as a string and raises `TypeError` mid-turn, inside the handling of an
    untrusted result. `True` is refused too: it is an `int` in Python and would become a
    one-character budget, which is a typo the language would otherwise accept silently.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(_profile_with_budget(budget))


def test_the_refusal_names_the_server_and_the_value() -> None:
    """An operator has to be able to find the line. There may be several servers.

    `_reject_unknown_keys` already sets the shape this file refuses in: say where, and say
    what. A message reading "invalid budget" against a profile with four MCP servers sends
    someone hunting through a file the loader had already read.
    """
    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(_profile_with_budget(-100))

    message = str(raised.value)
    assert "files" in message, "the refusal must name the server whose budget is bad"
    assert "-100" in message, "the refusal must quote the value it refused"


def test_a_positive_budget_still_loads_and_is_carried_through() -> None:
    """The contrast case, without which a loader that refused EVERY budget would pass.

    A validation test that only ever asserts refusal is satisfied by `raise` on line one.
    """
    profile = profile_module.AgentProfile.from_mapping(_profile_with_budget(8_000))

    assert profile.mcp_servers[0].result_budget_chars == 8_000


def test_an_omitted_budget_still_takes_the_default() -> None:
    """Validation must not turn an optional field into a required one.

    Every shipped profile that declares an MCP server without a budget keeps loading, and
    keeps the deliberately-lower-than-local default the class docstring explains.
    """
    mapping = _profile_with_budget(0)
    del mapping["mcp_servers"][0]["result_budget_chars"]

    profile = profile_module.AgentProfile.from_mapping(mapping)

    assert profile.mcp_servers[0].result_budget_chars == 50_000


# ---------------------------------------------------------------------------
# t-f11-37: the profile's own two budgets.
# ---------------------------------------------------------------------------


def _profile_with(**overrides: object) -> dict[str, Any]:
    """The smallest loadable profile, plus whatever budget key a test is probing."""
    mapping: dict[str, Any] = {
        "id": "budget-probe",
        "persona": "A profile that exists only to carry a budget.",
        "model": "minimax/MiniMax-M3",
    }
    mapping.update(overrides)
    return mapping


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("nan", id="nan"),
        pytest.param("NaN", id="NaN-as-yaml-would-spell-it"),
        pytest.param("-nan", id="negative-nan"),
        pytest.param("sNaN", id="signalling-nan"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("inf", id="inf"),
        pytest.param("-Infinity", id="negative-infinity"),
        pytest.param(float("nan"), id="float-nan-not-a-string"),
        pytest.param(float("inf"), id="float-inf-not-a-string"),
    ],
)
def test_a_non_finite_cost_ceiling_is_refused_at_load(amount: object) -> None:
    """The fail-open this module's own `_decimal` docstring already forbids.

    `from_mapping` does `Decimal(str(...))`, and `Decimal("nan")` parses without
    complaint. `domain/budget.py` then asks
    `self.max_cost_usd > 0 and self.spent_usd >= self.max_cost_usd`, and every comparison
    against NaN is `False` - so `cost_exhausted` is permanently `False` and the turn has no
    spend ceiling at all. `Infinity` is the honest spelling of the same thing and is
    refused with it: a profile that means "do not budget cost" says so with a number, not
    with a value that makes the comparison meaningless.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(_profile_with(max_cost_usd=amount))


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("plenty", id="a-word"),
        pytest.param("0.25 USD", id="an-amount-with-its-currency"),
        pytest.param("", id="empty-string"),
        pytest.param(None, id="explicit-null"),
        pytest.param(True, id="bool"),
        pytest.param(["0.25"], id="list"),
    ],
)
def test_a_cost_ceiling_that_is_not_a_number_fails_as_a_profile_error(amount: object) -> None:
    """The failure has to arrive as a malformed profile, not as `decimal.InvalidOperation`.

    `Decimal(str(mapping.get(...)))` raises `InvalidOperation` out of the loader today, so
    an operator with a typo in a YAML file gets an arithmetic error naming neither the file
    nor the key. Every other malformed value in this module is a `ProfileValidationError`,
    and the adapter that reads the file catches that one.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(_profile_with(max_cost_usd=amount))


def test_the_cost_refusal_names_the_field_and_the_value() -> None:
    """An operator has to be able to find the line, exactly as for `result_budget_chars`."""
    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(_profile_with(max_cost_usd="nan"))

    message = str(raised.value)
    assert "max_cost_usd" in message, "the refusal must name the field it refused"
    assert "nan" in message.lower(), "the refusal must quote the value it refused"


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("0.25", id="quoted-decimal-as-every-shipped-profile-writes-it"),
        pytest.param(2, id="plain-integer"),
        pytest.param("1E+2", id="exponent-notation"),
    ],
)
def test_a_finite_cost_ceiling_still_loads_and_keeps_its_exact_value(amount: object) -> None:
    """The contrast case, and the one that pins the quoted form the shipped profiles use.

    Every profile in `Core/profiles/` writes `max_cost_usd: "0.25"` QUOTED, because a YAML
    float does not hold a decimal amount exactly. Validating finiteness must not turn that
    into a type check that rejects text - what is refused is text that is not a finite
    number, never text as such.
    """
    profile = profile_module.AgentProfile.from_mapping(_profile_with(max_cost_usd=amount))

    assert profile.max_cost_usd == Decimal(str(amount))


def test_an_omitted_cost_ceiling_still_takes_the_default() -> None:
    """Validation must not turn an optional field into a required one."""
    profile = profile_module.AgentProfile.from_mapping(_profile_with())

    assert profile.max_cost_usd == Decimal("1.00")


@pytest.mark.parametrize(
    "iterations",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="minus-one"),
        pytest.param(-5, id="the-one-that-is-exhausted-before-it-starts"),
    ],
)
def test_an_iteration_ceiling_that_serves_no_turn_is_refused_at_load(iterations: int) -> None:
    """Fails CLOSED, which is why it is only annoying - and still refused at load.

    `iterations_exhausted` is `used_iterations >= max_iterations`, so `0 >= 0` and
    `0 >= -5` are both true before the first step runs. The profile parses, assigns a
    version, and dies on every turn it is ever given: the same shape as t-f11-05, where an
    unservable profile now fails at LOAD rather than at turn three.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(_profile_with(max_iterations=iterations))


@pytest.mark.parametrize(
    "iterations",
    [
        pytest.param("25", id="quoted-integer"),
        pytest.param(25.0, id="float"),
        pytest.param(True, id="bool-which-is-an-int-in-python"),
        pytest.param(None, id="explicit-null"),
    ],
)
def test_an_iteration_ceiling_that_is_not_an_integer_is_refused_at_load(
    iterations: object,
) -> None:
    """Nothing coerces a bound, for the reason `_require_positive_int` already writes down.

    `int("25")` turns a quoted scalar into a plausible value and `int(True)` turns a YAML
    `true` into a budget of one iteration. A ceiling reached by a coercion nobody wrote
    down is exactly what that helper exists to refuse, and `max_iterations` is the field it
    was named after.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(_profile_with(max_iterations=iterations))


def test_the_iteration_refusal_names_the_field_and_the_value() -> None:
    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(_profile_with(max_iterations=0))

    message = str(raised.value)
    assert "max_iterations" in message, "the refusal must name the field it refused"
    assert "0" in message, "the refusal must quote the value it refused"


def test_a_positive_iteration_ceiling_still_loads_and_an_omitted_one_defaults() -> None:
    """The contrast case, without which a loader that refused EVERY ceiling would pass."""
    declared = profile_module.AgentProfile.from_mapping(_profile_with(max_iterations=15))
    omitted = profile_module.AgentProfile.from_mapping(_profile_with())

    assert declared.max_iterations == 15
    assert omitted.max_iterations == 25


# ---------------------------------------------------------------------------
# Reported by the anchor before this one: a required key missing from a nested
# block leaves the loader as a bare `KeyError`, not a `ProfileValidationError`.
# `_build_agent_ref` already solved this for peers; these two blocks now follow it.
# The tests live here because this wave owns no other test module for this file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["name", "transport"])
def test_an_mcp_server_missing_a_required_key_is_a_profile_error(missing: str) -> None:
    """A `KeyError('name')` out of a loader names neither the file nor the block."""
    mapping = _profile_with_budget(8_000)
    del mapping["mcp_servers"][0][missing]

    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(mapping)

    assert missing in str(raised.value)


@pytest.mark.parametrize("missing", ["tool_name", "reason"])
def test_an_approval_rule_missing_a_required_key_is_a_profile_error(missing: str) -> None:
    """An approval rule is an authorisation record; its two keys are not defaultable."""
    rule: dict[str, Any] = {"tool_name": "issue_refund", "reason": "money leaves"}
    del rule[missing]

    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(_profile_with(approval_rules=[rule]))

    assert missing in str(raised.value)
