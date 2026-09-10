"""Compaction - a SILENT-BUG AREA. Wrong here shows up on the BILL, never as a red test.

The pairing test is the one that prevents a class of provider 400s that surface hours
after the compaction that caused them.

The ladder tests below (t-f5-01) guard the other half of the bill. Every rung climbed
rewrites more of the prompt prefix and invalidates more of the provider cache, so a
ladder that keeps climbing after the target is already met pays twice for nothing. That
overshoot frees MORE tokens, so no assertion about headroom would ever catch it - which
is exactly why the assertions here are about WHICH rungs ran, not about how much they
freed.
"""

from collections.abc import Mapping

import pytest

from agent_core.domain.compaction import (
    CompactionPolicy,
    Rung,
    climb_ladder,
)


class _Ladder:
    """Records which rungs were actually climbed and how many tokens each one freed.

    Standing in for `adapters/driven/context/engine.py` (t-f5-04): the domain decides
    WHETHER to climb, the adapter decides WHAT a rung does to a history. The recording is
    the whole point - `attempted` is the evidence that a rung the ladder did not need was
    never paid for.
    """

    def __init__(self, tokens: int, frees: Mapping[Rung, int]) -> None:
        self.tokens = tokens
        self._frees = frees
        self.attempted: list[Rung] = []

    def __call__(self, rung: Rung) -> int:
        self.attempted.append(rung)
        self.tokens -= self._frees.get(rung, 0)
        return self.tokens


WINDOW = 100_000
# CompactionPolicy.target_fraction defaults to 0.40, so the target is 40_000 tokens.


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_reaching_target_on_the_first_rung_stops_the_ladder_immediately() -> None:
    """L1 is free and often enough on its own. Climbing past it would spend a model call
    and rewrite more of the prefix for headroom that was already bought."""
    policy = CompactionPolicy()
    ladder = _Ladder(90_000, {Rung.L1_PRUNE_TOOL_OUTPUT: 60_000})

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [Rung.L1_PRUNE_TOOL_OUTPUT]
    assert run.rungs_applied == (Rung.L1_PRUNE_TOOL_OUTPUT,)
    assert run.reached_target is True
    assert run.tokens_after == 30_000


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_rung_is_climbed_only_after_the_previous_one_missed_target() -> None:
    """The two halves of the ladder rule in one run: L2 is reached because L1 fell short,
    and L3/L4 - the two rungs that cost a model call - are never reached because L2 did
    not fall short."""
    policy = CompactionPolicy()
    ladder = _Ladder(
        90_000,
        {Rung.L1_PRUNE_TOOL_OUTPUT: 20_000, Rung.L2_SLIDING_WINDOW: 40_000},
    )

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L2_SLIDING_WINDOW]
    assert run.rungs_applied == (Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L2_SLIDING_WINDOW)
    assert run.reached_target is True
    assert run.tokens_after == 30_000


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_history_already_within_target_climbs_no_rung_at_all() -> None:
    """Zero rungs is a legitimate outcome and the cheapest one there is. A pass that
    rewrites the prefix to free tokens nobody needed is pure loss - it costs a full
    re-billed prompt on the next request and buys nothing."""
    policy = CompactionPolicy()
    ladder = _Ladder(10_000, {Rung.L1_PRUNE_TOOL_OUTPUT: 5_000})

    run = climb_ladder(
        policy, tokens_before=10_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == []
    assert run.rungs_applied == ()
    assert run.reached_target is True
    assert run.tokens_after == 10_000


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_ladder_ends_when_the_rungs_run_out_without_reaching_target() -> None:
    """`reached_target is False` is what the caller escalates on. It must NOT be
    confused with no progress: tokens did fall, the ladder simply has nothing cheaper
    left to try."""
    policy = CompactionPolicy()
    ladder = _Ladder(90_000, dict.fromkeys(Rung, 5_000))

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L3_SUMMARISE_MIDDLE,
        Rung.L4_ITERATIVE_RESUMMARY,
    ]
    assert run.reached_target is False
    assert run.tokens_after == 70_000
    assert run.made_progress is True


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_rung_the_profile_disabled_is_never_climbed() -> None:
    """A profile that switches off the summarising rungs must not have them climbed
    behind its back - that is a model call the operator declined to pay for."""
    policy = CompactionPolicy(
        enabled_rungs=(Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L3_SUMMARISE_MIDDLE)
    )
    ladder = _Ladder(
        90_000,
        {Rung.L1_PRUNE_TOOL_OUTPUT: 1_000, Rung.L3_SUMMARISE_MIDDLE: 60_000},
    )

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L3_SUMMARISE_MIDDLE]
    assert run.reached_target is True


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_rungs_are_climbed_cheapest_first_whatever_order_the_profile_declared() -> None:
    """The ladder's order is a property of the ladder, not of how a YAML file happened to
    list it. A profile listing L4 first must not buy a summary before trying free pruning
    - and because compaction runs inside a DBOS step, a declaration-ordered climb would
    also replay differently after a crash."""
    policy = CompactionPolicy(
        enabled_rungs=(
            Rung.L4_ITERATIVE_RESUMMARY,
            Rung.L2_SLIDING_WINDOW,
            Rung.L1_PRUNE_TOOL_OUTPUT,
        )
    )
    ladder = _Ladder(90_000, dict.fromkeys(Rung, 1_000))

    climb_ladder(policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder)

    assert ladder.attempted == [
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L4_ITERATIVE_RESUMMARY,
    ]


@pytest.mark.silent
@pytest.mark.skip(reason="F5 - docs/TASKS.md#t-f5-02")
def test_tool_call_and_return_stay_paired_after_every_rung() -> None:
    """PSEUDO-CODE - implement in F5. Run this against L1, L2, L3 and L4 separately.

    1. Build a history with interleaved tool calls and returns.
    2. Apply one rung.
    3. Assert every remaining tool call has its return and vice versa.

    A broken pair makes the provider reject the whole conversation. CLAUDE.md #5.
    """


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_should_compress_falls_back_when_window_used_is_none() -> None:
    """`context_window_used` is None on providers that do not report usage - COMMON, not
    exceptional. Without the local estimate fallback the agent never compacts and dies of
    overflow in production, never in tests."""


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_head_and_tail_are_never_summarised() -> None:
    """The head carries the task definition. Summarising it is how an agent forgets what
    it was asked to do."""


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_l4_folds_the_previous_summary_instead_of_appending() -> None:
    """This is what keeps the summary current rather than frozen, and stops it repeating
    what it already said."""


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_no_progress_does_not_retry() -> None:
    """A compaction that frees nothing and is retried is an infinite loop that ALSO
    invalidates the prompt cache on every pass - the most expensive failure this system
    has."""


@pytest.mark.skip(reason="F5 - the acceptance criterion for the whole phase")
def test_cost_per_turn_stays_flat_over_two_hundred_turns() -> None:
    """PSEUDO-CODE - implement in F5 with a recording fake model.

    Simulate 200 turns. Assert cost per turn does not TREND UP.

    If it rises after enabling compaction, the trigger is too low and the prompt cache is
    being destroyed on every pass. Raise the trigger and the target. Do not add rungs.
    """
