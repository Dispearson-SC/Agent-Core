"""Port: EventReader and EventWakeup - how a feed follows the events of a turn tree (C22, D53).

Phase:   Agent-comms v2, wave 3
Adapter: adapters/driven/events_pg/reader.py
Per-vertical: NO

`EventPublisher` (C21) writes; this reads. Two small ports because the two questions are different
and a test wants to answer them separately.

`EventReader`
    * `after(seq)`: every event with a higher sequence number, oldest first, ALL tenants. The feed
      broker is one reader per process; it fans out by (tenant, root) itself. Never exposed to a
      caller.
    * `tree(tenant_id, root_turn_id)`: the events of ONE tree, scoped by tenant IN THE QUERY (the
      tenant predicate is the guarantee, the way it is for `TranscriptReader`). This is what a
      snapshot is folded from. A tree over the limit is returned as its newest events with the
      number left out (`EventWindow.omitted`); a reader never truncates silently.
    * `latest_seq()`: where "now" is, so a broker that starts late does not replay history.

`EventWakeup`
    * `wait(timeout)`: returns when something MAY have been published, or when `timeout` seconds
      pass. It is a hint, never a source of truth: the broker re-reads by sequence either way, so a
      lost wake-up costs latency (at most the poll interval), not events.
    * `close()`: the wake-up may hold one long-lived listening connection; the feed closes it when
      its broker stops.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.events import EventWindow, StoredEvent

__all__ = ["EventReader", "EventWakeup"]


class EventReader(Protocol):
    async def after(self, seq: int, *, limit: int = 500) -> tuple[StoredEvent, ...]:
        """Events with `seq` greater than the given one, in `seq` order, at most `limit`."""
        ...

    async def tree(self, tenant_id: str, root_turn_id: str, *, limit: int = 2000) -> EventWindow:
        """The events filed under `root_turn_id` for that tenant only, in `seq` order. A tree
        larger than `limit` yields its NEWEST `limit` events and the count of older ones left
        out."""
        ...

    async def latest_seq(self) -> int:
        """The highest sequence number stored (0 when there is none)."""
        ...


class EventWakeup(Protocol):
    async def wait(self, timeout: float) -> None:
        """Return when an event may have been published, or after `timeout` seconds. A
        notification that arrived since the previous wait counts: it is not lost between waits."""
        ...

    async def close(self) -> None:
        """Release whatever the wake-up holds (a listening connection). Safe to call twice."""
        ...
