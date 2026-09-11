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
import subprocess
import sys
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

# The repository root: `Core/tests/integration/test_preflight.py` is three parents below
# `Core/`, and `Core/src` is where `python -m agent_core` resolves the package from.
REPO_ROOT = Path(__file__).resolve().parents[3]

# How long the preflight PROCESS gets. Everything it is asked about in that test refuses
# immediately - a closed port, an unregistered toolset, a provider litellm does not know -
# so this bounds a hang rather than a wait.
_PREFLIGHT_PROCESS_TIMEOUT_SECONDS = 120.0


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


# A dedicated database, never shared with `_APP_DATABASE` above: this test needs its own
# databases both reset and reachable at once, which the four-way-broken tests never do.
_DBOS_CHECK_DATABASE = "agent_core_preflight_dbos_check"


def _reset_dbos_check_database() -> None:
    """The app database exists and is reachable; its DBOS siblings, old and new, do not.

    So `_database_checks` gets past its early returns to the check under test, and that
    check finds the sibling genuinely absent rather than left over from an earlier run.
    """
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        for database in (
            f"{_DBOS_CHECK_DATABASE}_dbos_sys",
            f"{_DBOS_CHECK_DATABASE}_dbos",
            _DBOS_CHECK_DATABASE,
        ):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{_DBOS_CHECK_DATABASE}"')


@pytest.mark.phase("F11")
@_needs_postgres
def test_the_preflight_checks_the_database_dbos_actually_opens(tmp_path: Path) -> None:
    """docs/TASKS.md#t-f11-36.

    `t-f11-20` moved the bootstrap to create `<app>_dbos_sys`, the name dbos 2.31.1
    derives from the app URL and opens - and left this module checking `<app>_dbos`, the
    retired convention nothing ever opens. A correctly bootstrapped instance was warned
    about a database that does not matter and asked nothing about the one that does.
    """
    from agent_core.adapters.driven.persistence_pg import migrations

    module = _preflight_module()
    assert module is not None, "there is no preflight module; see the test above."

    _reset_dbos_check_database()
    try:
        profiles = tmp_path / "profiles"
        _broken_profile(profiles)
        settings = Settings(
            app_conninfo=_conninfo_for(_DBOS_CHECK_DATABASE),
            profiles_dir=profiles,
            policy_dir=tmp_path / "no-policy-here",
            skills_dir=tmp_path / "skills",
            media_dir=tmp_path / "media",
        )
        report = asyncio.run(module.preflight(settings, environ={}))
    finally:
        with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
            admin.execute(
                f'DROP DATABASE IF EXISTS "{_DBOS_CHECK_DATABASE}" WITH (FORCE)'
            )

    names = [check.name for check in report.checks]
    system_database = migrations.dbos_system_database(_DBOS_CHECK_DATABASE)
    assert f"database {system_database}" in names, (
        f"the preflight never looks at {system_database}, the database dbos derives from "
        f"the app URL and opens - it names the wrong sibling instead:\n{report.render()}"
    )
    assert f"database {_DBOS_CHECK_DATABASE}_dbos" not in names, (
        "the preflight still checks the retired '_dbos' convention - a database nothing "
        f"creates and nothing opens:\n{report.render()}"
    )


def _mcp_down_profile(directory: Path) -> None:
    """A profile whose local half is trivially fine and whose only MCP server is not.

    `toolsets: []` on purpose - the question under test is what happens to the SERVER,
    and a profile that also failed to compose locally would confound the two.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mcp_down.yaml").write_text(
        "\n".join(
            (
                "id: mcp_down",
                "persona: |",
                "  Only used to prove an unreachable MCP server is reported, not swallowed.",
                "model: nowhere/does-not-exist",
                "toolsets: []",
                "mcp_servers:",
                "  - name: down",
                "    transport: http",
                # Port 1 is reserved and nothing on this machine listens on it, so the
                # connection is refused immediately rather than timing out.
                "    url: http://127.0.0.1:1/mcp",
            )
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.phase("F11")
@_needs_postgres
def test_an_unreachable_mcp_server_is_a_warning_not_a_silent_pass(
    tmp_path: Path,
) -> None:
    """docs/TASKS.md#t-f11-36. A degraded start must show up in the report, or nobody can.

    `provider.toolset_for` never connects - `adapters/driven/mcp/toolsets.py` composes a
    server's toolset without reaching it - so a profile whose only MCP server is down
    still, correctly, reports its local half servable. What must not happen is the report
    stopping there: `_GuardedToolset` and `_discover_raw_names` both swallow the failure
    at WARNING and keep going, which is right for a running turn and wrong for a report
    nobody is tailing the logs of at the moment it runs.
    """
    module = _preflight_module()
    assert module is not None, "there is no preflight module; see the test above."

    profiles = tmp_path / "profiles"
    _mcp_down_profile(profiles)
    settings = Settings(
        app_conninfo=_conninfo_for(_ABSENT_DATABASE),
        profiles_dir=profiles,
        policy_dir=tmp_path / "no-policy-here",
        skills_dir=tmp_path / "skills",
        media_dir=tmp_path / "media",
    )

    report = asyncio.run(module.preflight(settings, environ={}))

    servable = [check for check in report.checks if check.name == "profile mcp_down"]
    assert servable and servable[0].severity == "ok", (
        f"the local half of this profile has no server on it and should compose cleanly: "
        f"{servable}"
    )

    mcp_warnings = [
        check
        for check in report.warnings
        if check.category == "profile" and "down" in check.name
    ]
    assert mcp_warnings, (
        "the profile declares an MCP server nothing is listening on, and the report says "
        f"nothing about it - a degraded start is invisible to the operator:\n"
        f"{report.render()}"
    )
    assert mcp_warnings[0].remedy, (
        f"an unreachable MCP server is reported with no remedy: {mcp_warnings[0]}"
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
    reader = console._audit  # noqa: SLF001
    admin = console._admin  # noqa: SLF001
    asyncio.run(reader.tool_calls_for_turn(admin, TurnId(str(uuid4()))))

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


# --------------------------------------------------------------------------------------
# t-f11-38 - the exit code IS the verdict


def _isolated_deployment(tmp_path: Path) -> dict[str, str]:
    """A deployment broken only in ways that answer instantly, from a clean environment.

    No `.env`, no reachable server, no MCP server to spawn: the subprocess below is asked
    a question about its EXIT CODE, so everything that could make it slow or make it
    depend on this machine is removed rather than waited for.
    """
    _broken_profile(tmp_path / "profiles")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENT_CORE_") and not name.endswith("_API_KEY")
    }
    environment.update(
        {
            # Port 1 on loopback: refused, not filtered, so neither this connection nor
            # the maintenance one that tells "server down" from "database missing" waits
            # for a timeout.
            "AGENT_CORE_DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:1/nope",
            "AGENT_CORE_PROFILES_DIR": str(tmp_path / "profiles"),
            "AGENT_CORE_POLICY_DIR": str(tmp_path / "policy"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environment


@pytest.mark.phase("F11")
def test_the_preflight_process_exits_non_zero_when_a_check_fails(tmp_path: Path) -> None:
    """The one machine-readable part of the report has to carry the report's verdict.

    docs/TASKS.md#t-f11-38. The first thing anyone does with a readiness check is put it
    in front of a deploy, and a deploy script reads the exit code - not the eight lines
    above it. Nothing in this suite ran the PROCESS, so nothing could have caught an exit
    code that disagreed with the list printed above it.

    Run from `Core/src`, deliberately: that is where `python -m agent_core` resolves from,
    and it is not the repository root - see docs/TASKS.md#t-f11-39 for what a
    working-directory-relative path in a shipped profile does from here.
    """
    completed = subprocess.run(  # noqa: S603 - this module's own interpreter, fixed argv
        [sys.executable, "-m", "agent_core", "preflight"],
        cwd=REPO_ROOT / "Core" / "src",
        env=_isolated_deployment(tmp_path),
        capture_output=True,
        text=True,
        timeout=_PREFLIGHT_PROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    printed = completed.stdout + completed.stderr

    assert "[FAIL]" in printed, (
        "this deployment has no database and no credential and the report named neither, "
        f"so the exit code below is not the question this test meant to ask:\n{printed}"
    )
    assert completed.returncode != 0, (
        "the preflight printed a FAIL and exited 0, so a deploy script that gates on it "
        f"gets a green light on a deployment that cannot serve a turn:\n{printed}"
    )


@pytest.mark.phase("F11")
def test_a_warning_alone_is_not_a_refusal() -> None:
    """The other half of the contract, and it is a judgement rather than an omission.

    A `warn` is a deployment CHOICE the report has an opinion about: policy managed
    outside this repository, the durable engine's database left for its own best-effort
    creation, an MCP server that did not answer. The last one is the argument: an
    unreachable MCP server is documented as "a degraded start, not a refusal"
    (adapters/driven/mcp/toolsets.py) and a turn under that profile proceeds on its local
    tools. Exiting non-zero for it would refuse a deployment the process itself is willing
    to run - and an exit code that fires on something the operator chose is an exit code
    every deploy script learns to ignore, which is how the FAIL stops being read too.
    """
    module = _preflight_module()
    assert module is not None
    warned = module.PreflightReport(
        checks=(
            module.Check(category="database", name="db", severity="ok", detail="reachable"),
            module.Check(
                category="profile",
                name="mcp server routing",
                severity="warn",
                detail="did not answer at startup",
                remedy="confirm the process behind it is up",
            ),
        )
    )
    assert warned.ready, (
        "a warning refuses the deployment, so `serve` will not start on a profile whose "
        "MCP server is merely degraded - which this system explicitly supports."
    )
    assert not module.PreflightReport(
        checks=(
            module.Check(
                category="database",
                name="db",
                severity="fail",
                detail="missing",
                remedy="create it",
            ),
        )
    ).ready
