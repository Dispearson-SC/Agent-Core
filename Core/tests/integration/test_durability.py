"""F2/F3 acceptance tests. These need a real Postgres and a real DBOS.

READ THIS FIRST
    Every bug these guard against is SILENT. A green unit suite says nothing about any of
    them, because they only appear when a process dies at the wrong moment.

    That is why they are integration tests with deliberate crash injection, not mocks.

THE TWO TESTS t-f2-01 OWNS NEED NO INFRASTRUCTURE, AND THAT IS NOT A COMPROMISE
    Crash recovery is what SURFACES a replay bug; it is not what CAUSES one. The cause is
    always in the workflow BODY - a clock, a uuid or an unordered dispatch that runs again
    on replay and takes a different branch. Both are properties of the body itself, so both
    are checkable without killing a process:

      - where the non-determinism lives is a property of the SOURCE, so it is asserted
        over the AST. A test that needed Postgres to answer "is uuid4() called in the
        workflow body?" would be a test nobody runs on a laptop;
      - "replay reaches the same decision sequence" is a property of the body given a step
        log, so the body is driven twice - once executing its steps, once with the recorded
        results replayed back - and the two decision sequences are compared.

    The crash-injection test that proves the same thing end to end through real DBOS is
    docs/TASKS.md#t-f2-02 and is still skipped below. These do not replace it. They fail on
    a laptop in milliseconds, which is what makes them worth having next to it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
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

_WorkflowBody = Callable[[TurnRequest], Coroutine[Any, Any, TurnOutcome]]

_WORKFLOW_SOURCE = Path(str(turn_workflow.__file__))

# Every producer of a value that differs between two runs of the same code. The rule this
# encodes is CLAUDE.md non-negotiable #2: each of these may appear inside a @DBOS.step()
# and nowhere else. Matching on the LEAF of the dotted call catches `uuid.uuid4()`,
# `uuid4()` and `_uuid.uuid4()` alike, which is what a rule about behaviour needs - a rule
# keyed on the import style would be evaded by changing the import.
_NONDETERMINISTIC_LEAVES = frozenset(
    {
        "uuid1",
        "uuid3",
        "uuid4",
        "uuid5",
        "now",
        "utcnow",
        "today",
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "random",
        "randint",
        "randrange",
        "randbelow",
        "getrandbits",
        "choice",
        "shuffle",
        "urandom",
        "token_hex",
        "token_bytes",
        "token_urlsafe",
    }
)

_IDENTITY_LEAVES = frozenset({"uuid1", "uuid3", "uuid4", "uuid5", "token_hex", "token_urlsafe"})
_CLOCK_LEAVES = frozenset(
    {"now", "utcnow", "today", "time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"}
)


def _dotted(node: ast.expr) -> str:
    """`DBOS.step` for an `ast.Attribute`, `uuid4` for an `ast.Name`, `""` for anything else."""
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _functions_decorated_with(
    tree: ast.Module, decorator: str
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            _dotted(item) == decorator for item in node.decorator_list
        ):
            found.append(node)
    return found


def _nondeterministic_calls(node: ast.AST) -> set[str]:
    calls: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            dotted = _dotted(child.func)
            if dotted and dotted.rsplit(".", 1)[-1] in _NONDETERMINISTIC_LEAVES:
                calls.add(dotted)
    return calls


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_the_turn_id_the_clock_and_randomness_are_produced_inside_a_step() -> None:
    """CLAUDE.md non-negotiable #2, asserted over the source rather than over a crash.

    A `uuid4()` in the workflow body mints a NEW turn id on replay and forks the
    conversation; a `datetime.now()` there dates the same turn twice. Neither fails a test,
    neither raises, and both are invisible until a process dies mid-turn in production.

    So the assertion is positional, not behavioural: every producer of a value that differs
    between two runs must sit lexically inside a `@DBOS.step()` function, and none may
    appear in the `@DBOS.workflow()` body.
    """
    tree = ast.parse(_WORKFLOW_SOURCE.read_text(encoding="utf-8"))

    workflows = _functions_decorated_with(tree, "DBOS.workflow")
    steps = _functions_decorated_with(tree, "DBOS.step")

    assert len(workflows) == 1, (
        "expected exactly one @DBOS.workflow() function in "
        f"{_WORKFLOW_SOURCE.name}, found {[node.name for node in workflows]}. "
        "docs/TASKS.md#t-f2-01"
    )
    assert steps, (
        f"{_WORKFLOW_SOURCE.name} declares no @DBOS.step(). The turn id, the clock and any "
        "randomness have nowhere deterministic to live. CLAUDE.md non-negotiable #2"
    )

    in_the_body = sorted(_nondeterministic_calls(workflows[0]))
    assert in_the_body == [], (
        f"{workflows[0].name} is a workflow BODY and calls {in_the_body}. Every one of "
        "those returns a different value on replay, so the recovered turn takes a "
        "different branch from the one that crashed. Move it into a @DBOS.step(). "
        "CLAUDE.md non-negotiable #2"
    )

    in_steps = {call.rsplit(".", 1)[-1] for node in steps for call in _nondeterministic_calls(node)}
    assert in_steps & _IDENTITY_LEAVES, (
        "no step mints an identifier. The turn id must be generated INSIDE a @DBOS.step(), "
        "not passed in and not generated in the body. docs/TASKS.md#t-f2-01"
    )
    assert in_steps & _CLOCK_LEAVES, (
        "no step reads the clock. A workflow that stamps a time reads it inside a "
        "@DBOS.step() or it stamps a different time on every replay. CLAUDE.md #2"
    )


class _StepLog:
    """Stands in for DBOS's durable operation log, which is all the body can observe of it.

    First run: each step executes and its result is appended. Replay: the recorded results
    are handed back IN ORDER and no step body runs - exactly what DBOS does after a crash.
    """

    def __init__(self, recorded: Sequence[Any] = ()) -> None:
        self.decisions: list[tuple[str, str]] = []
        self.executed: list[str] = []
        self.recorded: list[Any] = list(recorded)
        self._cursor = 0

    async def call(self, name: str, label: str, produce: Callable[[], Any]) -> Any:
        self.decisions.append((name, label))
        if self._cursor < len(self.recorded):
            result = self.recorded[self._cursor]
        else:
            self.executed.append(name)
            result = produce()
            self.recorded.append(result)
        self._cursor += 1
        return result


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s-f2"), tenant_id=TenantId("t-f2")),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="http",
            tenant_id=TenantId("t-f2"),
            roles=frozenset({"operator"}),
        ),
        profile_id="p-1",
        input=UserInput(text="freeze the account"),
    )


def _pending(tool_call_id: str) -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="freeze_account",
        arguments={},
        reason="a human must agree",
    )


# Deliberately NOT in sorted order. Whatever order the runner produced, the workflow body
# must dispatch in one that does not depend on it - CLAUDE.md non-negotiable #7.
def _suspended(turn_id: TurnId) -> TurnOutcome:
    return TurnOutcome(turn_id=turn_id, pending=(_pending("tc-b"), _pending("tc-a")))


def _finished(turn_id: TurnId) -> TurnOutcome:
    return TurnOutcome(turn_id=turn_id, result=TurnResult(text="account frozen"))


def _workflow_body() -> _WorkflowBody:
    """The undecorated body inside `@DBOS.workflow()`.

    DBOS wraps a workflow twice and both wrappers refuse to run before DBOS is
    initialised, which is correct and is why `inspect.unwrap` is used to reach past them:
    what is under test is the BODY's decisions, and those are the same decisions whether a
    durable engine or this test is feeding it the step log.
    """
    decorated = turn_workflow.run_turn_workflow
    body = cast("_WorkflowBody", inspect.unwrap(decorated))
    assert body is not decorated, (
        "turn_workflow.run_turn_workflow is not decorated with @DBOS.workflow(), so there "
        "is no workflow body to replay. docs/TASKS.md#t-f2-01"
    )
    return body


def _install(
    monkeypatch: pytest.MonkeyPatch,
    log: _StepLog,
    *,
    mint: Callable[[], TurnId],
    resumes_to: Callable[[TurnId], TurnOutcome],
) -> None:
    """Replace every step and the durable receive with a recorder over `log`.

    The steps are module GLOBALS on purpose: the body resolves them at call time, so this
    swaps them without touching the body, and what is exercised is the body's own decision
    sequence rather than any step's work.
    """

    async def new_turn_id() -> Any:
        return await log.call("_step_new_turn_id", "", mint)

    async def start(turn_id: TurnId, request: TurnRequest) -> Any:
        return await log.call("_step_start", str(turn_id), lambda: _suspended(turn_id))

    async def publish(
        turn_id: TurnId, request: TurnRequest, pending: tuple[PendingRequest, ...]
    ) -> Any:
        label = ",".join(str(item.tool_call_id) for item in pending)
        return await log.call("_step_publish", label, lambda: None)

    async def resume(turn_id: TurnId, request: TurnRequest, answer: object) -> Any:
        return await log.call("_step_resume", str(turn_id), lambda: resumes_to(turn_id))

    async def abandon(turn_id: TurnId, reason: str) -> Any:
        return await log.call(
            "_step_abandon",
            reason,
            lambda: TurnOutcome(turn_id=turn_id, result=TurnResult(text=reason)),
        )

    async def expire(turn_id: TurnId) -> Any:
        return await log.call(
            "_step_expire",
            "",
            lambda: TurnOutcome(turn_id=turn_id, result=TurnResult(text="expired")),
        )

    class _DurableReceive:
        @staticmethod
        async def recv_async(topic: str | None = None, timeout_seconds: float = 60) -> Any:
            return await log.call("recv_async", f"{topic}:{timeout_seconds}", lambda: ("answer",))

    monkeypatch.setattr(turn_workflow, "_step_new_turn_id", new_turn_id)
    monkeypatch.setattr(turn_workflow, "_step_start", start)
    monkeypatch.setattr(turn_workflow, "_step_publish", publish)
    monkeypatch.setattr(turn_workflow, "_step_resume", resume)
    monkeypatch.setattr(turn_workflow, "_step_abandon", abandon)
    monkeypatch.setattr(turn_workflow, "_step_expire", expire)
    monkeypatch.setattr(turn_workflow, "DBOS", _DurableReceive)


def _minter() -> Callable[[], TurnId]:
    """A fresh id per EXECUTION. If replay re-executes the step, the id changes and the
    comparison below fails - which is the whole point of not returning a constant."""
    minted = 0

    def mint() -> TurnId:
        nonlocal minted
        minted += 1
        return TurnId(f"turn-{minted}")

    return mint


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_replaying_the_workflow_body_reaches_the_same_decision_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body is driven twice: once executing its steps, once replaying their results.

    The second pass is what DBOS does after a crash - the step log is handed back and no
    step body runs. If the body reaches a different sequence, or a different turn id, the
    recovered turn is not the turn that crashed.
    """
    body = _workflow_body()

    first = _StepLog()
    _install(monkeypatch, first, mint=_minter(), resumes_to=_finished)
    outcome = asyncio.run(body(_request()))

    replayed = _StepLog(recorded=first.recorded)
    _install(monkeypatch, replayed, mint=_minter(), resumes_to=_finished)
    replayed_outcome = asyncio.run(body(_request()))

    assert replayed.executed == [], (
        f"replay re-executed {replayed.executed}. A step whose result is already in the "
        "log must not run again - that is what makes the already-executed tool run twice."
    )
    assert replayed.decisions == first.decisions, (
        "the workflow body took a different path on replay:\n"
        f"  first : {first.decisions}\n"
        f"  replay: {replayed.decisions}\n"
        "CLAUDE.md non-negotiable #2 - non-determinism belongs inside a step."
    )
    assert replayed_outcome == outcome
    assert outcome.result is not None

    publishes = [label for name, label in first.decisions if name == "_step_publish"]
    assert publishes == ["tc-a,tc-b"], (
        f"pending requests were dispatched as {publishes}. The runner produced them "
        "unsorted; a workflow body that preserves that order dispatches differently "
        "whenever the producer's order differs. CLAUDE.md non-negotiable #7 - sort the "
        "work before dispatching it."
    )

    waits = [label for name, label in first.decisions if name == "recv_async"]
    assert waits and all(
        label.endswith(f":{turn_workflow.THREE_DAYS}") for label in waits
    ), (
        f"the durable wait ran with {waits}. DBOS.recv defaults to 60 seconds "
        "(docs/FIELD-NOTES.md, DBOS 2.31.1): without an explicit timeout the wait for a "
        "human quietly becomes a one-minute wait."
    )


@pytest.mark.phase("F2")
def test_a_turn_that_never_stops_asking_is_abandoned_rather_than_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed turn may suspend again; a tool that always defers must not pester a real
    person forever. The bound is `MAX_HUMAN_ROUNDS` and the exit is a step, not a raise."""
    body = _workflow_body()

    log = _StepLog()
    _install(monkeypatch, log, mint=_minter(), resumes_to=_suspended)
    outcome = asyncio.run(body(_request()))

    publishes = [name for name, _ in log.decisions if name == "_step_publish"]
    assert len(publishes) == turn_workflow.MAX_HUMAN_ROUNDS
    assert [name for name, _ in log.decisions][-1] == "_step_abandon"
    assert outcome.pending == ()
    assert outcome.result is not None


@pytest.mark.skip(reason="F2 - the acceptance criterion for the phase")
def test_killing_the_process_midturn_does_not_rerun_the_tool() -> None:
    """PSEUDO-CODE - implement in F2.

    1. Start a turn that calls two tools; the first records a side effect.
    2. Kill the process after tool one, before the turn is persisted.
    3. Restart.
    4. Assert the turn completes AND the first tool ran EXACTLY ONCE.

    This is the test the whole durability phase exists for.
    """


@pytest.mark.skip(reason="F3 - the acceptance criterion for the phase")
def test_approval_survives_a_redeploy() -> None:
    """PSEUDO-CODE - implement in F3.

    1. Start a turn that suspends on approval.
    2. Tear the process down and bring it back (simulating a deploy).
    3. Approve.
    4. Assert the turn resumes correctly.

    Fake the clock rather than waiting 24 hours; the point is process death, not elapsed
    time.
    """


@pytest.mark.skip(reason="F3")
def test_tool_call_ids_round_trip_verbatim() -> None:
    """A regenerated or re-cased tool_call_id is dropped by Pydantic AI WITHOUT an
    exception, and the agent asks the same question forever. Assert byte equality."""


@pytest.mark.skip(reason="F3")
def test_resolving_the_same_request_twice_is_a_noop() -> None:
    """Humans double-click and retries re-post. The second resolution must return the
    stored outcome, not run the tool again."""


@pytest.mark.skip(reason="F7")
def test_evidence_request_resumes_with_the_uploaded_image() -> None:
    """The F7 acceptance criterion: the agent asks for a photo, the turn waits, the user
    uploads from another device, the turn resumes with the image as the tool result."""
