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

IDENTITY OF A CHUNK. `detail` is `<run>:<response>:<index>`. `run` is a random id per handler (one
execution of the model step, so a step that is retried after a crash starts a new run and its text
REPLACES the abandoned run's in a snapshot instead of being appended to it); `response` counts the
model responses of the run that contained text (a reply after a tool call is a new response, and the
reply is the last one); `index` orders the chunks of a response. The event id derives from all
three, so publishing the same chunk twice (a retry inside one run) writes one row.

ONLY A PERSON'S TURN. The runner attaches this for a turn nobody else owns. A peer's text is another
agent's input; it is never put on the feed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterable, Callable
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

from agent_core.domain.events import EventKind, ProgressEvent, progress_event_id
from agent_core.domain.reply_chunks import ReplyChunker
from agent_core.domain.turn import SessionRef, TurnId
from agent_core.ports.event_publisher import EventPublisher

__all__ = ["ReplyStreamHandler"]

_LOG = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    ) -> None:
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

    async def __call__(self, _ctx: Any, stream: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                self._response += 1
                self._index = 0
                self._chunker = ReplyChunker()
                await self._publish(self._chunker.push(event.part.content))
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                await self._publish(self._chunker.push(event.delta.content_delta))
            elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                await self._publish(self._chunker.flush())
        await self._publish(self._chunker.flush())

    async def _publish(self, chunks: list[str]) -> None:
        for text in chunks:
            if self._disabled or not text:
                continue
            discriminator = f"{self._run}:{self._response}:{self._index}"
            self._index += 1
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
                _LOG.warning(
                    "could not publish the reply of turn %r to the event feed; the rest of this "
                    "reply is not streamed (the turn is unaffected)",
                    self._turn_id,
                    exc_info=True,
                )
