"""A worker killed mid-turn must not silence its session forever.

Phase:   F2 - Durability
Tasks:   docs/TASKS.md#t-f2-12
Covers:  Core/src/agent_core/composition.py - `dbos_config` and the pin it carries

THE FAILURE THIS FILE EXISTS FOR, AND WHY IT IS THE WORST KIND
    `agent-core-turns` is partitioned with `partition_concurrency=1` (docs/TASKS.md#t-f2-03).
    On dbos 2.31.1 the partitioned-dequeue mutual-exclusion probe in `_sys_db.py` is
    UNSCOPED BY DESIGN: a PENDING row on a partition is that partition's occupant whatever
    `application_version` wrote it. Startup recovery in `_dbos.py` is the opposite - it asks
    `get_pending_workflows(executor_id, GlobalParams.app_version)`, scoped to the version
    this process claims.

    So the two halves disagree, and the disagreement is silent. A worker killed mid-turn
    leaves PENDING behind; if the next process claims a DIFFERENT version, that row is never
    recovered and never releases the slot. Every later turn for that session is accepted,
    enqueued, and never dequeued. Nothing raises, nothing logs, and there is nothing to
    poll but silence - it reads exactly like "the turn never started". A crash that loses a
    turn is recoverable; a crash that makes one customer's conversation stop answering with
    no error anywhere is only found when they complain.

    And the version DOES change on every deploy unless somebody pins it: DBOS's default is
    an md5 of the registered workflow sources (`_dbos.py::compute_app_version`), so any
    edit to `turn_workflow.py` is by definition a new version.

WHAT IS ASSERTED HERE, IN THE ORDER IT IS ASSERTED
    1. the composition root pins a version that does not depend on this process, so the row
       a dying worker leaves carries a version the NEXT deploy still claims. No
       infrastructure - it is a property of the wiring;
    2. behaviourally, through real DBOS and real Postgres: a session whose previous turn was
       left PENDING by a killed worker RUNS its next turn. The DEPLOY half of that cannot be
       staged inside one process - the source hash does not move while the interpreter is
       up - so it is carried by the column: the abandoned row is read back and asserted to
       name the pin rather than a hash, which is precisely what survives a deploy;
    3. and the negative half, in the same run, because (2) alone is passable by an engine
       that recovers everything: a row carrying a version nobody claims is still silent, and
       a third, untouched session still runs. Without (3), (2) proves only that DBOS works.

WHY THE NEGATIVE HALF IS NOT A DUPLICATE OF test_delivery.py's LAST TEST
    That one reproduces the DEFECT - it is the barrier's evidence, and its docstring says
    the recovering configurations "are named in docs/TASKS.md; neither is wired here". This
    file is the wiring, so the two sessions have to sit side by side in one run: the
    difference between them is not the engine and not the fake, it is the single column the
    pin decides.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
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

_ANSWER = "the account is frozen"

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# Its own DBOS system database, for the reason every sibling that launches DBOS gives: a
# failed run here is destroyed without touching state another file owns, and this file
# deliberately corrupts its own.
_DBOS_DATABASE = "agent_core_partition_recovery_test"

# How long a turn that MUST run is given. Not a tolerance any assertion leans on - the
# proof is the turn running, and a slow machine only reaches it later. It exists so a queue
# that never dequeues fails with a sentence instead of hanging the suite.
_RUN_DEADLINE_SECONDS = 30.0

# How long a turn that must NOT run is watched for. This one IS a bound rather than a
# proof, and it is the only honest shape available: "never" is not observable. It is long
# enough that dbos.Queue's 1.0s poll has been round eight times.
_BLOCKED_DEADLINE_SECONDS = 8.0

# The version a build that is gone wrote. `_abandon` puts this on ONE session's row to
# stand in for the deploy that changed the source hash - which is what the unpinned default
# does on every deploy, by construction.
_A_VERSION_THAT_IS_GONE = "a-version-that-is-gone"


class _RecordingChannel:
    """A `Channel` by shape alone - the Protocol is structural on purpose (t-f3-13)."""

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _FinishesImmediately:
    """Stands in for `StartTurn`: one finished turn, no suspension, no collaborators.

    What is under test is whether the turn is ever DEQUEUED. A real `StartTurn` would need
    a model, a store and a policy, none of which change the answer, and all of which would
    make a blocked partition look like a slow provider.
    """

    def __init__(self, result: TurnResult) -> None:
        self._result = result
        self.calls = 0

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.calls += 1
        return TurnOutcome(turn_id=turn_id, result=self._result)


def _request(session: str) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId("t-f2")),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="telegram",
            tenant_id=TenantId("t-f2"),
            roles=frozenset({"operator"}),
        ),
        profile_id="p-1",
        input=UserInput(text="freeze the account"),
    )


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _dbos_conninfo(database: str = _DBOS_DATABASE) -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{database}"


def _drop_databases() -> None:
    """Destroy this file's own DBOS state.

    Required of every launch helper in this directory - test_delivery.py asserts it over
    these sources - because a PENDING row inherited from a killed run blocks its partition
    for reasons that are not in the code under test.
    """
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DBOS_DATABASE}_dbos_sys", _DBOS_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def _production_config() -> dict[str, str]:
    """The DBOS configuration a production process is handed, built by the wiring.

    THE `getattr` IS THE RED, AND IT IS DELIBERATE
        Before docs/TASKS.md#t-f2-12 nothing in `Core/src` constructed a `DBOSConfig` at
        all, so this assertion is what fails first and says why in one sentence, rather
        than every test below dying on an AttributeError that names nothing. It stays
        afterwards as the statement that the CONFIG is the composition root's to own: a
        launcher that builds its own two-key dict beside this one is the defect back.
    """
    builder = getattr(composition, "dbos_config", None)
    assert builder is not None, (
        "composition.py exports no dbos_config. Nothing in Core/src constructs a DBOSConfig "
        "at all, so every process launches DBOS with the DEFAULT application_version - an "
        "md5 of the registered workflow sources - and any deploy is a new version. A worker "
        "killed mid-turn then leaves a PENDING row no later process will ever recover, and "
        "because the partitioned-dequeue probe is unscoped that session's only slot is held "
        "forever. docs/TASKS.md#t-f2-12"
    )
    settings = composition.Settings(app_conninfo=_dbos_conninfo())
    return dict(composition.dbos_config(settings))


class _Dbos:
    """A launched DBOS for the length of one block, destroyed however the block ends.

    It launches with the PRODUCTION configuration rather than a two-key dict assembled
    here, which is the whole point of the file: a test that built its own config would
    prove a property of the test.

    `drop=False` is used by the block that must INHERIT what the previous one abandoned,
    exactly as the next process to start would.
    """

    def __init__(self, *, drop: bool = True) -> None:
        self._drop = drop

    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        if self._drop:
            _drop_databases()
        DBOS(config=cast("DBOSConfig", _production_config()))
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


def _bind(monkeypatch: pytest.MonkeyPatch, *, start_turn: _FinishesImmediately) -> None:
    """Wire the workflow's collaborators for one test, and unwire them afterwards.

    `monkeypatch` rather than `bind_dependencies`, for the reason test_delivery.py gives:
    the binding is a process-wide module global by design (a DBOS workflow is a
    module-level object), which is exactly what makes it leak into the next test.
    """
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", start_turn),
            channels=ChannelRegistry((("telegram", _RecordingChannel()),)),
        ),
    )


def _system_conninfo() -> str:
    return _dbos_conninfo(f"{_DBOS_DATABASE}_dbos_sys")


def _abandon(workflow_id: str, *, application_version: str | None = None) -> None:
    """Turn one finished row into exactly what a killed worker leaves behind.

    `status = 'PENDING'` is the whole of it when `application_version` is None, and that is
    the honest case: a dying worker writes no version, it simply stops, and the row keeps
    the version the build that wrote it claimed. Passing a version stands in for a DEPLOY
    having happened since - the source hash moved, so the row names a build that is gone.
    """
    with psycopg.connect(_system_conninfo(), connect_timeout=5, autocommit=True) as system:
        if application_version is None:
            system.execute(
                "UPDATE dbos.workflow_status SET status = 'PENDING' WHERE workflow_uuid = %s",
                (workflow_id,),
            )
        else:
            system.execute(
                "UPDATE dbos.workflow_status "
                "SET status = 'PENDING', application_version = %s "
                "WHERE workflow_uuid = %s",
                (application_version, workflow_id),
            )


def _row_version(workflow_id: str) -> str:
    with psycopg.connect(_system_conninfo(), connect_timeout=5, autocommit=True) as system:
        row = system.execute(
            "SELECT application_version FROM dbos.workflow_status WHERE workflow_uuid = %s",
            (workflow_id,),
        ).fetchone()
    assert row is not None, f"no dbos.workflow_status row for {workflow_id}"
    return str(row[0])


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_the_composition_root_pins_a_version_that_does_not_move_between_deploys() -> None:
    """The property that makes recovery possible at all, asserted without infrastructure.

    DBOS recovers a PENDING row only when the row's `application_version` equals the one
    the recovering process claims (`_dbos.py`, `_sys_db.get_pending_workflows`). Left to
    the default, that value is an md5 of the registered workflow sources - so it changes
    the moment `turn_workflow.py` changes, which is to say on every deploy that matters.

    So the assertion is that the version is a CONSTANT of the wiring and not a function of
    this process: two containers built from different settings claim the same version. A
    pin that happened to be derived from something deployment-specific would satisfy
    "carries an application_version" and still lose the session.
    """
    config = _production_config()

    assert config.get("application_version"), (
        f"the production DBOS config pins no application_version: {sorted(config)}. DBOS "
        "then computes one from a hash of the registered workflow sources, so the next "
        "deploy claims a different version and never recovers this build's PENDING rows."
    )
    other = composition.dbos_config(
        composition.Settings(app_conninfo="postgresql://elsewhere/another_database")
    )
    assert other["application_version"] == config["application_version"], (
        f"the version depends on the deployment: {other['application_version']!r} vs "
        f"{config['application_version']!r}. Recovery compares the row's version with the "
        "recovering process's, so a version that moves with the settings is the default "
        "defect wearing a pin's clothes."
    )
    assert config["application_version"] == composition.PINNED_DBOS_APPLICATION_VERSION, (
        "the pin is not the module constant, so there is nothing for a reviewer to bump "
        "when a workflow signature changes - which is the one rule a pinned version puts "
        "on whoever deploys it. See composition.dbos_config's docstring."
    )
    assert config["database_url"] == _dbos_conninfo(), (
        f"DBOS was pointed at {config['database_url']!r} rather than the app conninfo the "
        "container was built with. It derives its own system database from this URL, so a "
        "second opinion about which database this is means two sets of durable state."
    )
    assert "conductor_key" not in config, (
        "a conductor key was configured from nowhere. The recovery-service route is the "
        "OTHER fix for this defect and it is opt-in: under it DBOS skips local "
        "version-scoped recovery entirely, and it must not appear unless a deployment "
        "asked for it."
    )
    with_conductor = composition.dbos_config(
        composition.Settings(dbos_conductor_key="not-a-real-key")
    )
    assert with_conductor.get("conductor_key") == "not-a-real-key", (
        "a configured conductor key never reaches DBOS, so the documented alternative to "
        "the pin cannot actually be selected."
    )


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_session_left_pending_by_a_killed_worker_runs_its_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-12, reproduced causally and then fixed by the wiring.

    Three sessions in one restarted process, because separately each has an innocent
    explanation:

      - `s-killed` was left PENDING by a worker that died, with the version its build
        wrote untouched. Because that version is PINNED, the restarted process claims the
        same one, recovery picks the row up, the slot is released, and the session's NEXT
        turn runs. That is the anchor;
      - `s-orphaned` was left PENDING naming a build that is gone - what the unpinned
        default produces on every deploy. It is still silent, which is what gives the line
        above teeth: an engine that recovered everything would pass the first assertion
        while proving nothing about the pin;
      - `s-free` was never touched and runs, which is what separates "this partition is
        blocked" from "DBOS or the fake is broken for this whole block".
    """
    start_turn = _FinishesImmediately(TurnResult(text=_ANSWER))
    _bind(monkeypatch, start_turn=start_turn)

    async def drive() -> tuple[str, bool, bool, str, bool]:
        async with _Dbos():
            killed = await turn_workflow.enqueue_turn(_request("s-killed"))
            await asyncio.wait_for(killed.get_result(), timeout=_RUN_DEADLINE_SECONDS)
            orphaned = await turn_workflow.enqueue_turn(_request("s-orphaned"))
            await asyncio.wait_for(orphaned.get_result(), timeout=_RUN_DEADLINE_SECONDS)

            written_version = _row_version(killed.workflow_id)
            _abandon(killed.workflow_id)
            _abandon(orphaned.workflow_id, application_version=_A_VERSION_THAT_IS_GONE)

        # Deliberately NOT dropping: this block inherits both abandoned rows, exactly as
        # the next process to start would.
        async with _Dbos(drop=False):
            again = await turn_workflow.enqueue_turn(_request("s-killed"))
            try:
                await asyncio.wait_for(again.get_result(), timeout=_RUN_DEADLINE_SECONDS)
                recovered = True
            except TimeoutError:
                recovered = False

            blocked = await turn_workflow.enqueue_turn(_request("s-orphaned"))
            try:
                await asyncio.wait_for(
                    blocked.get_result(), timeout=_BLOCKED_DEADLINE_SECONDS
                )
                orphan_ran = True
            except TimeoutError:
                orphan_ran = False
            orphan_status = (await blocked.get_status()).status

            free = await turn_workflow.enqueue_turn(_request("s-free"))
            try:
                await asyncio.wait_for(free.get_result(), timeout=_RUN_DEADLINE_SECONDS)
                free_ran = True
            except TimeoutError:
                free_ran = False

        return written_version, recovered, orphan_ran, orphan_status, free_ran

    written_version, recovered, orphan_ran, orphan_status, free_ran = asyncio.run(drive())

    assert written_version == composition.PINNED_DBOS_APPLICATION_VERSION, (
        f"the killed worker's row carries application_version {written_version!r}, not the "
        f"pin {composition.PINNED_DBOS_APPLICATION_VERSION!r}. That value is what the next "
        "process compares against, so a row stamped with a computed hash is unrecoverable "
        "the moment the source changes - and the source changes on every deploy."
    )
    assert free_ran, (
        "no turn ran at all, on any session, so nothing below proved anything about "
        "partitions - it proved DBOS or the fake was broken for this whole block."
    )
    assert recovered, (
        "the session whose previous turn was left PENDING by a killed worker never ran its "
        "next turn. It was accepted, enqueued, and never dequeued - no exception, no log, "
        "nothing to poll but silence, which reads as 'the turn never started'. Either the "
        "application_version is not pinned across the restart, or something else is holding "
        "the partition. docs/TASKS.md#t-f2-12"
    )
    assert not orphan_ran, (
        "a turn ran on a session whose partition still holds a PENDING row naming a build "
        "that is gone. If dbos has started recovering foreign versions, this negative half "
        "has outlived its cause - and so has the pin it gives teeth to. Record the version "
        "that changed and retire BOTH, never one alone."
    )
    assert orphan_status == "ENQUEUED", (
        f"the blocked turn ended {orphan_status!r}. The defect's signature is SILENCE - the "
        "turn sits at ENQUEUED with nothing raised - and an ERROR here would mean something "
        "else is failing and this assertion is watching the wrong thing."
    )
