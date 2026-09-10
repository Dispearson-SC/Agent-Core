"""Compaction - a SILENT-BUG AREA. Wrong here shows up on the BILL, never as a red test.

The pairing test is the one that prevents a class of provider 400s that surface hours
after the compaction that caused them.
"""

import pytest


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
