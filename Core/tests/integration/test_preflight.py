"""What is not ready, in ONE pass - and the approval that finally wakes its turn.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-04, docs/TASKS.md#t-f11-18, docs/TASKS.md#t-f11-19
Covers:  adapters/driving/cli/preflight.py, agent_core/composition.py, agent_core/main.py

WHY THESE THREE ANCHORS SHARE A TEST FILE
    They are one thing: the composition root finishing the cold start. The preflight can
    only answer "what is not ready" because the container already knows every seat it
    needs; the audit reader is the last driven adapter `main.py` was hand-building because
    the container had no seat; and the decision signal is the last seat that was wired to
    a function that raised. All three fail the same way - silently, at the worst moment.

WHAT IS ACTUALLY BEING DEFENDED
    1. ONE PASS. Every failure in F11's list surfaced one at a time, three commands apart:
       the database refused, then the credential was missing, then the model id did not
       resolve, then the toolset was unregistered. A preflight that raised on the first
       item would be the same afternoon with a nicer message, so the assertion below is
       that a deployment broken in FOUR different ways is told about all four at once.

    2. NO CREDENTIAL VALUE, EVER. A readiness report is exactly the kind of output someone
       pastes into a chat window. `MINIMAX_API_KEY: set` and `GEMINI_API_KEY: missing` is
       the whole vocabulary, and the test below plants a synthetic value and asserts the
       rendered report does not contain it.

    3. THE APPROVAL WAKES THE TURN. `composition._decision_signal` raised
       `NotImplementedError`, so D25 recorded a decision and the turn it approved waited
       three days and expired. That is proved here through a REAL DBOS wait woken by the
       CONTAINER's own `decide_approval` - not by `turn_workflow.signal_decision` called
       directly, which is what every existing test does and is exactly why the missing
       binding survived: the collaborator a test supplies is the one production never binds.

NOTHING HERE PRINTS, LOGS OR ASSERTS ON A REAL CREDENTIAL. The only secret-shaped string
in this file is a synthetic one, planted to prove it never reaches the report.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from dbos import DBOS, DBOSConfig, SetWorkflowID

from agent_core import composition, main
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.composition import Settings
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DATABASE = "agent_core_preflight_test"

# A database name no deployment will have created, on a server that IS reachable. That
# pairing is the point: "the server is down" and "the database is missing" are two
# different remedies, and a preflight that cannot tell them apart sends an operator to fix
# the wrong thing.
_ABSENT_DATABASE = "agent_core_preflight_absent"

# Planted, synthetic, and asserted ABSENT from every rendered report below. It is not a
# credential and never was one; what it stands in for is.
_SYNTHETIC_SECRET = "s3cret-value-that-must-never-be-rendered"  # noqa: S105

# How long the woken workflow is given to report what it received. Generous because it
# crosses a real durable queue; the test never waits on it when the wake works.
_WAKE_TIMEOUT_SECONDS = 30.0


def _conninfo_for(database: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{database}\1", _ADMIN_CONNINFO)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _preflight_module() -> ModuleType | None:
    """The module t-f11-04 adds, stated as a claim rather than an ImportError.

    An import error at collection time is not a failing test - it is a test that never
    ran. docs/WAVES.md's test-first contract asks for a red ASSERTION, so the absence is
    asserted with the sentence that says what is missing.
    """
    try:
        return importlib.import_module("agent_core.adapters.driving.cli.preflight")
    except ModuleNotFoundError:
        return None


def _broken_profile(directory: Path) -> None:
    """A profile that PARSES and cannot be served, in three different ways at once.

    An unregistered toolset, a model no provider resolves, and therefore a credential
    nothing can supply. All three are real failures of the same file, and all three must
    appear in the same report.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.yaml").write_text(
        "\n".join(
            (
                "id: broken_agent",
                "persona: |",
                "  A profile that parses and cannot take a single turn.",
                "model: nowhere/does-not-exist",
                "toolsets:",
                "  - not_registered",
                "mcp_servers: []",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _broken_settings(tmp_path: Path) -> Settings:
    profiles = tmp_path / "profiles"
    _broken_profile(profiles)
    return Settings(
        app_conninfo=_conninfo_for(_ABSENT_DATABASE),
        profiles_dir=profiles,
        # Absent on purpose: a deployment that manages policy some other way is not the
        # same as one that has no rules, and the report has to be able to say so.
        policy_dir=tmp_path / "no-policy-here",
        skills_dir=tmp_path / "skills",
        media_dir=tmp_path / "media",
    )


@pytest.mark.phase("F11")
@_needs_postgres
def test_a_deployment_broken_four_ways_is_told_all_four_in_one_pass(
    tmp_path: Path,
) -> None:
    """docs/TASKS.md#t-f11-04 - the row this phase is worth the most.

    The database is missing, the credential is missing, the model resolves to no provider
    and the profile names a toolset nothing registered. Each of those, historically,
    surfaced three commands after the last. One call, one report, four findings.
    """
    module = _preflight_module()
    assert module is not None, (
        "there is no adapters/driving/cli/preflight.py, so a deployment still learns what "
        "is wrong one failure at a time - the database, then the credential, then the "
        "model id, then the toolset, each three commands after the last. "
        "docs/TASKS.md#t-f11-04"
    )

    report = asyncio.run(module.preflight(_broken_settings(tmp_path), environ={}))

    assert not report.ready, (
        "the preflight called this deployment ready while its database does not exist, "
        "its model resolves to no provider and its only profile names an unregistered "
        "toolset."
    )
    categories = {check.category for check in report.failures}
    assert {"database", "credential", "model", "profile"} <= categories, (
        f"one pass reported {sorted(categories)}. A preflight that stops at the first "
        "failure is the same afternoon with a nicer message: all four of these are "
        "broken right now and the operator has to be told all four at once. "
        "docs/TASKS.md#t-f11-04"
    )
    assert all(check.remedy for check in report.failures), (
        "a failing check carries no remedy. 'not ready' without what to do about it is "
        f"the same afternoon in one line: {[c.name for c in report.failures if not c.remedy]}"
    )

    rendered = report.render()
    assert _ABSENT_DATABASE in rendered and "not_registered" in rendered, (
        "the rendered report does not name the missing database and the unregistered "
        f"toolset by name, so it cannot be acted on:\n{rendered}"
    )


@pytest.mark.phase("F11")
@_needs_postgres
def test_a_credential_is_reported_by_name_and_never_by_value(tmp_path: Path) -> None:
    """NEVER PRINT A CREDENTIAL VALUE. 'set' and 'missing' is the whole vocabulary.

    A readiness report is exactly the kind of output someone pastes into a chat window,
    and the one check that has to read a secret to answer is the one that must never
    echo it.
    """
    module = _preflight_module()
    assert module is not None, "there is no preflight module; see the test above."

    report = asyncio.run(
        module.preflight(
            _broken_settings(tmp_path),
            environ={"NOWHERE_API_KEY": _SYNTHETIC_SECRET},
        )
    )
    rendered = report.render()

    assert _SYNTHETIC_SECRET not in rendered, (
        "the rendered preflight report contains a credential VALUE. This output is pasted "
        "into chat windows and issue trackers; the vocabulary is 'set' and 'missing'."
    )
    assert "NOWHERE_API_KEY" in rendered, (
        "the report does not name the credential variable, so an operator cannot tell "
        f"which one to set:\n{rendered}"
    )
    assert not any(check.category == "credential" for check in report.failures), (
        "a credential that IS set was still reported as a failure, which teaches an "
        "operator to ignore the report."
    )


@pytest.mark.phase("F11")
@_needs_postgres
def test_serve_and_console_refuse_to_start_on_a_hard_failure(tmp_path: Path) -> None:
    """The preflight is a GATE, not a report nobody runs. t-f11-04.

    `main.py` stays thin - the gate is one call - but both entry points make it, and a
    deployment that is not ready does not get a listening socket or a prompt.
    """
    gate = getattr(main, "refuse_unless_ready", None)
    assert gate is not None, (
        "main.py exposes no preflight gate, so `serve` and `console` still start against "
        "a deployment that cannot take a turn. docs/TASKS.md#t-f11-04"
    )

    for entry_point in (main.serve, main.run_console):
        assert "refuse_unless_ready" in inspect.getsource(entry_point), (
            f"{entry_point.__name__} does not run the preflight, so it starts on a "
            "deployment the preflight would have refused."
        )

    printed: list[str] = []
    with pytest.raises(SystemExit):
        gate(_broken_settings(tmp_path), write_line=printed.append)

    assert len(printed) > 1, (
        "the gate refused without printing the list it refused on. A refusal with no "
        f"report is the failure ladder again: {printed}"
    )


class _RecordingConnection:
    """Enough of a psycopg connection for a SELECT that returns nothing."""

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: object, **__: object) -> _RecordingConnection:
        return self

    def fetchall(self) -> list[Any]:
        return []


class _RecordingPool:
    """A pool that records that it was checked out, and connects to nothing.

    `composition.PoolConnections` exists so that WHICH pool an adapter reads through is a
    value a test can look at (its own docstring). This is the other half of that: two
    identical pool objects cannot be told apart by comparison, but they can be told apart
    by which one was used.
    """

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self.checkouts = 0

    def connection(self) -> _RecordingConnection:
        self.checkouts += 1
        return _RecordingConnection()


@pytest.mark.phase("F11")
def test_the_audit_reader_seat_reads_through_the_audit_pool_and_retires_the_hand_built_one(
    tmp_path: Path,
) -> None:
    """docs/TASKS.md#t-f11-18. The pool is the whole assertion.

    `t-f1-14` put the audit sink on a separate pool so an audit write survives the
    rollback of the turn it describes (CLAUDE.md non-negotiable #6). A READER that
    borrowed the domain pool would reintroduce exactly the coupling that separation
    exists to prevent - and it would look like a tidy-up in review.
    """
    profiles = tmp_path / "profiles"
    _broken_profile(profiles)
    # The shipped profiles would do, but this one needs no registered toolset to LOAD -
    # and `build_container` refuses an unservable profile (t-f11-05), so the seat under
    # test would never be reached.
    settings = Settings(
        app_conninfo="postgresql://localhost/agent_core_seat_test",
        audit_conninfo="postgresql://localhost/agent_core_seat_audit",
        profiles_dir=Path(composition.__file__).resolve().parents[2] / "profiles",
        policy_dir=tmp_path / "no-policy",
        skills_dir=tmp_path / "skills",
        media_dir=tmp_path / "media",
    )
    container = composition.build_container(settings, pool_factory=_RecordingPool)

    reader = getattr(container, "audit_reader", None)
    assert reader is not None, (
        "Container has no `audit_reader` seat, so `main.py` still hand-builds a third "
        "driven adapter over `audit_tool_calls` - the only place outside composition.py "
        "that chooses one. docs/TASKS.md#t-f11-18"
    )

    assert not hasattr(main, "_PgToolCallLog"), (
        "main._PgToolCallLog is still there. The container now has the seat, so the "
        "hand-built driven adapter and its SQL are the thing t-f11-18 retires."
    )

    console = main.build_console(container)
    # Reaching for the console's own seat on purpose: `build_console` CHOOSES this
    # collaborator, and the choice - which pool the trail is read through - is what is
    # under test. Passing one in would assert that the parameter works.
    log = console._tool_calls  # noqa: SLF001
    asyncio.run(log.for_turn(TurnId(str(uuid4()))))

    assert container.audit_pool.checkouts == 1, (
        "the console's tool-call read did not go through the AUDIT pool. A reader on the "
        "domain pool reintroduces the coupling non-negotiable #6 keeps the sink away from."
    )
    assert container.domain_pool.checkouts == 0, (
        "the console's tool-call read checked out the DOMAIN pool. The trail is written on "
        "the audit pool and must be read on it."
    )


@DBOS.workflow()
async def _await_a_human_answer() -> dict[str, object]:
    """A turn, reduced to the one thing t-f11-19 is about: it is WAITING.

    Deliberately not `run_turn_workflow`: everything that surrounds the wait - the model,
    the tools, the delivery - is machinery this assertion is not about, and driving it
    would make a failure here mean six things. What is real is the wait: the same
    `DBOS.recv_async`, on the same topic, addressed by the same workflow id.
    """
    answer = await DBOS.recv_async(
        turn_workflow.RESUME_TOPIC, timeout_seconds=_WAKE_TIMEOUT_SECONDS
    )
    if answer is None:
        return {"woken": False}
    return {
        "woken": True,
        "approved": bool(answer.approved),
        "note": answer.note,
        "tool_call_id": str(answer.tool_call_id),
    }


def _drop_databases() -> None:
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        for database in (
            f"{_APP_DATABASE}_dbos_sys",
            f"{_APP_DATABASE}_dbos",
            _APP_DATABASE,
        ):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


@pytest.mark.phase("F11")
@pytest.mark.silent
@_needs_postgres
def test_an_approval_recorded_through_the_container_wakes_the_turn_that_was_waiting(
    tmp_path: Path,
) -> None:
    """docs/TASKS.md#t-f11-19. The seat that recorded a decision and woke nobody.

    `composition._decision_signal` raised `NotImplementedError`: the audit row landed, the
    caller was told the approval was accepted, and the workflow slept until it expired
    three days later. Nothing raised where anybody was looking.

    THE WAKE GOES THROUGH THE CONTAINER, WHICH IS THE ENTIRE POINT
        `tests/integration/test_turn_polling.py` already proves that
        `turn_workflow.signal_decision` wakes a waiting workflow - by passing that function
        to `DecideApproval` itself. That is the shape docs/STATE.md has recorded seven
        times: the collaborator a test supplies is the one production never binds. This
        test may not name the send at all; it builds the production container and uses the
        `decide_approval` that comes out of it.
    """
    _drop_databases()
    settings = Settings(
        app_conninfo=_conninfo_for(_APP_DATABASE),
        admin_conninfo=_ADMIN_CONNINFO,
        profiles_dir=Path(composition.__file__).resolve().parents[2] / "profiles",
        policy_dir=tmp_path / "no-policy",
        skills_dir=tmp_path / "skills",
        media_dir=tmp_path / "media",
        dbos_app_name="agent-core-preflight-test",
    )

    turn_id = TurnId(str(uuid4()))
    tool_call_id = ToolCallId("call-wake-1")
    session = SessionRef(session_id=SessionId("s-wake"), tenant_id=TenantId("t-1"))

    async def drive() -> tuple[dict[str, object], str]:
        container = await composition.start_container(settings)
        container.domain_pool.open()
        container.audit_pool.open()
        try:
            # The requester, on the only row that records one: D25 refuses an approval it
            # cannot show came from a second person, and an unknown requester is a refusal
            # rather than a pass.
            await container.audit.record_tool_call(
                turn_id,
                CallerIdentity(
                    subject_id="u-1", channel="http", tenant_id=TenantId("t-1")
                ),
                "refund",
                {},
                PolicyDecision(
                    effect=Effect.NEEDS_APPROVAL,
                    reason="a refund above the limit needs a human",
                    rule_id="r-refund",
                ),
            )
            await container.human_gateway.publish(
                turn_id,
                session,
                (
                    PendingRequest(
                        kind=PendingKind.APPROVAL,
                        tool_call_id=tool_call_id,
                        tool_name="refund",
                        arguments={},
                        reason="a refund above the limit needs a human",
                    ),
                ),
            )
            correlation_id = await asyncio.to_thread(_published_handle, settings, turn_id)

            DBOS(config=cast("DBOSConfig", composition.dbos_config(settings)))
            DBOS.launch()
            try:
                with SetWorkflowID(str(turn_id)):
                    handle = await DBOS.start_workflow_async(_await_a_human_answer)
                await container.decide_approval.execute(
                    correlation_id,
                    # A different subject from the one that started the turn: an approval
                    # from the requester is D25's four-eyes refusal, not this test's subject.
                    "u-2",
                    True,
                    "go ahead",
                )
                received = await asyncio.wait_for(
                    handle.get_result(), timeout=_WAKE_TIMEOUT_SECONDS
                )
            finally:
                DBOS.destroy()
            return received, correlation_id
        finally:
            container.audit_pool.close()
            container.domain_pool.close()

    try:
        received, _ = asyncio.run(drive())
    finally:
        _drop_databases()

    assert received["woken"], (
        "the decision never reached the waiting workflow. `composition._decision_signal` "
        "raises NotImplementedError, so D25 records the approval and the turn it approved "
        "waits three days and expires - and the caller is told it worked. "
        "docs/TASKS.md#t-f11-19"
    )
    assert received["approved"] is True and received["note"] == "go ahead", (
        f"the woken turn received {received!r}; the human approved with a note, and both "
        "halves have to survive the durable send - a refusal's reason is what lets the "
        "model adapt instead of retrying."
    )
    assert received["tool_call_id"] == str(tool_call_id), (
        f"the answer woke the turn addressed to tool call {received['tool_call_id']!r} "
        f"rather than {str(tool_call_id)!r}. Resuming with a different id silently drops "
        "the result and the agent loops asking again."
    )


def _published_handle(settings: Settings, turn_id: TurnId) -> str:
    with psycopg.connect(settings.app_conninfo) as conn:
        row = conn.execute(
            "SELECT correlation_id FROM human_requests WHERE turn_id = %s",
            (str(turn_id),),
        ).fetchone()
    assert row is not None, "the gateway published no correlation row to answer on"
    return str(row[0])
