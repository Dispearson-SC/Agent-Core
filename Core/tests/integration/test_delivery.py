"""The return trip: a finished turn leaves the system on the channel it arrived on.

Phase:   F3 - Deferred human interaction / outbound delivery
         F2 - the barrier that keeps a test from starting a turn off-queue
Tasks:   docs/TASKS.md#t-f3-07, docs/TASKS.md#t-f2-03, docs/TASKS.md#t-f0-06
Covers:  adapters/driving/workflow/turn_workflow.py - `_step_deliver` and the body's call
         Core/tests/integration/               - every sibling that drives a turn

WHAT IS BEING DEFENDED
    Before `_step_deliver` exists a turn runs perfectly, is audited, is persisted - and
    the answer reaches nobody. That failure has no symptom inside the process: every
    assertion a unit test can make about `StartTurn` still holds. It is only visible from
    the outside, as a human who asked a question and never got a reply.

    So the two things pinned here are the two ways the answer can go missing:

      1. it is never handed to a channel at all;
      2. it is handed to the WRONG channel, or to none, because the id the turn arrived on
         is not in the registry - and the lookup returns `None` instead of raising.

    (2) is the dangerous one. `UnknownChannelError` exists precisely so a missing wiring
    entry cannot degrade into a turn that completes, records success and delivers nothing;
    `adapters/driving/channels/registry.py` says so in its own docstring. This test is what
    makes that guarantee reach the workflow.

WHY THE TURN IS ENQUEUED AND NO LONGER DRIVEN AS A BARE BODY - READ BEFORE "SIMPLIFYING"
    This file used to reach past `@DBOS.workflow()` with `inspect.unwrap` and call the body
    in-process, on the grounds that delivery is a decision of the BODY and needs no
    infrastructure. docs/TASKS.md#t-f0-06 removed that option, and it removed it for a
    reason worth keeping written down.

    The domain `TurnId` is now the workflow's own durable id: `_step_new_turn_id` reads it
    off the running workflow instead of minting a `uuid4`. A body invoked outside a
    workflow therefore has NO id to file the turn under, and it now says so -
    `NotInsideAWorkflowError`, quoting `enqueue_turn`. Weakening that guard to let these
    two tests back in would restore the exact defect t-f0-06 closed: a third id, minted by
    nobody's authority, addressing no workflow, joined to no audit row.

    There is a second reason, and it is the one this file's first two tests exist for.
    `agent-core-turns` is partitioned with `partition_concurrency=1`. A turn that skips
    `enqueue_turn` skips the partition with it, so a test that drives the body directly
    cannot see an interleaving that only the queue prevents - and it cannot see a
    PENDING row left behind by an earlier run either, which is the failure mode the third
    test below reproduces on purpose.

THE ASSERTIONS OVER THE SIBLING FILES ARE THE BARRIER, NOT BOOKKEEPING
    Two of them, and both were red when they were written: this file drove the body, and
    `test_coalescing.py` launched DBOS onto whatever its last killed run had left in the
    system table. Neither failure is loud. The first mints an id nothing is filed under;
    the second reads as "the turn never started", sixty seconds later, with nothing raised
    anywhere. A rule that lives in a docstring is re-broken by the next agent; this one
    fails a test.
"""

from __future__ import annotations

import ast
import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from agent_core.adapters.driving.channels.registry import (
    ChannelRegistry,
    OutboundMessage,
)
from agent_core.adapters.driving.http.routes import create_app
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.ingest_media import IngestMedia
from agent_core.domain.media import MediaId, MediaKind, MediaPolicy, MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)
from tests.fakes.ports import FakeAuditSink, FakeMediaStore

_ANSWER = "the account is frozen"

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# Its own DBOS system database, for the reason test_coalescing.py and test_turn_polling.py
# both give: a failed run here can be destroyed without touching anything another file
# owns, and the third test below deliberately corrupts it.
_DBOS_DATABASE = "agent_core_delivery_test"

# How long a turn that MUST run is given. Not a tolerance any assertion leans on: the
# proof is the turn running, and a slow machine only reaches it later. It exists so a
# queue that never dequeues fails with a sentence instead of hanging the suite.
_RUN_DEADLINE_SECONDS = 30.0

# How long a turn that must NOT run is watched for. This one IS a bound rather than a
# proof, and it is the only honest shape available: "never" is not observable. It is long
# enough that the 1.0s queue poll has been round ten times over.
_BLOCKED_DEADLINE_SECONDS = 8.0


# ---------------------------------------------------------------------------
# The barrier: no sibling test may start a turn past the queue, and no sibling
# test may launch DBOS onto state an earlier run abandoned.
# ---------------------------------------------------------------------------


def _integration_modules() -> tuple[Path, ...]:
    """Every test module in this directory, this one included.

    Sorted, and never a set or a directory-order glob: CLAUDE.md non-negotiable #7's
    reasoning applies to a guard's failure message as much as to a dispatch - a rule that
    names a different file each time it fails is a rule nobody trusts.
    """
    return tuple(sorted(Path(__file__).parent.glob("test_*.py")))


def _dotted(node: ast.expr) -> str:
    """`DBOS.launch` for an `ast.Attribute`, `setattr` for an `ast.Name`, `""` otherwise."""
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _parsed(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8"))


def _called_leaves(tree: ast.Module) -> set[str]:
    """The LAST segment of every call's dotted name.

    Keyed on the leaf so an alias cannot evade the rule: `turn_workflow.enqueue_turn(...)`
    and `enqueue_turn(...)` are the same fact, and a guard keyed on the import style would
    be defeated by changing the import.
    """
    return {
        leaf
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for leaf in (_dotted(node.func).rsplit(".", 1)[-1],)
        if leaf
    }


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every identifier the module REFERENCES, ignoring identifiers it merely quotes.

    The distinction carries the guard. `test_turn_polling.py` and `test_durability.py` both
    mention "run_turn_workflow" inside assertion messages, which says nothing about how
    they start a turn; `turn_workflow.run_turn_workflow` as an attribute access is the
    module reaching for the workflow object itself, which says everything.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _patched_attributes(tree: ast.Module) -> set[str]:
    """The attribute names handed to a `setattr`-shaped call as a string literal.

    `monkeypatch.setattr(turn_workflow, "_step_new_turn_id", ...)` is how a test declares
    that it has taken a step out of the picture, and it is the only declaration of that
    kind the AST can see.
    """
    patched: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _dotted(node.func).rsplit(".", 1)[-1] != "setattr":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                patched.add(argument.value)
    return patched


_DESTRUCTIVE = "DROP DATABASE"


def _string_fragments(node: ast.expr) -> Iterator[str]:
    """Every string constant under one expression, f-string fragments included.

    An f-string is a `JoinedStr` of `Constant` parts, so
    `f'DROP DATABASE IF EXISTS "{name}"'` is found by walking for constants - which a rule
    about "does this file destroy the state it inherited" has to do, because the database
    name is always interpolated.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def _call_argument_fragments(tree: ast.Module) -> Iterator[str]:
    """The same fragments, but only where they are ARGUMENTS TO A CALL.

    This is the whole difference between a guard and a wish. Walking every `ast.Constant`
    in a module finds the fragment in a docstring, in an assertion message, in a `_NOTE =
    "remember to ..."` - none of which destroy anything - so a module could launch DBOS
    onto a partition an earlier killed run still holds and satisfy the rule by TALKING
    about the cleanup. `test_durability.py` states in its own docstring that the
    destructive statement is "deliberately the ONLY place in this module that spells it";
    that promise is only checkable by a rule that reads an executable call.

    Keyword arguments are included with the positional ones because `execute(sql=...)` is
    the same statement, and `*args`/`**kwargs` arrive inside `node.args`/`node.keywords`
    already. What is deliberately NOT included is `node.func`: a call's own name is not
    something it passes to a database.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in node.args:
            yield from _string_fragments(argument)
        for keyword in node.keywords:
            yield from _string_fragments(keyword.value)


def _launches_dbos(tree: ast.Module) -> bool:
    """Does this module start a DBOS runtime at all? `DBOS.launch()` is the only way."""
    return "launch" in _called_leaves(tree)


def _destroys_inherited_state(tree: ast.Module) -> bool:
    """Does this module actually EXECUTE the destruction of its own DBOS databases?"""
    return any(_DESTRUCTIVE in fragment for fragment in _call_argument_fragments(tree))


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_no_integration_test_starts_a_turn_past_the_queue() -> None:
    """docs/TASKS.md#t-f2-03 + #t-f0-06, enforced over this directory's own sources.

    A turn is started through `enqueue_turn` or it is not a turn: that is where the
    partition key is attached and where the durable id the whole system files rows under
    comes from. A test that reaches past `@DBOS.workflow()` and calls the body instead is
    exercising a turn with neither.

    ONE LEGITIMATE EXCEPTION, AND THE CONDITION THAT MAKES IT LEGITIMATE
        `test_durability.py`'s replay test IS about the body - it drives it twice, once
        executing its steps and once replaying their recorded results, and no durable
        engine can be asked to do that on demand. What makes that honest rather than a
        bypass is that it replaces `_step_new_turn_id`: there is no real step reaching for
        a workflow that is not running, so no id is invented.

        So the rule is not "never touch the body", which would delete a real test. It is
        "if you touch the body, take the id-reading step out of the picture first" - and a
        module that does neither is starting turns off-queue.

    Deliberately NOT a list of allowed filenames. A name is a fact about today's directory
    that drifts the next time a file is added, and a guard that has to be edited for a
    legal change teaches everyone to edit guards.
    """
    offenders: dict[str, list[str]] = {}
    for module in _integration_modules():
        tree = _parsed(module)
        if "run_turn_workflow" not in _referenced_names(tree):
            continue
        if "enqueue_turn" in _called_leaves(tree):
            continue
        if "_step_new_turn_id" in _patched_attributes(tree):
            continue
        offenders[module.name] = sorted(_called_leaves(tree) & {"unwrap", "run_turn_workflow"})

    assert offenders == {}, (
        f"{sorted(offenders)} reach for run_turn_workflow without going through "
        "enqueue_turn and without replacing _step_new_turn_id. Since "
        "docs/TASKS.md#t-f0-06 the domain TurnId IS the workflow's durable id, so a body "
        "invoked outside a workflow has no id to file the turn under - and it also has no "
        "partition, which is what docs/TASKS.md#t-f2-03 exists to give it. Enqueue the "
        "turn, or swap the step that reads the id; do not weaken the guard that says so."
    )


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_every_integration_test_that_launches_dbos_destroys_the_state_it_inherited() -> None:
    """A PENDING row from a killed run must not be able to make a later run HANG.

    `agent-core-turns` is partitioned with `partition_concurrency=1`, and DBOS's dequeue
    treats a PENDING workflow on a partition as that partition's occupant regardless of
    which application version wrote it (`_sys_db.py`: the mutual-exclusion probe is
    "unscoped by design"). Local recovery, on the other hand, IS scoped - it asks for
    pending workflows of the CURRENT `application_version` only (`_dbos.py`) - so a row
    left behind by a run whose code has since changed is never recovered and never
    releases the slot.

    The consequence for a test file is that every later turn for that session is accepted,
    enqueued, and sits at ENQUEUED forever. Nothing raises. It reads as "the turn never
    started" after whatever deadline the test happened to set, which is indistinguishable
    from a defect in the code under test - and it is how `test_coalescing.py` came to fail
    with `DBOSNonExistentWorkflowError` for reasons that were not in this repository.

    The last test in this file reproduces that blocking causally. This assertion is what
    stops the cleanup being deleted as noise by someone who has never seen it happen.
    """
    offenders: list[str] = []
    for module in _integration_modules():
        tree = _parsed(module)
        if not _launches_dbos(tree):
            continue
        if _destroys_inherited_state(tree):
            continue
        offenders.append(module.name)

    assert offenders == [], (
        f"{offenders} launch DBOS without destroying the system database first. One "
        "workflow left PENDING by a killed run occupies its session's only partition slot "
        "forever - its application_version is never recovered - so every later turn for "
        "that session is enqueued and never dequeued, with nothing raising. Drop the "
        "file's own DBOS databases in the launch helper, the way test_turn_polling.py "
        "does; a rerun that 'went green on its own' is the same bug still there."
    )


_MENTIONS_IT_ONLY = '''
"""A module that talks about how it would DROP DATABASE and never does it.

A comment-shaped literal is the same thing:
"""

_NOTE = "remember to DROP DATABASE before launching, one day"


def launch_it() -> None:
    DBOS.launch()
'''

_ACTUALLY_DOES_IT = '''
"""A module that says nothing and destroys its own state."""


def launch_it(admin: object, database: str) -> None:
    admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    DBOS.launch()
'''


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_the_cleanup_guard_rejects_a_module_that_only_talks_about_the_cleanup() -> None:
    """The guard above must read an executable call, not any string in the file.

    A rule that walks every `ast.Constant` is satisfied by a docstring, an assertion
    message, or a `_NOTE = "remember to ..."` - so a module could launch DBOS onto a
    partition still held by a killed run's PENDING row and pass, which is precisely the
    silence the guard exists to break. `test_durability.py` says in its own words that the
    destructive statement is "deliberately the ONLY place in this module that spells it",
    and that promise is only worth anything if the guard can tell the difference.

    So the fragment has to appear inside a CALL's arguments - f-string parts included,
    because the database name is always interpolated. Both fixtures below launch DBOS;
    only one of them destroys anything.

    THIS TEST IS THE TEETH, AND IT IS HERE BECAUSE A GUARD THAT CANNOT FAIL HAS SHIPPED IN
    THIS REPOSITORY THREE TIMES. Asserting the guard's verdict on the real directory would
    pass either way - every module in it happens to call `execute`. Only a module built to
    be rejected proves the rule rejects anything at all.
    """
    mentions = ast.parse(_MENTIONS_IT_ONLY)
    does_it = ast.parse(_ACTUALLY_DOES_IT)

    assert _launches_dbos(mentions) and _launches_dbos(does_it), (
        "both fixtures must reach the destruction rule at all, or the assertions below "
        "say nothing about it."
    )
    assert not _destroys_inherited_state(mentions), (
        "a module that only MENTIONS dropping its databases satisfies the cleanup guard. "
        "It may then launch DBOS onto a partition an earlier killed run still holds, and "
        "every later turn for that session is enqueued and never dequeued with nothing "
        "raising - the guard would be certifying the exact failure it was written for."
    )
    assert _destroys_inherited_state(does_it), (
        "a module that really executes the drop is now refused, so the fix went too far: "
        "the interpolated database name lives in an f-string, and every launch helper in "
        "this directory writes it that way."
    )


# ---------------------------------------------------------------------------
# Delivery, driven through the queue that production uses
# ---------------------------------------------------------------------------


class _RecordingChannel:
    """A `Channel` by shape alone - the Protocol is structural on purpose (t-f3-13).

    It records rather than sends, and it records the CALLER as well as the message: the
    channel adapter needs the identity to know which chat to answer, and a delivery step
    that dropped it would still look correct from the message alone.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _FinishesImmediately:
    """Stands in for `StartTurn`: one finished turn, no suspension, no collaborators.

    Delivery is what is under test, and a real `StartTurn` would need a model, a store and
    a policy - none of which change where the answer goes.
    """

    def __init__(self, result: TurnResult) -> None:
        self._result = result
        self.calls = 0

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.calls += 1
        return TurnOutcome(turn_id=turn_id, result=self._result)


def _request(channel: str, session: str = "s-f3") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId("t-f3")),
        caller=CallerIdentity(
            subject_id="u-1",
            channel=channel,
            tenant_id=TenantId("t-f3"),
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
    """Destroy this file's DBOS state. See the barrier assertion above for why."""
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DBOS_DATABASE}_dbos_sys", _DBOS_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _Dbos:
    """A launched DBOS for the length of one block, destroyed however the block ends.

    `drop=False` is used by exactly one test, which needs the state the previous block
    left behind; every other entry starts from nothing.
    """

    def __init__(self, name: str, *, drop: bool = True) -> None:
        self._name = name
        self._drop = drop

    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        if self._drop:
            _drop_databases()
        config: DBOSConfig = {"name": self._name, "database_url": _dbos_conninfo()}
        DBOS(config=config)
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_turn: _FinishesImmediately,
    channels: ChannelRegistry,
) -> None:
    """Wire the workflow's collaborators for one test, and unwire them afterwards.

    `monkeypatch` rather than `bind_dependencies` so the module global does not leak into
    the next test in the session - the binding is process-wide by design (a DBOS workflow
    is a module-level object) and that is exactly what makes it leak.
    """
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", start_turn), channels=channels
        ),
    )


async def _run_one_turn(request: TurnRequest) -> tuple[TurnOutcome, str]:
    """Enqueue one turn the way production does, and hand back its outcome and its id.

    The id is returned alongside the outcome because the two being EQUAL is the property
    docs/TASKS.md#t-f0-06 landed, and the queue is where it is observable: the handle is
    what `POST /turns` answers a caller with, and the outcome is what every audit row is
    filed under.
    """
    async with _Dbos("agent-core-delivery-test"):
        handle = await turn_workflow.enqueue_turn(request)
        outcome = await asyncio.wait_for(handle.get_result(), timeout=_RUN_DEADLINE_SECONDS)
        return outcome, handle.workflow_id


@pytest.mark.phase("F3")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_finished_turn_is_delivered_on_the_channel_it_arrived_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f3-07, the whole point of the anchor.

    The turn arrived on `telegram`, so the answer leaves on `telegram` - not on the only
    channel registered, not on the first one, and not on every one of them.
    """
    telegram = _RecordingChannel()
    whatsapp = _RecordingChannel()
    registry = ChannelRegistry((("telegram", telegram), ("whatsapp", whatsapp)))

    media = (
        MediaRef(
            media_id=MediaId("m-1"),
            kind=MediaKind.IMAGE,
            mime_type="image/png",
            size_bytes=11,
            sha256="0" * 64,
        ),
    )
    start_turn = _FinishesImmediately(TurnResult(text=_ANSWER, media=media))
    _bind(monkeypatch, start_turn=start_turn, channels=registry)

    request = _request("telegram")
    outcome, workflow_id = asyncio.run(_run_one_turn(request))

    assert outcome.result is not None
    assert telegram.sent, (
        "the turn finished and nothing was handed to a channel. Every assertion about "
        "StartTurn still passes and the human who asked gets no reply; that is the failure "
        "docs/TASKS.md#t-f3-07 exists to close."
    )
    delivered_to, message = telegram.sent[0]
    assert delivered_to == request.caller, (
        "the message was delivered without the identity it must be addressed to. A channel "
        "adapter needs the caller to know which chat to answer."
    )
    assert message == OutboundMessage(text=_ANSWER, media=media), (
        f"the channel received {message!r}. A finished TurnResult travels as the "
        "OutboundMessage the registry defines - text plus media references, and none of "
        "the usage or timing that belongs to the audit trail."
    )
    assert whatsapp.sent == [], (
        f"the answer also went to whatsapp ({whatsapp.sent!r}). Delivery is routed by "
        "CallerIdentity.channel, never broadcast to everything registered."
    )
    assert len(telegram.sent) == 1, (
        f"the answer was sent {len(telegram.sent)} times in a single pass of the body."
    )
    assert str(outcome.turn_id) == workflow_id, (
        f"the turn was delivered under {outcome.turn_id} while the caller polls "
        f"{workflow_id}. docs/TASKS.md#t-f0-06 made those one value; two of them means "
        "every lookup across the pair - GET /turns, the audit rows, the correlation table, "
        "DecideApproval's wake-up - returns empty forever without raising."
    )


@pytest.mark.phase("F3")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_turn_on_an_unregistered_channel_fails_loudly_instead_of_dropping_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing registry entry is a wiring mistake, and it must behave like one.

    The alternative - skip the send and return the outcome anyway - is a turn that
    completes, is audited as successful, and reaches nobody. Nothing raises, nothing logs
    an error, and the only report is a user saying the bot ignored them. That is the exact
    failure `UnknownChannelError` was written for in
    `adapters/driving/channels/registry.py`; this pins that the workflow lets it through
    rather than swallowing it.

    WHAT IS ASSERTED IS THE TURN'S FATE, NOT THE EXCEPTION'S TYPE, AND THAT IS A FINDING
        In-process, `_step_deliver` raises `UnknownChannelError` naming the channel. Across
        the durable boundary the type does NOT survive: DBOS pickles a workflow's
        exception, `UnknownChannelError.__init__` takes two arguments while
        `BaseException.__reduce__` hands back only `args`, so `get_result()` raises
        `TypeError: UnknownChannelError.__init__() missing 1 required positional argument:
        'known'` and the channel id is gone from the message. That is a defect in the
        registry's exception, not in this workflow, and it belongs to an anchor nobody has
        written - so this test asserts the property it is named for (the turn ENDS IN
        ERROR and nothing was delivered anywhere) and does not pretend the identity
        survived.
    """
    telegram = _RecordingChannel()
    registry = ChannelRegistry((("telegram", telegram),))

    start_turn = _FinishesImmediately(TurnResult(text=_ANSWER))
    _bind(monkeypatch, start_turn=start_turn, channels=registry)

    async def drive() -> str:
        async with _Dbos("agent-core-delivery-test"):
            handle = await turn_workflow.enqueue_turn(_request("carrier-pigeon"))
            with pytest.raises(Exception):  # noqa: B017 - see the docstring
                await asyncio.wait_for(handle.get_result(), timeout=_RUN_DEADLINE_SECONDS)
            return (await handle.get_status()).status

    status = asyncio.run(drive())

    assert status == "ERROR", (
        f"the turn ended {status!r}. An answer with nowhere to go must fail the turn: a "
        "workflow recorded SUCCESS while nobody was told anything is the audited-and-"
        "delivered-to-nobody outcome UnknownChannelError exists to prevent."
    )
    assert telegram.sent == [], (
        f"the answer was delivered to telegram ({telegram.sent!r}) because that was the "
        "only channel registered. Falling back to whatever is wired sends one user's "
        "conversation to another platform."
    )
    assert start_turn.calls == 1, (
        "the turn itself did not run, so the test proved nothing about delivery."
    )


def _pending_rows() -> list[tuple[str, str, str | None]]:
    with psycopg.connect(
        _dbos_conninfo(f"{_DBOS_DATABASE}_dbos_sys"), connect_timeout=5, autocommit=True
    ) as system:
        return [
            (str(row[0]), str(row[1]), None if row[2] is None else str(row[2]))
            for row in system.execute(
                "SELECT workflow_uuid, status, queue_partition_key "
                "FROM dbos.workflow_status"
            ).fetchall()
        ]


def _abandon(workflow_id: str) -> None:
    """Turn one finished row into exactly what a killed worker leaves behind.

    PENDING plus an `application_version` this process will never claim: that pair is the
    whole failure mode. DBOS writes it by dying between dequeue and completion; two
    columns write it here deterministically, which is the difference between a guard and a
    story about a flake.
    """
    with psycopg.connect(
        _dbos_conninfo(f"{_DBOS_DATABASE}_dbos_sys"), connect_timeout=5, autocommit=True
    ) as system:
        system.execute(
            "UPDATE dbos.workflow_status "
            "SET status = 'PENDING', application_version = 'a-version-that-is-gone' "
            "WHERE workflow_uuid = %s",
            (workflow_id,),
        )


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_pending_row_from_a_dead_run_silences_its_session_until_the_state_is_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism the barrier above exists for, reproduced rather than described.

    Three facts, in one run, because separately each one has an innocent explanation:

      1. a turn whose session carries a PENDING row from a version this process will not
         claim is accepted, enqueued, and never dequeued - no exception, no log, nothing
         to poll but silence;
      2. a turn on a DIFFERENT session runs normally in the same process, which is what
         makes (1) a partition slot rather than a broken DBOS or a broken fake;
      3. destroying the system database releases it, which is what every launch helper in
         this directory now does and why the barrier assertion refuses one that does not.

    THIS IS ALSO A PRODUCTION FAILURE MODE AND THE TEST SAYS SO ON PURPOSE.
        Nothing here is test-only. A worker killed mid-turn in production leaves the same
        row, and `_dbos.py` recovers pending workflows for the CURRENT
        `application_version` only - which by default is a hash of the registered source,
        so the next deploy is by definition a different version. That session then answers
        nobody, forever, and the symptom is silence. The configurations that would recover
        it are named in docs/TASKS.md; neither is wired here.
    """
    start_turn = _FinishesImmediately(TurnResult(text=_ANSWER))
    _bind(
        monkeypatch,
        start_turn=start_turn,
        channels=ChannelRegistry((("telegram", _RecordingChannel()),)),
    )

    async def drive() -> tuple[bool, bool, bool]:
        async with _Dbos("agent-core-delivery-test"):
            first = await turn_workflow.enqueue_turn(_request("telegram", "s-blocked"))
            await asyncio.wait_for(first.get_result(), timeout=_RUN_DEADLINE_SECONDS)
            _abandon(first.workflow_id)

        # Deliberately NOT dropping: this block inherits the abandoned row, exactly as the
        # next process to start would.
        async with _Dbos("agent-core-delivery-test", drop=False):
            blocked = await turn_workflow.enqueue_turn(_request("telegram", "s-blocked"))
            try:
                await asyncio.wait_for(
                    blocked.get_result(), timeout=_BLOCKED_DEADLINE_SECONDS
                )
                same_session_ran = True
            except TimeoutError:
                same_session_ran = False

            other = await turn_workflow.enqueue_turn(_request("telegram", "s-free"))
            try:
                await asyncio.wait_for(other.get_result(), timeout=_RUN_DEADLINE_SECONDS)
                other_session_ran = True
            except TimeoutError:
                other_session_ran = False

        async with _Dbos("agent-core-delivery-test"):
            after = await turn_workflow.enqueue_turn(_request("telegram", "s-blocked"))
            try:
                await asyncio.wait_for(after.get_result(), timeout=_RUN_DEADLINE_SECONDS)
                recovered = True
            except TimeoutError:
                recovered = False

        return same_session_ran, other_session_ran, recovered

    same_session_ran, other_session_ran, recovered = asyncio.run(drive())

    assert not same_session_ran, (
        "a turn ran on a session whose partition still holds a PENDING row from a version "
        "this process cannot claim. If dbos has started recovering foreign versions, or "
        "released the slot some other way, this guard has outlived its cause - delete it "
        "WITH the cleanup it justifies and record the version that changed, never one "
        "without the other."
    )
    assert other_session_ran, (
        "no turn ran at all, on any session, so the assertion above proved nothing about "
        "partitions - it proved DBOS or the fake was broken for this whole block."
    )
    assert recovered, (
        "the session was still silent after its DBOS databases were destroyed. That is "
        "the cleanup every launch helper in this directory performs, and if it does not "
        "release the slot then the barrier assertion above is guarding the wrong thing."
    )


# ---------------------------------------------------------------------------
# F7: the evidence wire, and the route that puts something on it
# docs/TASKS.md#t-f7-11
# ---------------------------------------------------------------------------


_EVIDENCE_PROFILE = AgentProfile(
    id="evidence-profile",
    persona="asks for photos",
    model="test-model",
    media=MediaPolicy(accepted_kinds=frozenset({MediaKind.IMAGE}), max_bytes=1_000),
)

# Shaped like a PROVIDER's id and not like a tidy fixture: mixed case, an underscore, a
# dash and a dot. CLAUDE.md non-negotiable #5 - a mangled tool_call_id is dropped by
# Pydantic AI WITHOUT an exception, so nothing here may compare normalised forms.
_EVIDENCE_TOOL_CALL_ID = "call_9aF-Zx.Q7_tB3"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _RecordingGateway:
    """A `HumanGateway` that records the ask instead of sending it anywhere."""

    def __init__(self) -> None:
        self.published: list[PendingRequest] = []

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        self.published.extend(requests)

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        raise AssertionError("the workflow never correlates; the route does")


class _RecordingResume:
    """`ResumeTurn` by shape: records what the workflow handed it, then finishes the turn.

    It records the RESOLUTIONS rather than a summary of them, because what has to survive
    the durable crossing is a value and not a shape - see the test below.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[TurnId, tuple[Any, ...]]] = []

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[Any, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        self.calls.append((turn_id, resolutions))
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=_ANSWER))


class _SuspendsOnEvidence:
    """`StartTurn`, in-process: one suspension asking a human for a photo."""

    def __init__(self) -> None:
        self.runs = 0

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.runs += 1
        return TurnOutcome(
            turn_id=turn_id,
            pending=(
                PendingRequest(
                    kind=PendingKind.EVIDENCE,
                    tool_call_id=ToolCallId(_EVIDENCE_TOOL_CALL_ID),
                    tool_name="request_evidence",
                    arguments={"kind": "photo"},
                    reason="a photo of the parcel as it arrived",
                ),
            ),
        )


async def _wait_until_asked(asked: _RecordingGateway) -> None:
    """Block until the turn has published its question and entered the durable wait.

    Not a tolerance any assertion leans on: the proof is the answer being applied, and a
    slow machine only reaches it later. The deadline exists so a turn that never asks
    fails with a sentence instead of hanging the suite.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RUN_DEADLINE_SECONDS
    while not asked.published:
        if loop.time() > deadline:
            raise AssertionError(
                "the turn never asked anybody for evidence, so nothing was waiting for "
                "the upload and the test would prove nothing about the wire."
            )
        await asyncio.sleep(0.05)


@pytest.mark.phase("F7")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_an_uploaded_image_survives_the_durable_wire_into_the_waiting_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f7-11. The evidence carrier admits a `MediaRef`, and it round-trips.

    `t-f7-09` proved the transport carries an image only through a deliberately visible
    `cast`: `HumanAnswer.note` was `str | None`, so no type-checked caller could put a
    stored upload on the durable wire. A test that needs a cast to exercise a supported
    path is reporting that the type is wrong. This test has no cast on that call, which is
    half of what it asserts - the other half only `mypy` can see.

    ASSERT THE ROUND TRIP, NOT THE SHAPE, AND docs/TASKS.md#t-f3-18 IS WHY
        Everything sent here crosses a pickle boundary: `DBOS.send` writes the payload
        into the system database and the workflow reads it back, in another process after
        a redeploy as far as the type is concerned. `UnknownChannelError` did not survive
        that crossing - `BaseException.__reduce__` yields only `args`, so the id was gone
        on the far side while every in-process test still passed. A frozen `slots=True`
        dataclass has its own version of that trap, so what is asserted is the VALUE that
        came out the other end, field by field, and never `isinstance`.

    IDEMPOTENCE IS THE SEND'S, AND IT IS NOT REBUILT HERE. The same upload is signalled
    twice, keyed on (turn_id, tool_call_id) exactly as `signal_decision` is: humans
    double-click and retry logic re-posts, and one deferred call must be resolved once.
    """
    started = _SuspendsOnEvidence()
    asked = _RecordingGateway()
    resumed = _RecordingResume()
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", started),
            channels=ChannelRegistry((("telegram", _RecordingChannel()),)),
            human_gateway=asked,
            resume_turn=cast("Any", resumed),
        ),
    )

    uploaded = MediaRef(
        media_id=MediaId("sha256:1f0c"),
        kind=MediaKind.IMAGE,
        mime_type="image/jpeg",
        size_bytes=48_122,
        sha256="1f0c",
        filename="parcel.jpg",
    )

    async def drive() -> TurnOutcome:
        async with _Dbos("agent-core-delivery-test"):
            handle = await turn_workflow.enqueue_turn(_request("telegram", "s-evidence"))
            await _wait_until_asked(asked)
            turn_id = TurnId(handle.workflow_id)
            # No cast. This line is the anchor: a production caller holding a stored
            # MediaRef must be able to write it.
            await turn_workflow.signal_evidence(
                turn_id, ToolCallId(_EVIDENCE_TOOL_CALL_ID), uploaded
            )
            await turn_workflow.signal_evidence(
                turn_id, ToolCallId(_EVIDENCE_TOOL_CALL_ID), uploaded
            )
            return await asyncio.wait_for(
                handle.get_result(), timeout=_RUN_DEADLINE_SECONDS
            )

    outcome = asyncio.run(drive())

    assert [request.kind for request in asked.published] == [PendingKind.EVIDENCE], (
        f"the human was asked {[r.kind for r in asked.published]}. An evidence request is "
        "human-answerable and must reach a person, or nobody ever uploads anything."
    )
    assert len(resumed.calls) == 1, (
        f"the turn resumed {len(resumed.calls)} times for one upload. The send is keyed on "
        "(turn_id, tool_call_id) so the second delivery of the same evidence is discarded "
        "by the database the wait lives in; a second resume runs the deferred call twice."
    )

    _, resolutions = resumed.calls[0]
    (resolution,) = resolutions
    assert str(resolution.tool_call_id) == _EVIDENCE_TOOL_CALL_ID, (
        "the deferred call the image answers must be the call the model issued, character "
        "for character - a normalised id is dropped by Pydantic AI without an exception."
    )
    payload = resolution.payload
    assert payload == uploaded, (
        f"the turn resumed holding {payload!r} instead of the uploaded image. This is the "
        "docs/TASKS.md#t-f3-18 crossing: the value has been pickled into the system "
        "database and read back, and anything the carrier loses on the way is lost "
        "silently - `ResumeTurn` then hands the provider a description of a photo."
    )
    assert (payload.media_id, payload.kind, payload.mime_type, payload.filename) == (
        uploaded.media_id,
        uploaded.kind,
        uploaded.mime_type,
        uploaded.filename,
    ), (
        "the reference came back equal but its fields did not, which is the shape of the "
        "t-f3-18 defect: a carrier that survives the crossing having dropped what it "
        "carried."
    )
    assert outcome.result is not None


class _RecordingEvidenceSignal:
    """The seat `POST /evidence/{corr_id}` wakes the turn through."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, TurnId, MediaRef]] = []

    async def __call__(
        self, correlation_id: str, turn_id: TurnId, media: MediaRef
    ) -> None:
        self.calls.append((correlation_id, turn_id, media))


@pytest.mark.phase("F7")
def test_the_evidence_route_wakes_the_turn_it_accepted_the_upload_for() -> None:
    """docs/TASKS.md#t-f7-11, the other half: a stored file that wakes nobody is no answer.

    `POST /evidence/{corr_id}` validated and stored and stopped, and said so in its own
    docstring. From the user's side that is a system which asked for a photo, was given
    one, and went quiet - the turn sits in its durable wait until it expires three days
    later, and nothing raises anywhere.

    THE ROUTE STILL OWNS NEITHER VALIDATION NOR THE SEND. `IngestMedia` owns size, then
    sniff, then accept, then store; the seat asserted here owns turning a resolved handle
    into `turn_workflow.signal_evidence`. What is pinned is that the route calls it, with
    the turn the handle resolved to and the ref `IngestMedia` actually returned - a route
    that signalled some other id would wake the wrong turn, or none.
    """
    store = FakeMediaStore()
    signalled = _RecordingEvidenceSignal()

    async def _resolve(correlation_id: str) -> tuple[TurnId, AgentProfile] | None:
        if correlation_id != "handle-1":
            return None
        return TurnId("turn-1"), _EVIDENCE_PROFILE

    async def _unused_starter(request: object) -> object:  # pragma: no cover - unused
        raise AssertionError("the evidence route must not start a turn")

    client = TestClient(
        create_app(
            start_turn=cast("Any", _unused_starter),
            ingest_media=IngestMedia(media=store, audit=FakeAuditSink()),
            resolve_evidence=_resolve,
            signal_evidence=signalled,
        )
    )

    response = client.post(
        "/evidence/handle-1", files={"file": ("parcel.png", _PNG, "image/png")}
    )

    assert response.status_code == 202, response.text
    assert signalled.calls, (
        "the upload was validated and stored and the waiting turn was never told. The "
        "turn stays in its durable wait until it expires three days later, and the person "
        "who sent the photo is told it was accepted."
    )
    correlation_id, turn_id, media = signalled.calls[0]
    assert (correlation_id, turn_id) == ("handle-1", TurnId("turn-1")), (
        f"the route signalled {(correlation_id, turn_id)!r}. The turn is the one the "
        "handle resolved to; any other id wakes somebody else's turn or nobody's."
    )
    assert str(media.media_id) == response.json()["media_id"], (
        "the reference handed to the signal is not the one IngestMedia stored and the "
        "route answered with, so the turn would resume on a file that is not the upload."
    )
    assert len(signalled.calls) == 1, (
        f"one upload woke the turn {len(signalled.calls)} times."
    )
