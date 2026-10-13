"""The ownership tree of a turn: who asked whom, in what state, for how long (C23, D54). Pure.

Pi's `taskGraph()` answers "what is alive and who owns it" from the task records. Here the records
are the mailbox rows (`peer_messages`): every ask is a row naming the turn that asked, the target,
its state and its times. A callee's own turn id is a pure function of its row
(`peer_workflow_id(handle, generation)`), so a nested ask (whose asking turn IS that id) is linked
to its parent without a second table or a stored parent pointer.

The tree: the ROOT is the turn a person started; its children are the asks it made; each child is a
callee whose own asks are its children, to any depth. A background ask is a child like any other,
flagged. An ask whose parent cannot be found (a row older than the owner chain, or a parent past
the read limit) is returned apart as `unlinked` rather than hung on the root, which would claim an
ownership nobody recorded.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from agent_core.domain.agent_mode import DelegationKind
from agent_core.domain.peers import (
    BackgroundState,
    legacy_peer_workflow_id,
    peer_workflow_id,
)

__all__ = ["AskRecord", "AskWindow", "TaskGraph", "TaskNode", "build_task_graph"]


@dataclass(frozen=True, slots=True)
class AskRecord:
    """One mailbox row as the graph needs it. `asking_turn_id` is the turn that made the ask: the
    root turn, or the callee turn of another ask."""

    handle: str
    target: str
    asker: str | None
    asking_turn_id: str
    state: BackgroundState
    background: bool
    redeliveries: int
    asked_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # C28. A peer that was asked or a subagent that was spawned.
    kind: DelegationKind = DelegationKind.PEER


@dataclass(frozen=True, slots=True)
class AskWindow:
    """What a bounded read of a tree's asks returned: the oldest `limit` rows and how many more
    the limit left out (0 = every ask). A reader never drops silently."""

    records: tuple[AskRecord, ...]
    omitted: int = 0


@dataclass(frozen=True, slots=True)
class TaskNode:
    """A turn of the tree. The root has no `handle` and no `state` (the rows do not know it)."""

    turn_id: str
    agent: str | None
    handle: str | None
    state: BackgroundState | None
    background: bool
    asked_at: datetime | None
    queued_ms: int | None
    run_ms: int | None
    children: tuple[TaskNode, ...] = ()
    # C28. What the child is; None for the root (a turn a person started).
    kind: DelegationKind | None = None

    @property
    def depth(self) -> int:
        """Levels from this node down, counting itself (a leaf is 1)."""
        return 1 + max((child.depth for child in self.children), default=0)


@dataclass(frozen=True, slots=True)
class TaskGraph:
    root: TaskNode
    unlinked: tuple[TaskNode, ...] = ()
    omitted_asks: int = 0

    @property
    def truncated(self) -> bool:
        """The read behind this graph left asks out: the tree is a prefix of the real one, and an
        ask whose parent was left out is listed under `unlinked`."""
        return self.omitted_asks > 0


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _callee_turn_ids(record: AskRecord) -> tuple[str, ...]:
    """Every turn id a callee of this ask may have run under: each redelivery generation, in both
    spellings (`.4` builds named them `peer-<id>`)."""
    ids: list[str] = []
    for generation in range(record.redeliveries + 1):
        ids.append(peer_workflow_id(record.handle, generation))
        ids.append(legacy_peer_workflow_id(record.handle, generation))
    return tuple(ids)


def build_task_graph(
    root_turn_id: str, records: tuple[AskRecord, ...], *, omitted: int = 0
) -> TaskGraph:
    """The tree under `root_turn_id` from the asks recorded beneath it. Pure and total: a cycle or
    a duplicate in the rows cannot loop (each record is placed at most once). `omitted` is how many
    asks the read left out; it is carried on the graph, never guessed at."""
    by_parent: dict[str, list[AskRecord]] = defaultdict(list)
    for record in records:
        by_parent[record.asking_turn_id].append(record)
    placed: set[str] = set()

    def node(record: AskRecord) -> TaskNode:
        placed.add(record.handle)
        turn_id = peer_workflow_id(record.handle, record.redeliveries)
        children = tuple(
            node(child)
            for owner in _callee_turn_ids(record)
            for child in _ordered(by_parent.get(owner, ()))
            if child.handle not in placed
        )
        return TaskNode(
            turn_id=turn_id,
            agent=record.target,
            handle=record.handle,
            state=record.state,
            background=record.background,
            asked_at=record.asked_at,
            queued_ms=_ms(record.asked_at, record.started_at),
            run_ms=_ms(record.started_at, record.finished_at),
            children=children,
            kind=record.kind,
        )

    top = tuple(
        node(r) for r in _ordered(by_parent.get(root_turn_id, ())) if r.handle not in placed
    )
    asker = next((r.asker for r in _ordered(by_parent.get(root_turn_id, ())) if r.asker), None)
    root = TaskNode(
        turn_id=root_turn_id,
        agent=asker,
        handle=None,
        state=None,
        background=False,
        asked_at=None,
        queued_ms=None,
        run_ms=None,
        children=top,
    )
    unlinked = tuple(node(r) for r in _ordered(tuple(records)) if r.handle not in placed)
    return TaskGraph(root=root, unlinked=unlinked, omitted_asks=max(0, omitted))


def _ordered(records: tuple[AskRecord, ...] | list[AskRecord]) -> list[AskRecord]:
    return sorted(records, key=lambda r: (r.asked_at, r.handle))
