"""The ladder engine - a SILENT-BUG AREA (CLAUDE.md). Wrong here shows up on the BILL.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-04, docs/TASKS.md#t-f5-11

WHAT THIS MODULE PINS, AND WHY NEITHER PROPERTY HAS ANY OTHER WITNESS

    1. `compress` STOPS AT THE FIRST RUNG THAT MEETS `target_fraction`.
       L3 and L4 each cost a summariser call. A ladder that keeps climbing after the
       target is already met frees MORE tokens, so it looks better on every measure the
       system reports - shorter history, more headroom - and worse on the only one that
       matters. It also rewrites more of the prompt prefix, and a rewritten prefix
       re-bills the whole prompt at full price on the next request. `domain/compaction.py`
       proves the DECISION with `climb_ladder`; nothing until this module proved that the
       adapter actually routes its rungs through it, or that a rung which met the target
       stops the model call behind the next one.

    2. `compress` MUTATES NONE OF ITS INPUTS.
       The port says in as many words that `compress` returns a result and does not touch
       the live conversation, and that this separation is what lets compaction run inside
       a `@DBOS.step()` and be discarded safely when the step retries. Pydantic AI's
       message parts are MUTABLE dataclasses, so pruning a tool result in place is the
       natural way to write L1 and it passes every assertion about the returned history.
       The damage only appears when DBOS retries the step: the second attempt starts from
       a history the first attempt already shortened, so the ladder compounds silently and
       the conversation loses content nobody asked it to lose. No retry happens in a unit
       suite, which is exactly why this has to be asserted directly.

    A third test guards the first one's integrity: "stops at the first rung" is only
    evidence if the ladder can be shown to CLIMB when the first rung falls short.

    3. `estimated_tokens` IS CONTEXT SIZE, NOT TOTAL SPEND (t-f5-11).
       The trigger divides this number by the window. A running total of every token ever
       billed only ever grows, so once it crosses `trigger_fraction` it stays across it
       for the rest of the session and the ladder runs on EVERY turn - the per-turn
       prompt-prefix rewriting that `domain/compaction.py` says costs more than it saves.
       Nothing catches it today because `runner.py::_history_processor` measures the live
       messages itself and never reads the accumulator, so the wrong value is DORMANT,
       sitting exactly where the trigger's input belongs. The test below asks the port's
       own `should_compress` rather than the number directly, because "the trigger latches
       True forever" is the failure, not the arithmetic.

    4. THE ASYNC TWIN CLIMBS IN THE LADDER'S ORDER, NOT THE PROFILE'S (t-f5-11).
       `climb_ladder_async` iterates `sorted(set(policy.enabled_rungs))`. Pinning it
       against the domain over `CompactionPolicy()` is an agreement about nothing: the
       DEFAULT rungs are already sorted and already unique, so `sorted`, `set` and both
       together give the identical sequence and deleting either is invisible. The rungs
       below are therefore scrambled AND carry a duplicate, which is the only input where
       each half of `sorted(set(...))` has a witness: drop `set` and L2 is climbed twice,
       drop `sorted` and the profile's order decides which rung runs first. That order is
       CLAUDE.md non-negotiable #7 - compaction runs inside a `@DBOS.step()`, and an order
       taken from a set replays differently after a crash.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agent_core.adapters.driven.context.engine import (
    LadderContextEngine,
    SummaryRequest,
)
from agent_core.domain.compaction import (
    CompactionPolicy,
    ContextState,
    Rung,
    climb_ladder,
    is_within_target,
    target_tokens,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId, Usage
from agent_core.ports.context_engine import ContextEngine

SESSION = SessionRef(session_id=SessionId("s-ladder"), tenant_id=TenantId("t-1"))

WINDOW = 100_000
# CompactionPolicy.target_fraction defaults to 0.40, so the default target is 40_000.


class _RecordingSummariser:
    """Stands in for the cheap model behind L3 and L4.

    Recording the requests is the whole point: `requests == []` is the evidence that a
    rung the ladder did not need was never PAID for, which is the assertion no amount of
    checking the returned history can make.
    """

    def __init__(self, text: str = "SUMMARY") -> None:
        self.requests: list[SummaryRequest] = []
        self._text = text

    async def __call__(self, request: SummaryRequest) -> str:
        self.requests.append(request)
        return f"{self._text}-{len(self.requests)}"


def _exchange(index: int, *, user_chars: int, tool_chars: int) -> list[ModelMessage]:
    """One complete exchange: a user turn, a tool call, its return, and an answer.

    The tool call and its return are deliberately in the same exchange. That is the shape
    CLAUDE.md #5 is about - cutting between them makes the provider reject the whole
    conversation with a 400 - and it is why an exchange, not a message, is the unit the
    ladder is allowed to drop.
    """
    call_id = f"call-{index}"
    return [
        ModelRequest(parts=[UserPromptPart(content="u" * user_chars)]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="search", args={"q": index}, tool_call_id=call_id)]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search", content="o" * tool_chars, tool_call_id=call_id
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content=f"answer {index}")]),
    ]


def _history(count: int, *, user_chars: int, tool_chars: int) -> list[ModelMessage]:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="you are an agent")])
    ]
    for index in range(count):
        messages.extend(_exchange(index, user_chars=user_chars, tool_chars=tool_chars))
    return messages


def _tool_return_contents(history: Sequence[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_compress_stops_at_the_first_rung_that_meets_the_target() -> None:
    """L1 is free and is usually enough, because tool output is usually the bulk.

    Climbing past it would spend a summariser call and rewrite more of the prompt prefix
    to buy headroom that had already been bought.
    """
    policy = CompactionPolicy()
    summariser = _RecordingSummariser()
    engine = LadderContextEngine(summariser, context_window=WINDOW)

    # Tool output is the whole volume here: ~60_000 tokens against a 40_000 target, and
    # ~12_000 left once L1 has stubbed everything but the most recent exchange.
    history = _history(5, user_chars=40, tool_chars=48_000)

    result = asyncio.run(engine.compress(SESSION, history, policy))

    assert result.tokens_before > target_tokens(policy, WINDOW)
    assert result.rungs_applied == (Rung.L1_PRUNE_TOOL_OUTPUT,)
    assert summariser.requests == []
    assert result.checkpoint is None
    assert is_within_target(policy, result.tokens_after, WINDOW)


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_compress_climbs_when_the_first_rung_falls_short() -> None:
    """The integrity check on the test above: stopping early is only a virtue if the
    ladder demonstrably climbs when it has to.

    Here the volume is in the USER turns, which L1 is not allowed to touch, so L1 frees
    almost nothing and L2 has to run. L2 then meets the target, so the two paid rungs are
    still never reached.
    """
    policy = CompactionPolicy()
    summariser = _RecordingSummariser()
    engine = LadderContextEngine(summariser, context_window=WINDOW)

    history = _history(8, user_chars=40_000, tool_chars=40)

    result = asyncio.run(engine.compress(SESSION, history, policy))

    assert result.rungs_applied == (Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L2_SLIDING_WINDOW)
    assert summariser.requests == []
    assert is_within_target(policy, result.tokens_after, WINDOW)


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_compress_mutates_none_of_its_inputs() -> None:
    """Every rung, including the two that rewrite message parts, builds new objects.

    The target is set low enough that all four rungs run, so the assertion covers the
    paid rungs and not only the free ones. See this module's header for why an in-place
    L1 survives every other assertion in the suite and only fails on a DBOS step retry.
    """
    policy = CompactionPolicy(target_fraction=0.01)
    summariser = _RecordingSummariser()
    engine = LadderContextEngine(summariser, context_window=WINDOW)

    history = _history(8, user_chars=40_000, tool_chars=4_000)
    snapshot = copy.deepcopy(history)
    original_length = len(history)

    result = asyncio.run(engine.compress(SESSION, history, policy))

    assert result.rungs_applied == (
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L3_SUMMARISE_MIDDLE,
        Rung.L4_ITERATIVE_RESUMMARY,
    )
    # The input list itself: same length, same messages, same part contents.
    assert len(history) == original_length
    assert history == snapshot
    # L1's own target, stated separately so a failure names the rung that caused it.
    assert _tool_return_contents(history) == _tool_return_contents(snapshot)
    # And the result is a different object, never the caller's list handed back.
    assert result.compacted_history is not history


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_second_pass_folds_the_previous_summary_instead_of_starting_over() -> None:
    """L4's only reason to exist. The previous summary is an INPUT to the new one, so
    information is refined rather than duplicated, and the chain records what was dropped.
    """
    policy = CompactionPolicy(target_fraction=0.01)
    summariser = _RecordingSummariser()
    engine = LadderContextEngine(summariser, context_window=WINDOW)
    history = _history(8, user_chars=40_000, tool_chars=4_000)

    first = asyncio.run(engine.compress(SESSION, history, policy))
    second = asyncio.run(engine.compress(SESSION, history, policy))

    assert first.checkpoint is not None
    assert second.checkpoint is not None
    assert second.checkpoint.supersedes == first.checkpoint.checkpoint_id
    assert summariser.requests[-1].previous_summary == first.checkpoint.summary


# Scrambled on purpose, and carrying L2 twice. See point 4 of this module's header: over
# the DEFAULT rungs `sorted`, `set` and `sorted(set(...))` are indistinguishable, so a test
# written against the default cannot fail when either half is deleted.
SCRAMBLED_RUNGS = (
    Rung.L4_ITERATIVE_RESUMMARY,
    Rung.L2_SLIDING_WINDOW,
    Rung.L1_PRUNE_TOOL_OUTPUT,
    Rung.L2_SLIDING_WINDOW,
    Rung.L3_SUMMARISE_MIDDLE,
)

LADDER_ORDER = (
    Rung.L1_PRUNE_TOOL_OUTPUT,
    Rung.L2_SLIDING_WINDOW,
    Rung.L3_SUMMARISE_MIDDLE,
    Rung.L4_ITERATIVE_RESUMMARY,
)


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_async_climb_agrees_with_the_domain_ladder() -> None:
    """The engine cannot call `domain.climb_ladder`: its `apply_rung` is sync and L3/L4
    await a model (D13). So the adapter carries an async twin of that loop, and a twin
    that is free to drift is a bug waiting for a bill.

    The profile below lists the rungs out of order and lists L2 twice - the only input
    that can tell `sorted(set(...))` apart from either half of it. Every rung frees the
    same small amount and none of them reaches the target, so the whole ladder is climbed
    and the sequence of calls is the entire evidence.
    """
    policy = CompactionPolicy(enabled_rungs=SCRAMBLED_RUNGS)
    freed_per_rung = 5_000

    sync_order: list[Rung] = []
    tokens = 90_000

    def apply_sync(rung: Rung) -> int:
        nonlocal tokens
        sync_order.append(rung)
        tokens -= freed_per_rung
        return tokens

    expected = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=apply_sync
    )

    async_order: list[Rung] = []
    async_tokens = 90_000

    async def apply_async(rung: Rung) -> int:
        nonlocal async_tokens
        async_order.append(rung)
        async_tokens -= freed_per_rung
        return async_tokens

    actual = asyncio.run(
        LadderContextEngine.climb_ladder_async(
            policy, tokens_before=90_000, context_window=WINDOW, apply_rung=apply_async
        )
    )

    # The domain is the reference, so a drift in either direction names itself.
    assert actual == expected
    assert sync_order == list(LADDER_ORDER)

    # Stated against the ladder's own order rather than only against the domain, so the
    # twin still fails alone if both loops are ever edited together.
    assert async_order == list(LADDER_ORDER)
    assert actual.rungs_applied == LADDER_ORDER
    # Dropping `set` climbs L2 twice and pays for the duplicate. Verified by mutation.
    assert len(actual.rungs_applied) == len(set(actual.rungs_applied))
    # Dropping BOTH iterates the profile's own tuple and starts at L4 - a summary bought
    # before free pruning was even tried. Verified by mutation.
    assert actual.rungs_applied[0] is Rung.L1_PRUNE_TOOL_OUTPUT

    # HONEST LIMIT, so nobody reads more into this test than it proves. Dropping `sorted`
    # while KEEPING `set` cannot be made to fail here, and that is a property of the data
    # rather than a hole in the assertions: `Rung` is an `IntEnum`, so a rung hashes to its
    # own small value, and a CPython set holding 1..4 iterates them in ascending order for
    # every hash seed. `sorted` is therefore a no-op TODAY and load-bearing the moment a
    # rung is renumbered, inserted, or the ladder outgrows one hash table - which is why it
    # stays, and why relying on the set's order instead would be CLAUDE.md #7 exactly.


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_trigger_input_is_context_size_not_the_running_total() -> None:
    """Two responses in a row do not add up. Each one reports the whole prompt it was
    billed for, so the LATEST is already the size of the conversation - adding them counts
    every earlier turn again, once per turn that follows it."""
    engine = LadderContextEngine(context_window=WINDOW)

    engine.update_from_response(SESSION, Usage(input_tokens=30_000, output_tokens=400))
    engine.update_from_response(SESSION, Usage(input_tokens=34_000, output_tokens=600))

    assert engine.estimated_tokens(SESSION) == 34_600


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_trigger_stops_firing_once_a_compaction_has_freed_the_room() -> None:
    """A running total never decreases, so a trigger fed one latches True for the rest of
    the session and compacts on EVERY turn - the prompt-prefix rewriting that costs more
    than it saves. Asked through `should_compress`, because the latch is the failure.
    """
    policy = CompactionPolicy()
    engine = LadderContextEngine(_RecordingSummariser(), context_window=WINDOW)

    # Tool output is the whole volume, so the free rung alone frees most of it.
    history = _history(5, user_chars=40, tool_chars=48_000)
    engine.update_from_response(SESSION, Usage(input_tokens=80_000, output_tokens=500))

    before = engine.estimated_tokens(SESSION)
    assert engine.should_compress(_trigger_state(engine), policy)

    result = asyncio.run(engine.compress(SESSION, history, policy))
    assert result.made_progress

    after = engine.estimated_tokens(SESSION)
    assert after < before
    assert after == result.tokens_after
    assert not engine.should_compress(_trigger_state(engine), policy)


def _trigger_state(engine: LadderContextEngine) -> ContextState:
    """The state the runner builds, with `window_used` None - the common case, and the one
    where the engine's own number is the only input the trigger has."""
    return ContextState(
        session=SESSION,
        window_used=None,
        estimated_tokens=engine.estimated_tokens(SESSION),
        context_window=WINDOW,
        message_count=0,
    )


@pytest.mark.phase("F5")
def test_the_engine_satisfies_the_port() -> None:
    """Type-level conformance, checked by the project-wide mypy run at this assignment."""
    engine: ContextEngine = LadderContextEngine()
    assert engine.should_compress is not None
