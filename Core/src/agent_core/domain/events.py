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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

__all__ = ["EventKind", "EventOwner", "ProgressEvent", "progress_event_id"]

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
