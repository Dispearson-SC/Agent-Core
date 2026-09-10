"""The partitioned turn queue: one turn at a time per session, sessions in parallel.

Phase:   F2 (durability)
Tasks:   docs/TASKS.md#t-f2-03
Covers:  adapters/driving/workflow/turn_workflow.py - the queue and its enqueue helper

WHAT IS BEING DEFENDED (D19)
    A session is a conversation. Two turns of the SAME conversation running at once read
    and write the same history, so the second one starts from a transcript the first has
    not finished writing - the classic interleaving that produces an answer to a message
    the user has already superseded. Serialising per session is the fix.

    The cost of getting the fix wrong in the other direction is just as bad and much less
    obvious: a queue that serialises EVERYTHING makes every tenant wait behind every other
    tenant's slowest turn, and it looks perfectly healthy under a single-session test.

    So both halves are asserted here, and both are wall-clock properties: same partition
    key never overlaps, different partition keys do.

WHY THIS IS AN INTEGRATION TEST AND NOT A UNIT TEST
    The serialisation is not code in this repository - it is `partition_concurrency=1` on
    `dbos.Queue` plus a `queue_partition_key` per enqueue (docs/FIELD-NOTES.md, dbos
    2.31.1). A test with a fake queue would assert that the fake honours the key it was
    handed, which is a test of the fake. Only a real DBOS over a real Postgres can say
    whether the parameter was spelled right and whether the key derived from a `SessionRef`
    reaches the dequeue side at all.

    It skips cleanly when no Postgres is reachable, the same way the other integration
    tests in this directory do.

WHY THE KEY CARRIES THE TENANT
    `SessionId` is unique inside a tenant, not globally. Keying the partition on the
    session id alone would make two tenants that happen to pick the same session id
    serialise against each other - cross-tenant interference that no single-tenant test
    can see. The third pair below is here to pin that.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, cast

import psycopg
import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# DBOS keeps its own system tables; it gets its own database so a failed run here can be
# dropped without touching the schema the repository tests use.
_DBOS_DATABASE = "agent_core_queue_test"

# Long enough that two turns dequeued in the same polling tick demonstrably overlap, short
# enough that the whole test is a few seconds. The queue polls once a second by default,
# so a hold far below that would make "they overlapped" a measurement of scheduler jitter.
_HOLD_SECONDS = 0.5


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _dbos_conninfo() -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{_DBOS_DATABASE}"


@dataclass(frozen=True)
class _Span:
    """When one turn actually held the session, measured inside the turn itself."""

    partition: str
    message: int
    started: float
    ended: float

    def overlaps(self, other: _Span) -> bool:
        return self.started < other.ended and other.started < self.ended


class _HoldsTheSession:
    """Stands in for `StartTurn`: sleeps for a measurable interval and records the span.

    A real `StartTurn` would need a model, a store and a policy, and none of them change
    the answer to "were these two turns in flight at the same time?". What matters is that
    the workflow's own `_step_start` is the thing being held, so the interval measured is
    the interval the queue slot was actually occupied.
    """

    def __init__(self) -> None:
        self.spans: list[_Span] = []

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        started = time.monotonic()
        await asyncio.sleep(_HOLD_SECONDS)
        self.spans.append(
            _Span(
                partition=turn_workflow.session_partition_key(request.session),
                message=request.input.text.count("."),
                started=started,
                ended=time.monotonic(),
            )
        )
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


def _request(tenant: str, session: str, message: int) -> TurnRequest:
    # The message index travels in the text rather than in a field the domain does not
    # have; `_HoldsTheSession` reads it back out. Dots, so the value survives verbatim.
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId(tenant)),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="http",
            tenant_id=TenantId(tenant),
            roles=frozenset({"operator"}),
        ),
        profile_id="p-1",
        input=UserInput(text="." * message),
    )


# Two messages on one session, two messages on a second session of the same tenant, and
# two messages on a session of ANOTHER tenant that reuses the first session id.
#
# The order is a literal tuple and never a set: CLAUDE.md non-negotiable #7. Dispatch
# order decides which turn of a partition runs first, and deriving it from an unordered
# container would make this test assert something different on every run.
_DISPATCH: tuple[tuple[str, str, int], ...] = (
    ("t-1", "s-1", 1),
    ("t-1", "s-1", 2),
    ("t-1", "s-2", 1),
    ("t-1", "s-2", 2),
    ("t-2", "s-1", 1),
    ("t-2", "s-1", 2),
)


async def _drive(held: _HoldsTheSession) -> None:
    from dbos import DBOS, DBOSConfig

    config: DBOSConfig = {"name": "agent-core-queue-test", "database_url": _dbos_conninfo()}
    DBOS(config=config)
    DBOS.launch()
    try:
        handles = [
            await turn_workflow.enqueue_turn(_request(tenant, session, message))
            for tenant, session, message in _DISPATCH
        ]
        for handle in handles:
            await handle.get_result()
    finally:
        DBOS.destroy()


@pytest.mark.phase("F2")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_one_turn_at_a_time_per_session_while_sessions_run_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-03, both halves of it.

    Same partition key: strictly sequential, whatever else is in flight.
    Different partition key: genuinely concurrent, including across tenants that reused a
    session id.
    """
    enqueue = getattr(turn_workflow, "enqueue_turn", None)
    assert enqueue is not None, (
        "turn_workflow exposes no enqueue_turn: there is no partitioned queue, so every "
        "turn of a session runs the moment it arrives and two messages interleave over "
        "one conversation. docs/TASKS.md#t-f2-03"
    )

    held = _HoldsTheSession()
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(start_turn=cast("Any", held)),
    )

    asyncio.run(_drive(held))

    assert len(held.spans) == len(_DISPATCH), (
        f"{len(held.spans)} of {len(_DISPATCH)} enqueued turns ran. A turn that never "
        "leaves the queue is not serialisation, it is a deadlock."
    )

    by_partition: dict[str, list[_Span]] = {}
    for span in held.spans:
        by_partition.setdefault(span.partition, []).append(span)

    assert len(by_partition) == 3, (
        f"the six turns fell into {sorted(by_partition)}. Expected one partition per "
        "(tenant, session): a key that collapses two tenants onto one partition makes "
        "them queue behind each other."
    )

    for partition, spans in sorted(by_partition.items()):
        first, second = sorted(spans, key=lambda span: span.started)
        assert not first.overlaps(second), (
            f"two turns of partition {partition} were in flight at the same time "
            f"({first.started:.3f}-{first.ended:.3f} and "
            f"{second.started:.3f}-{second.ended:.3f}). One session runs one turn at a "
            "time or the second turn reads a history the first has not finished writing. "
            "docs/TASKS.md#t-f2-03"
        )

    firsts = {
        partition: min(spans, key=lambda span: span.started)
        for partition, spans in by_partition.items()
    }
    for left, right in (("t-1/s-1", "t-1/s-2"), ("t-1/s-1", "t-2/s-1")):
        assert firsts[left].overlaps(firsts[right]), (
            f"partitions {left} and {right} did not overlap in wall-clock "
            f"({firsts[left].started:.3f}-{firsts[left].ended:.3f} and "
            f"{firsts[right].started:.3f}-{firsts[right].ended:.3f}). They are different "
            "conversations: serialising them makes every tenant wait behind every other "
            "tenant's slowest turn, and nothing in a single-session test would show it."
        )
