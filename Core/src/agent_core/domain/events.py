"""Progress events of a turn tree (C21, D52). Pure: no I/O, no framework.

A turn, a peer's turn and (later) a subagent's turn each publish the same four facts while they
run, so a UI, a voice front-end or an operator can follow a conversation's tree without polling
the database: `started`, `tool_called`, `finished`, `failed`.

Every event carries the OWNER chain of the turn that made it (the root turn a person started, the
turn that asked, the asking profile, the ask's handle) so a consumer can file it under the root
without a lookup. Events are facts about what ALREADY happened, never instructions, and they carry
no model text and no tool arguments: a tool's NAME, a duration and a short error class at most.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

__all__ = [
    "EventKind",
    "EventOwner",
    "EventWindow",
    "FeedSnapshot",
    "ProgressEvent",
    "StoredEvent",
    "TurnProgress",
    "TurnState",
    "fold_snapshot",
    "progress_event_id",
]

_EVENT_NAMESPACE: Final[uuid.UUID] = uuid.UUID("0d3e8c52-6a1b-5b7e-8f40-2c9a7e1d5b63")
"""Fixed namespace of event ids. NEVER change it: ids already stored were derived from it."""


class EventKind(StrEnum):
    STARTED = "started"
    TOOL_CALLED = "tool_called"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EventOwner:
    """Who owns the turn an event is about: the chain of a peer turn (mirrors `PeerOwner`).

    `root_turn_id` is the turn a person started and the feed key; `asker_turn_id` the turn that
    made the ask; `asker` the profile it ran under (None if unknown); `call` the ask's handle.
    """

    root_turn_id: str
    asker_turn_id: str
    asker: str | None
    call: str


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One fact about a turn. `event_id` makes publishing idempotent (see `progress_event_id`).

    `owner` is None for a turn a person or a channel started (it is its own root). `tool` is set on
    `tool_called` only; `elapsed_ms` on `finished` and `failed` (time since `started`); `detail` is
    a short label (the tool's policy verdict, an error class), never free text from a model.
    `root_turn_id` may be empty on a `tool_called` made where the chain is not at hand: the store
    fills it from the turn's `started` event.
    """

    event_id: str
    kind: EventKind
    tenant_id: str
    session_id: str
    turn_id: str
    agent: str
    at: datetime
    owner: EventOwner | None = None
    tool: str | None = None
    elapsed_ms: int | None = None
    detail: str | None = None

    @property
    def root_turn_id(self) -> str:
        return self.owner.root_turn_id if self.owner is not None else self.turn_id


def progress_event_id(turn_id: str, kind: EventKind, discriminator: str = "") -> str:
    """The id of one event. Pure and deterministic: a replay of the step that publishes it names
    the same event, and the store keeps one row per id. `discriminator` separates events of the same
    kind in one turn (none for started/finished/failed, which happen once)."""
    return str(uuid.uuid5(_EVENT_NAMESPACE, f"{turn_id}:{kind.value}:{discriminator}"))


# --- the state a feed shows a late or lagging subscriber (C22) -----------------------------------


class TurnState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event with the sequence number the store gave it: the total order a feed resumes from."""

    seq: int
    event: ProgressEvent


@dataclass(frozen=True, slots=True)
class EventWindow:
    """What a bounded read of a tree returned: the NEWEST events that fit, in `seq` order, and how
    many older ones the limit left out (0 = the whole tree). A reader never drops silently."""

    events: tuple[StoredEvent, ...]
    omitted: int = 0


@dataclass(frozen=True, slots=True)
class TurnProgress:
    """Where one turn of the tree stands, folded from its events."""

    turn_id: str
    agent: str
    state: TurnState
    asker_turn_id: str | None
    asker: str | None
    call: str | None
    started_at: datetime
    elapsed_ms: int | None
    tools_called: int
    last_tool: str | None


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    """The whole tree under `root_turn_id` as of event `last_seq` (0 = nothing yet).

    What a subscriber that connected late, reconnected, or fell behind receives INSTEAD of the
    events it missed: the newest state, not a replay (D53). Turns are in order of first event.
    """

    root_turn_id: str
    last_seq: int
    turns: tuple[TurnProgress, ...]
    open_asks: int = 0
    omitted_events: int = 0

    @property
    def truncated(self) -> bool:
        """The read behind this snapshot left older events out: early turns may be missing and
        counts such as `tools_called` are lower bounds."""
        return self.omitted_events > 0

    @property
    def settled(self) -> bool:
        """The root turn and everything under it has finished or failed: no turn is running and no
        ask recorded under the tree is still queued or running (`open_asks`, which a background
        ask keeps above zero after the turn that made it has ended)."""
        return (
            bool(self.turns)
            and self.open_asks == 0
            and all(t.state is not TurnState.RUNNING for t in self.turns)
        )


def fold_snapshot(
    root_turn_id: str, events: tuple[StoredEvent, ...], *, omitted: int = 0
) -> FeedSnapshot:
    """The state of the tree after `events` (in `seq` order). Pure.

    `omitted` is how many older events the read left out. When it is positive, an event of a turn
    whose `started` is not in the window still shows the turn (running until a `finished` or
    `failed` says otherwise): hiding it would let the tree look settled while it is not. When it is
    zero such an event belongs to a turn from before events existed and is ignored."""
    turns: dict[str, TurnProgress] = {}
    last = 0
    for stored in events:
        last = max(last, stored.seq)
        event = stored.event
        if event.kind is EventKind.STARTED:
            turns[event.turn_id] = _placeholder(event)
            continue
        current = turns.get(event.turn_id)
        if current is None:
            if omitted <= 0:
                continue  # a turn from before events existed: nothing to show
            current = _placeholder(event)
        if event.kind is EventKind.TOOL_CALLED:
            turns[event.turn_id] = replace(
                current, tools_called=current.tools_called + 1, last_tool=event.tool
            )
        else:
            turns[event.turn_id] = _finish(current, event)
    return FeedSnapshot(
        root_turn_id=root_turn_id,
        last_seq=last,
        turns=tuple(turns.values()),
        omitted_events=max(0, omitted),
    )


def _placeholder(event: ProgressEvent) -> TurnProgress:
    """A running turn as of this event: its `started`, or (over a truncated window) the first of
    its events that is in the window."""
    owner = event.owner
    return TurnProgress(
        turn_id=event.turn_id,
        agent=event.agent,
        state=TurnState.RUNNING,
        asker_turn_id=None if owner is None else owner.asker_turn_id,
        asker=None if owner is None else owner.asker,
        call=None if owner is None else owner.call,
        started_at=event.at,
        elapsed_ms=None,
        tools_called=0,
        last_tool=None,
    )


def _finish(current: TurnProgress, event: ProgressEvent) -> TurnProgress:
    return replace(
        current,
        state=TurnState.FINISHED if event.kind is EventKind.FINISHED else TurnState.FAILED,
        elapsed_ms=event.elapsed_ms,
    )
