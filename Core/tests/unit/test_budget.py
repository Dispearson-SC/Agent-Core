"""Budget guards - the wrap-up notice fires ONCE per turn, not once per iteration.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-03

The whole point of these tests is the second half of the assertion. A `wrapup_notice_due`
that simply answers "am I past the threshold?" passes a naive check and then re-injects the
notice on every remaining iteration - burning the very budget it exists to protect. That
mistake produces no error, only a bigger bill, so the loops below deliberately run several
iterations past the threshold and assert the notice never comes back.
"""

from decimal import Decimal

import pytest

from agent_core.domain.budget import BudgetState


def _iterations_where_notice_fired(state: BudgetState, steps: int) -> list[int]:
    """Drive the budget `steps` times and record which iterations asked for the notice."""
    fired: list[int] = []
    for _ in range(steps):
        state = state.consume()
        if state.wrapup_notice_due():
            fired.append(state.used_iterations)
    return fired


@pytest.mark.phase("F1")
def test_iteration_notice_fires_once_at_the_crossing_and_never_again() -> None:
    state = BudgetState(max_iterations=10)

    # Ten iterations: the threshold is crossed at the eighth and six more follow it.
    assert _iterations_where_notice_fired(state, steps=10) == [8]


@pytest.mark.phase("F1")
def test_notice_is_not_due_before_the_threshold() -> None:
    state = BudgetState(max_iterations=10)
    for _ in range(7):
        state = state.consume()

    assert state.used_iterations == 7
    assert state.wrapup_notice_due() is False


@pytest.mark.phase("F1")
def test_a_single_large_consume_that_jumps_the_threshold_still_fires_once() -> None:
    state = BudgetState(max_iterations=10)

    state = state.consume(iterations=5)
    assert state.wrapup_notice_due() is False

    state = state.consume(iterations=5)
    assert state.wrapup_notice_due() is True

    # Past the end of the budget the notice must stay silent.
    state = state.consume(iterations=5)
    assert state.wrapup_notice_due() is False


@pytest.mark.phase("F1")
def test_cost_crossing_fires_once_even_while_iterations_stay_cheap() -> None:
    state = BudgetState(max_iterations=1000, max_cost_usd=Decimal("1.00"))
    fired: list[Decimal] = []
    for _ in range(10):
        state = state.consume(cost_usd=Decimal("0.10"))
        if state.wrapup_notice_due():
            fired.append(state.spent_usd)

    assert fired == [Decimal("0.80")]


@pytest.mark.phase("F1")
def test_threshold_is_configurable_and_still_fires_once() -> None:
    state = BudgetState(max_iterations=10)
    fired: list[int] = []
    for _ in range(10):
        state = state.consume()
        if state.wrapup_notice_due(threshold=0.5):
            fired.append(state.used_iterations)

    assert fired == [5]


@pytest.mark.phase("F1")
def test_asking_twice_about_the_same_state_gives_the_same_answer() -> None:
    """The method is pure: it reports a crossing, it does not consume a token.

    Once-per-turn comes from the state advancing, never from counting calls - a method
    that mutated on read would double-fire on DBOS replay.
    """
    state = BudgetState(max_iterations=10)
    for _ in range(8):
        state = state.consume()

    assert state.wrapup_notice_due() is True
    assert state.wrapup_notice_due() is True


@pytest.mark.phase("F1")
def test_a_budget_with_no_cost_ceiling_never_fires_on_cost() -> None:
    state = BudgetState(max_iterations=1000)
    for _ in range(10):
        state = state.consume(cost_usd=Decimal("50.00"))
        assert state.wrapup_notice_due() is False
