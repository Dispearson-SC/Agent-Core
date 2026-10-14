"""Progress events of a turn tree (C21, D52). Pure: no I/O, no framework.

A turn, a peer's turn and (later) a subagent's turn each publish the same four facts while they
run, so a UI, a voice front-end or an operator can follow a conversation's tree without polling
the database: `started`, `tool_called`, `finished`, `failed`. A turn a PERSON started also streams
its reply as `delta` events (C26, D58).

Every event carries the OWNER chain of the turn that made it (the root turn a person started, the
turn that asked, the asking profile, the ask's handle) so a consumer can file it under the root
without a lookup. Events are facts about what ALREADY happened, never instructions, and they carry
no tool arguments: a tool's NAME, a duration and a short error class at most. The one exception to
"no model text" is `delta`, whose chunk is the reply the turn is about to record for the person
who is reading the feed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

__all__ = [
    "MAX_DELTA_CHARS",
    "MAX_LABEL_CHARS",
    "EventKind",
    "EventOwner",
    "EventWindow",
    "FeedSnapshot",
    "ProgressEvent",
    "StoredEvent",
    "TurnProgress",
    "TurnState",
    "bound_event",
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
    # C26 (D58). A piece of the reply a person-started turn is streaming: `text` is the chunk and
    # `detail` is `<run>:<response>:<index>` (see `ProgressEvent`). The only kind that carries text.
    DELTA = "delta"
    # D95. A router chose the profile a person-started turn runs under, before `started`: `agent`
    # is that profile, `detail` the outcome (`routed`, `passed`, `fallback:<reason>`), `elapsed_ms`
    # how long the router took. Never a label or text. It does not change a turn's state.
    ROUTED = "routed"


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
    `delta` (C26) is the one kind that carries model text: `text` is a chunk of the reply a PERSON'S
    turn is streaming (never a peer's), the same words the turn later records as its answer, and
    `detail` is `<run>:<response>:<index>`: `run` names one execution of the model step (a retried
    step is a new run, whose text replaces the old one's), `response` which model response of the
    run the chunk belongs to (the reply is the last one) and `index` its order within it.

    `owner` is also None on a `tool_called` made where the chain is not at hand (the audit hook
    holds only the turn id); the `root_turn_id` property then answers the event's own turn id, and
    the publisher files the stored row under the chain of the turn's `started` event instead.
    The property is never empty.
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
    text: str | None = None

    @property
    def root_turn_id(self) -> str:
        return self.owner.root_turn_id if self.owner is not None else self.turn_id


MAX_LABEL_CHARS: Final[int] = 200
"""Longest agent, tool, asker, call or detail label an event keeps. A label is a name or a short
class, never prose; the bound keeps one odd value from bloating a row the feed re-reads."""

MAX_DELTA_CHARS: Final[int] = 2000
"""Longest `text` of a `delta` event. The chunker cuts far below this (`MAX_CHUNK_CHARS`)."""


def _cut(value: str, limit: int = MAX_LABEL_CHARS) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def bound_event(event: ProgressEvent) -> ProgressEvent:
    """The event with every free-form label cut to `MAX_LABEL_CHARS` (ending in `…` when cut).
    Identity (ids, tenant, session, kind, time, numbers) is never touched. Pure."""
    owner = event.owner
    return replace(
        event,
        agent=_cut(event.agent),
        tool=None if event.tool is None else _cut(event.tool),
        detail=None if event.detail is None else _cut(event.detail),
        text=None if event.text is None else _cut(event.text, MAX_DELTA_CHARS),
        owner=None
        if owner is None
        else replace(
            owner,
            asker=None if owner.asker is None else _cut(owner.asker),
            call=_cut(owner.call),
        ),
    )


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
    # C26. The reply this turn has streamed so far: the chunks of its latest model response, in
    # order. Empty for a turn that does not stream. The recorded answer, not this, is the truth.
    reply_text: str = ""


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
    replies: dict[str, _Reply] = {}
    last = 0
    for stored in events:
        last = max(last, stored.seq)
        event = stored.event
        if event.kind is EventKind.STARTED:
            turns[event.turn_id] = _placeholder(event)
            continue
        if event.kind is EventKind.ROUTED:
            continue  # precedes `started` and changes nothing about the turn's state
        current = turns.get(event.turn_id)
        if current is None:
            if omitted <= 0:
                continue  # a turn from before events existed: nothing to show
            current = _placeholder(event)
            turns[event.turn_id] = current
        if event.kind is EventKind.DELTA:
            replies.setdefault(event.turn_id, _Reply()).add(event)
        elif event.kind is EventKind.TOOL_CALLED:
            turns[event.turn_id] = replace(
                current, tools_called=current.tools_called + 1, last_tool=event.tool
            )
        else:
            turns[event.turn_id] = _finish(current, event)
    for turn_id, reply in replies.items():
        if turn_id in turns:
            turns[turn_id] = replace(turns[turn_id], reply_text=reply.text())
    return FeedSnapshot(
        root_turn_id=root_turn_id,
        last_seq=last,
        turns=tuple(turns.values()),
        omitted_events=max(0, omitted),
    )


class _Reply:
    """The streamed reply of one turn while a snapshot is folded: the chunks of the latest
    (run, response), by index. A chunk of a later run or response starts the text over."""

    __slots__ = ("_chunks", "_which")

    def __init__(self) -> None:
        self._which: tuple[str, str] | None = None
        self._chunks: dict[int, str] = {}

    def add(self, event: ProgressEvent) -> None:
        parts = (event.detail or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit() or event.text is None:
            return  # not a chunk this build wrote
        which = (parts[0], parts[1])
        if which != self._which:
            self._which, self._chunks = which, {}
        self._chunks[int(parts[2])] = event.text

    def text(self) -> str:
        return "".join(self._chunks[i] for i in sorted(self._chunks))


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
