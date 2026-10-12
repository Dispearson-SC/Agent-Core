"""Port: EventPublisher - where a turn tells the world how it is going (C21, D52).

Phase:   Agent-comms v2, wave 3
Adapter: adapters/driven/events_pg/ (Postgres table + LISTEN/NOTIFY)
Per-vertical: NO

One verb. The caller is a workflow or a hook in the middle of a turn, so the contract is about what
the caller may rely on:

  * IDEMPOTENT BY `event_id`. Publishing the same event twice leaves one event. A replayed step
    publishes the id it published before.
  * AT MOST A SMALL PAYLOAD. An event is a fact (kind, ids, tool name, timing), never a transcript.
  * A FAILURE MUST NOT BREAK A TURN. An adapter may raise; every caller wraps the call and carries
    on (`fail open`, logged), because losing a progress event is a smaller harm than failing the
    answer a person is waiting for.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.events import ProgressEvent

__all__ = ["EventPublisher"]


class EventPublisher(Protocol):
    async def publish(self, event: ProgressEvent) -> None:
        """Record `event` and tell whoever is listening. Idempotent by `event.event_id`."""
        ...
