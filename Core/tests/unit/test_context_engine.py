"""The ladder engine - a SILENT-BUG AREA (CLAUDE.md). Wrong here shows up on the BILL.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-04

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
    Rung,
    climb_ladder,
    is_within_target,
    target_tokens,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId
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


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_async_climb_agrees_with_the_domain_ladder() -> None:
    """The engine cannot call `domain.climb_ladder`: its `apply_rung` is sync and L3/L4
    await a model (D13). So the adapter carries an async twin of that loop, and a twin
    that is free to drift is a bug waiting for a bill.

    This pins them together over the one input where both are total - a ladder whose
    rungs are pure arithmetic.
    """
    policy = CompactionPolicy()
    frees = {
        Rung.L1_PRUNE_TOOL_OUTPUT: 10_000,
        Rung.L2_SLIDING_WINDOW: 20_000,
        Rung.L3_SUMMARISE_MIDDLE: 30_000,
        Rung.L4_ITERATIVE_RESUMMARY: 0,
    }

    tokens = 90_000

    def apply_sync(rung: Rung) -> int:
        nonlocal tokens
        tokens -= frees[rung]
        return tokens

    expected = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=apply_sync
    )

    async_tokens = 90_000

    async def apply_async(rung: Rung) -> int:
        nonlocal async_tokens
        async_tokens -= frees[rung]
        return async_tokens

    actual = asyncio.run(
        LadderContextEngine.climb_ladder_async(
            policy, tokens_before=90_000, context_window=WINDOW, apply_rung=apply_async
        )
    )

    assert actual == expected


@pytest.mark.phase("F5")
def test_the_engine_satisfies_the_port() -> None:
    """Type-level conformance, checked by the project-wide mypy run at this assignment."""
    engine: ContextEngine = LadderContextEngine()
    assert engine.should_compress is not None
