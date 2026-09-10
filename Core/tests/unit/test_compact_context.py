"""CompactContext - the use case that decides whether to compress, and commits it once.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-03

SILENT-BUG AREA (CLAUDE.md). Everything wrong here shows up on the BILL, not in a test -
which is exactly why the one property this module exists to pin is asserted by COUNTING
calls rather than by inspecting a return value.

THE PROPERTY THAT IS THE WHOLE TASK
    A pass that freed nothing must be returned, not retried. Retrying it is an infinite
    loop that ALSO rewrites the prompt prefix on every pass, so the provider's cache is
    invalidated and the next request re-bills the whole prompt at full price. That is the
    most expensive failure mode this system has, and no return value can catch it: only
    "how many times was `compress` called" can. `FakeContextEngine.compress_calls` is
    therefore the assertion, and `made_progress` is only the input that provokes it.

`FakeContextEngine` is declared here rather than in tests/fakes/ports.py: that file's own
TODO list defers the F5 fake, and t-f5-03 does not own it.

Fakes only - no database, no model. tests/conftest.py says why.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.application.compact_context import CompactContext
from agent_core.domain.compaction import (
    CompactionCheckpoint,
    CompactionPolicy,
    CompactionResult,
    ContextState,
    Rung,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import SessionRef, Usage
from tests.fakes.ports import FakeConversationStore

PROFILE = AgentProfile(id="delivery_optimizer", persona="You optimise deliveries.", model="m")


class FakeContextEngine:
    """A scripted engine: it answers whatever the test told it to, and COUNTS.

    The counter is the point. A use case that retried a fruitless compaction would still
    return a perfectly well-formed `CompactionResult`, so a test that only looked at the
    return value would pass while the bill doubled."""

    def __init__(self, *, trigger: bool, result: CompactionResult) -> None:
        self._trigger = trigger
        self._result = result
        self.compress_calls: list[tuple[SessionRef, object, CompactionPolicy]] = []
        self.trigger_calls: list[tuple[ContextState, CompactionPolicy]] = []

    def on_session_start(self, session: SessionRef) -> None:
        return None

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        return None

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        self.trigger_calls.append((state, policy))
        return self._trigger

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        self.compress_calls.append((session, history, policy))
        return self._result

    def on_session_end(self, session: SessionRef) -> None:
        return None


def _state(session: SessionRef) -> ContextState:
    return ContextState(
        session=session,
        window_used=0.9,
        estimated_tokens=90_000,
        context_window=100_000,
        message_count=40,
    )


def _checkpoint(
    session: SessionRef, *, tokens_before: int, tokens_after: int
) -> CompactionCheckpoint:
    return CompactionCheckpoint(
        checkpoint_id="ck-1",
        session=session,
        summary="The courier was rerouted twice.",
        covers_through_message=20,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        rungs_applied=(Rung.L3_SUMMARISE_MIDDLE,),
    )


def _result(
    *, tokens_before: int, tokens_after: int, checkpoint: CompactionCheckpoint | None
) -> CompactionResult:
    return CompactionResult(
        compacted_history=["compacted"],
        checkpoint=checkpoint,
        rungs_applied=(Rung.L1_PRUNE_TOOL_OUTPUT,),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
    )


@pytest.mark.phase("F5")
@pytest.mark.silent
def test_no_progress_returns_immediately_and_never_compresses_twice(
    session: SessionRef,
) -> None:
    """THE anchor assertion for t-f5-03: no retry, and exactly one `compress` call.

    `tokens_after == tokens_before` is the ladder saying it freed nothing. The use case
    must hand that back and stop. A second call would be the infinite loop described in
    `domain/compaction.py`, and it would re-invalidate the prompt cache every pass."""
    result = _result(tokens_before=90_000, tokens_after=90_000, checkpoint=None)
    assert result.made_progress is False

    engine = FakeContextEngine(trigger=True, result=result)
    store = FakeConversationStore()

    returned = asyncio.run(
        CompactContext(context=engine, store=store).execute(session, PROFILE, _state(session))
    )

    assert returned is result
    assert len(engine.compress_calls) == 1


@pytest.mark.phase("F5")
@pytest.mark.silent
def test_a_fruitless_pass_writes_no_checkpoint(session: SessionRef) -> None:
    """Stopping means stopping. Nothing was freed, so nothing joins the `supersedes`
    chain - a checkpoint that covers the same messages as the last one makes the chain
    unreadable as a record of what the agent used to know."""
    result = _result(
        tokens_before=90_000,
        tokens_after=90_000,
        checkpoint=_checkpoint(session, tokens_before=90_000, tokens_after=90_000),
    )

    engine = FakeContextEngine(trigger=True, result=result)
    store = FakeConversationStore()

    asyncio.run(
        CompactContext(context=engine, store=store).execute(session, PROFILE, _state(session))
    )

    assert store.checkpoints == []


@pytest.mark.phase("F5")
def test_no_trigger_means_no_compression_at_all(session: SessionRef) -> None:
    """The engine owns the trigger, including the None-window fallback. The use case asks
    once and believes the answer: a second opinion here is two triggers that can disagree,
    which is worse than one that is occasionally wrong."""
    engine = FakeContextEngine(
        trigger=False,
        result=_result(tokens_before=90_000, tokens_after=10_000, checkpoint=None),
    )
    store = FakeConversationStore()

    returned = asyncio.run(
        CompactContext(context=engine, store=store).execute(session, PROFILE, _state(session))
    )

    assert returned is None
    assert engine.compress_calls == []
    assert store.checkpoints == []


@pytest.mark.phase("F5")
def test_a_progressing_pass_persists_its_checkpoint(session: SessionRef) -> None:
    """The happy path, and the one write this use case is allowed to make. `compress`
    does not mutate the live conversation - the caller commits, which is what makes a
    retried DBOS step safe."""
    checkpoint = _checkpoint(session, tokens_before=90_000, tokens_after=30_000)
    result = _result(tokens_before=90_000, tokens_after=30_000, checkpoint=checkpoint)

    engine = FakeContextEngine(trigger=True, result=result)
    store = FakeConversationStore()

    returned = asyncio.run(
        CompactContext(context=engine, store=store).execute(session, PROFILE, _state(session))
    )

    assert returned is result
    assert store.checkpoints == [checkpoint]
    assert len(engine.compress_calls) == 1
    # The history handed to the engine is the one the store returned, not a freshly
    # invented list: compacting anything else compacts a conversation nobody is having.
    assert engine.compress_calls[0][1] == []
