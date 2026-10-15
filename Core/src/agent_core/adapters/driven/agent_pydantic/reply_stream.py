"""Driven adapter: stream a turn's reply text to the event feed as it is written (C26, D58).

Phase:   Agent-comms v2, C26
Tests:   Core/tests/unit/test_reply_stream.py

Pydantic AI hands an `event_stream_handler` every stream event of a model response while it is still
being produced. This handler listens for the TEXT of the response, cuts it into sentence-sized
chunks (`domain/reply_chunks.py`) and publishes each as a `delta` event, so a voice front-end can
start speaking on the first sentence instead of waiting for the whole answer.

A SIDE CHANNEL, NOT THE ANSWER. The turn's recorded answer is what the model run returns, exactly
as before; nothing here changes it, reads it back, or decides anything from it. A handler that
fails is disabled for the rest of the run after the FIRST failure (one log line, not one per
chunk) and the run carries on: the chunks are a courtesy copy of text the turn is about to record.

OFF THE MODEL'S PATH (C1q). A chunk is not published inside the model stream: it is queued and a
background task publishes it, so a slow event store never delays the next token. The queue holds at
most `max_pending` chunks; text that arrives while it is full is merged into the newest pending
chunk of the same response (up to the delta bound, past which it is dropped with one log line), so
memory stays bounded and the text stays whole. The end of every stream flushes what is pending
(bounded by `flush_timeout`), so a finished response is on the feed before the step moves on.

IDENTITY OF A CHUNK. `detail` is `<run>:<response>:<index>`. `run` is a random id per handler (one
execution of the model step, so a step that is retried after a crash starts a new run and its text
REPLACES the abandoned run's in a snapshot instead of being appended to it); `response` counts the
model responses of the run (a response starts at its part index 0, however many text parts it has;
a reply after a tool call is a new response, and the reply is the last one); `index` orders the
chunks of a response. The event id derives from all three, so publishing the same chunk twice (a
retry inside one run) writes one row.

ONLY A PERSON'S TURN. The runner attaches this for a turn nobody else owns. A peer's text is another
agent's input; it is never put on the feed. A ROUTED turn (S8: it runs under the delegated identity,
on behalf of the person) is the person's turn and streams like one.

TAKING TEXT BACK (P11). A run whose text turns out not to be the reply (an intent plan's compose
cut at its token bound, or failing while it streams, before the turn falls back to the normal path)
calls `withdraw`: ONE empty chunk of the next response of the same run. By the feed's rule a chunk
of a later response starts the text over, so the turn's `reply_text` becomes empty and a client
that discards on a new (run, response) drops what it held, instead of showing a cut draft that
contradicts the fallback reply until that reply's first chunk arrives.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from agent_core.domain.events import MAX_DELTA_CHARS, EventKind, ProgressEvent, progress_event_id
from agent_core.domain.reply_chunks import ReplyChunker
from agent_core.domain.turn import SessionRef, TurnId
from agent_core.ports.event_publisher import EventPublisher

__all__ = ["ReplyStreamHandler"]

_LOG = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 32
DEFAULT_FLUSH_TIMEOUT_SECONDS = 5.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _Chunk:
    """One piece of reply text waiting to be published. Mutable on purpose: overflow is merged
    into the newest pending chunk of the same response."""

    response: int
    index: int
    text: str


class ReplyStreamHandler:
    """One per model run. Call it as Pydantic AI's `event_stream_handler`."""

    def __init__(
        self,
        events: EventPublisher,
        *,
        turn_id: TurnId,
        session: SessionRef,
        agent: str,
        clock: Callable[[], datetime] = _utc_now,
        max_pending: int = DEFAULT_MAX_PENDING,
        flush_timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._events = events
        self._turn_id = str(turn_id)
        self._session = session
        self._agent = agent
        self._clock = clock
        self._run = uuid.uuid4().hex
        self._response = -1
        self._index = 0
        self._chunker = ReplyChunker()
        self._disabled = False
        self._max_pending = max_pending
        self._flush_timeout = flush_timeout
        self._pending: deque[_Chunk] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._overflow_logged = False
        self._streamed = False  # some text of this run was queued for the feed

    @property
    def pending(self) -> int:
        """How many chunks are waiting to be published."""
        return len(self._pending)

    async def __call__(self, _ctx: Any, stream: AsyncIterable[AgentStreamEvent]) -> None:
        try:
            await self._consume(stream)
            await self._flush()
        finally:
            # However the stream ended (finished, raised, cancelled) the publish worker ends with
            # it: cancelled AND awaited, so nothing keeps publishing the text of a finished turn.
            await self._reap()

    async def _consume(self, stream: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in stream:
            if isinstance(event, PartStartEvent):
                if event.index == 0:  # the first part of a model response
                    self._response += 1
                    self._index = 0
                if isinstance(event.part, TextPart):
                    self._chunker = ReplyChunker()
                    self._enqueue(self._chunker.push(event.part.content))
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                self._enqueue(self._chunker.push(event.delta.content_delta))
            elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                self._enqueue(self._chunker.flush())
        self._enqueue(self._chunker.flush())

    def _enqueue(self, chunks: list[str]) -> None:
        for text in chunks:
            if self._disabled or not text:
                continue
            index = self._index
            self._index += 1
            self._streamed = True
            if len(self._pending) >= self._max_pending:
                self._coalesce(text)
                continue
            self._pending.append(_Chunk(self._response, index, text))
            if self._worker is None or self._worker.done():
                self._worker = asyncio.get_running_loop().create_task(self._drain())

    async def withdraw(self) -> None:
        """Take back the text this run streamed: stop streaming and publish one empty chunk of the
        next response (see TAKING TEXT BACK). Nothing is published when no text was streamed. A
        failure is logged once, like any publish, and never raised: the turn goes on."""
        self._disabled = True
        await self._reap()
        if not self._streamed:
            return
        self._streamed = False
        await self._publish(self._response + 1, 0, "")

    def _coalesce(self, text: str) -> None:
        newest = self._pending[-1]
        if newest.response == self._response and len(newest.text) + len(text) <= MAX_DELTA_CHARS:
            newest.text += text
            return
        if not self._overflow_logged:
            self._overflow_logged = True
            _LOG.warning(
                "the event store is too slow for the reply of turn %r; part of the streamed "
                "text is dropped (the turn is unaffected)",
                self._turn_id,
            )

    async def _drain(self) -> None:
        while self._pending and not self._disabled:
            chunk = self._pending.popleft()
            await self._publish(chunk.response, chunk.index, chunk.text)

    async def _flush(self) -> None:
        worker = self._worker
        if worker is None or worker.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(worker), self._flush_timeout)
        except TimeoutError:
            self._disabled = True
            self._abandon()
            _LOG.warning(
                "the event store did not accept the reply of turn %r in time; the rest of it is "
                "not streamed (the turn is unaffected)",
                self._turn_id,
            )

    def _abandon(self) -> None:
        self._pending.clear()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    async def _reap(self) -> None:
        """Cancel whatever is still publishing and wait until it has actually stopped."""
        self._abandon()
        worker, self._worker = self._worker, None
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)

    async def _publish(self, response: int, index: int, text: str) -> None:
        discriminator = f"{self._run}:{response}:{index}"
        try:
            await self._events.publish(
                ProgressEvent(
                    event_id=progress_event_id(self._turn_id, EventKind.DELTA, discriminator),
                    kind=EventKind.DELTA,
                    tenant_id=str(self._session.tenant_id),
                    session_id=str(self._session.session_id),
                    turn_id=self._turn_id,
                    agent=self._agent,
                    at=self._clock(),
                    detail=discriminator,
                    text=text,
                )
            )
        except Exception:
            self._disabled = True
            self._pending.clear()
            _LOG.warning(
                "could not publish the reply of turn %r to the event feed; the rest of this "
                "reply is not streamed (the turn is unaffected)",
                self._turn_id,
                exc_info=True,
            )
