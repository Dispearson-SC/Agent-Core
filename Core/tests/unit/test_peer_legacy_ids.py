"""C1k (R3-001/R4-001, D34): the lease and the cancel cascade still see `.4` peer workflows.

Subject: adapters/driving/workflow/turn_workflow.py (`legacy_peer_workflow_id`,
`DurablePeerTurnStarter.exists/retire`, `_peer_workflow_ids`).

Until `.4` a peer workflow was named `peer-<correlation>` (generation 0) or
`peer-<correlation>-r<n>`. `.5` names it `uuid5(...)` (D33). A callee still running under a live
`.4` executor is therefore invisible to a lookup of the new id, and the lease would requeue its ask
even with `AGENT_CORE_RETIRE_OTHER_VERSIONS` off. The legacy id must be consulted too.

DBOS is replaced by a table of workflow statuses: the behaviour under test is which ids are asked
about and what is decided, not the engine.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow as tw


def _sync[**P](test: Callable[P, Awaitable[None]]) -> Callable[P, None]:
    """The suite drives coroutines with `asyncio.run` (no async plugin): run an async test body."""

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))  # type: ignore[arg-type]

    return run


NEW_VERSION = "agent-core-turns.5"
OLD_VERSION = "agent-core-turns.4"


class _FakeDbos:
    application_version = NEW_VERSION
    statuses: dict[str, SimpleNamespace] = {}
    cancelled: list[str] = []

    @classmethod
    async def get_workflow_status_async(cls, workflow_id: str) -> SimpleNamespace | None:
        return cls.statuses.get(workflow_id)

    @classmethod
    async def cancel_workflow_async(cls, workflow_id: str) -> None:
        cls.cancelled.append(workflow_id)
        found = cls.statuses.get(workflow_id)
        if found is not None:
            found.status = "CANCELLED"


@dataclass
class _Mailbox:
    generations: int = 0
    cancel_open_calls: list[str] = field(default_factory=list)

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        self.cancel_open_calls.append(turn_id)
        return ()

    async def redeliveries(self, correlation_id: str) -> int:
        return self.generations


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> tuple[type[_FakeDbos], _Mailbox]:
    _FakeDbos.statuses = {}
    _FakeDbos.cancelled = []
    mailbox = _Mailbox()
    monkeypatch.setattr(tw, "DBOS", _FakeDbos)
    monkeypatch.setattr(tw, "_require_peer_seat", lambda: SimpleNamespace(mailbox=mailbox))
    return _FakeDbos, mailbox


def _put(workflow_id: str, status: str, version: str) -> SimpleNamespace:
    found = SimpleNamespace(status=status, app_version=version)
    _FakeDbos.statuses[workflow_id] = found
    return found


def test_the_legacy_id_is_the_prefixed_correlation_id_with_a_retry_suffix() -> None:
    assert tw.legacy_peer_workflow_id("c1", 0) == "peer-c1"
    assert tw.legacy_peer_workflow_id("c1", 2) == "peer-c1-r2"


@pytest.mark.parametrize("generation", [0, 2])
@_sync
async def test_a_legacy_workflow_open_under_a_live_old_version_counts_as_existing_by_default(
    engine: Any, generation: int
) -> None:
    _put(tw.legacy_peer_workflow_id("c1", generation), "PENDING", OLD_VERSION)

    assert await tw.DurablePeerTurnStarter().exists("c1", generation) is True


@_sync
async def test_a_legacy_workflow_is_absent_only_when_the_operator_says_the_old_executor_is_gone(
    engine: Any,
) -> None:
    _put("peer-c1", "PENDING", OLD_VERSION)

    assert await tw.DurablePeerTurnStarter(retire_other_versions=True).exists("c1") is False


@pytest.mark.parametrize("status", ["CANCELLED", "ERROR"])
@_sync
async def test_a_legacy_workflow_that_ended_without_an_answer_is_absent(
    engine: Any, status: str
) -> None:
    _put("peer-c1", status, OLD_VERSION)

    assert await tw.DurablePeerTurnStarter().exists("c1") is False


@_sync
async def test_a_finished_legacy_workflow_still_exists(engine: Any) -> None:
    _put("peer-c1", "SUCCESS", OLD_VERSION)

    assert await tw.DurablePeerTurnStarter(retire_other_versions=True).exists("c1") is True


@_sync
async def test_the_current_id_decides_when_both_exist(engine: Any) -> None:
    _put("peer-c1", "PENDING", OLD_VERSION)
    _put(tw.peer_workflow_id("c1", 0), "PENDING", NEW_VERSION)

    assert await tw.DurablePeerTurnStarter(retire_other_versions=True).exists("c1") is True


@_sync
async def test_retire_cancels_a_legacy_workflow_and_the_asks_it_made(engine: Any) -> None:
    dbos, mailbox = engine
    _put("peer-c1-r1", "PENDING", OLD_VERSION)

    await tw.DurablePeerTurnStarter(retire_other_versions=True).retire("c1", 1)

    assert dbos.cancelled == ["peer-c1-r1"]
    assert mailbox.cancel_open_calls == ["peer-c1-r1"], "its own asks were made under that turn id"


@_sync
async def test_retire_leaves_a_workflow_that_is_unknown_or_finished_alone(engine: Any) -> None:
    dbos, mailbox = engine
    _put("peer-c1", "SUCCESS", OLD_VERSION)

    await tw.DurablePeerTurnStarter(retire_other_versions=True).retire("c1", 0)
    await tw.DurablePeerTurnStarter(retire_other_versions=True).retire("c2", 0)

    assert dbos.cancelled == [] and mailbox.cancel_open_calls == []


@_sync
async def test_the_cancel_cascade_names_the_legacy_ids_DBOS_knows(engine: Any) -> None:
    _, mailbox = engine
    mailbox.generations = 1
    _put("peer-c1-r1", "PENDING", OLD_VERSION)

    ids = await tw._peer_workflow_ids("c1")

    assert set(ids) == {tw.peer_workflow_id("c1", 0), tw.peer_workflow_id("c1", 1), "peer-c1-r1"}


# --- C1l: which dead generations were the callee's own failure --------------------------------


@_sync
async def test_only_an_errored_workflow_counts_as_a_callee_failure(
    engine: tuple[type[_FakeDbos], _Mailbox],
) -> None:
    """The lease requeues a generation whose workflow is gone (never started), CANCELLED (a
    deploy retired it) or ERROR (the callee raised). Only the last is the callee's failure and
    only it counts toward the redelivery cap (`DurablePeerTurnStarter.failed`)."""
    starter = tw.DurablePeerTurnStarter()
    _put(tw.peer_workflow_id("raised", 0), "ERROR", NEW_VERSION)
    _put(tw.peer_workflow_id("retired", 0), "CANCELLED", NEW_VERSION)
    _put(tw.peer_workflow_id("running", 0), "PENDING", NEW_VERSION)
    _put(tw.legacy_peer_workflow_id("old-raised", 1), "ERROR", OLD_VERSION)

    assert await starter.failed("raised", 0) is True
    assert await starter.failed("retired", 0) is False
    assert await starter.failed("running", 0) is False
    assert await starter.failed("never-started", 0) is False
    assert await starter.failed("raised", 1) is False, "another generation is another workflow"
    assert await starter.failed("old-raised", 1) is True, "the .4 spelling is consulted too"
