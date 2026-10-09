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
    docs/TASKS.md#t-f2-02, and it is at the BOTTOM of this file rather than skipped. These
    do not replace it. They fail on a laptop in milliseconds, which is what makes them
    worth having next to it.

WHAT THE BOTTOM HALF OF THIS FILE IS, AND WHY IT USED TO BE FIVE `@pytest.mark.skip`s
    Five tests here shipped as pseudo-code behind a skip marker, and two of them ARE the
    done-when criteria of F3 and F7 as docs/TASKS.md states them. docs/WAVES.md says what
    that costs: *a phase whose own acceptance criterion is an unowned skipped test cannot
    be finished, only declared finished.* They are implemented below, against a real
    Postgres and a real DBOS, and two of them against a real process that is really killed.

    THE KILL IS A KILL. `subprocess.Popen(...).kill()` - TerminateProcess on Windows,
    SIGKILL elsewhere - with no handler, no flush and no `DBOS.destroy()`. A simulated
    crash (destroy-and-relaunch in one process) proves nothing about recovery: destroy
    drains, and the whole defect class here is what a worker leaves behind when it does
    NOT get to drain. The child writes what it observed to a file as it goes, because a
    killed process cannot report anything afterwards.

    WHAT THE CHILD PROCESSES PROVE THAT NO IN-PROCESS TEST CAN
        `agent-core-turns` is partitioned with `partition_concurrency=1`, and DBOS's
        partitioned-dequeue probe is unscoped by application version while startup
        recovery is scoped to it (docs/TASKS.md#t-f2-12). So a killed worker's PENDING row
        holds that session's only slot until a process claiming the SAME
        `application_version` recovers it. These two tests are the first thing in this
        repository that exercises the pin: both children build their configuration through
        `composition.dbos_config`, which is where the pin lives, and neither is allowed to
        assemble one of its own.

    WHAT CANNOT BE PROVED HERE, STATED RATHER THAN WEAKENED
        F3's criterion says "approving 24h later". Nothing waits a day, and faking the
        clock would fake the durable timer as well - DBOS computes the deadline inside its
        own system database. What IS proved is the half that actually fails in production:
        the wait survives the process being killed, and its deadline is `THREE_DAYS`
        passed explicitly, not the 60-second default that would make a one-minute wait
        look like a durable one on a fast machine (docs/FIELD-NOTES.md).

        F7's criterion is proved as far as the durable boundary reaches - the deferred call
        resolves with the uploaded image - and the two seats still unwired ABOVE it are
        named in that test's own docstring rather than implied by a green tick: the upload
        route does not wake the turn, and `HumanAnswer.note` is declared `str | None` so no
        type-checked caller can put an image on the wire the way that test does.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from agent_core.adapters.driven.human import gateway
from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.http import routes
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.start_turn import StartTurn
from agent_core.composition import Settings, dbos_config
from agent_core.domain.compaction import CompactionPolicy, CompactionResult, ContextState
from agent_core.domain.media import MediaId, MediaKind, MediaRef
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
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
    Usage,
    UserInput,
)
from agent_core.ports.agent_runner import ToolResolution
from agent_core.ports.human_gateway import HumanGateway
from agent_core.ports.skill_registry import SkillMeta
from tests.fakes.ports import (
    FakeAgentRunner,
    FakeAuditSink,
    FakeConversationStore,
    FakeToolPolicy,
    FakeToolProvider,
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
    appear in ANY `@DBOS.workflow()` body.

    THIS GUARD USED TO COUNT THE WORKFLOWS, AND THE COUNT WAS NEVER THE PROPERTY.
        It opened with `len(workflows) == 1` and then checked `workflows[0]` alone. That
        went red when `t-f2-11` added the coalescing window - a SECOND `@DBOS.workflow()`,
        `run_turn_window`, which exists because dbos 2.31.1 refuses `deduplication_id` on a
        partitioned queue and the module docstring carries the evidence. Nothing about
        non-determinism had moved: the body assertion still passed, and still passes.

        The count was also hiding a hole. Checking `workflows[0]` meant the second
        workflow's body was never checked at all, so the day it was added it became the one
        place in this file where a `uuid4()` could sit unguarded. Asserting over EVERY
        workflow body is what the rule always said, and it is strictly stronger than what
        was here before - it covers a function the old assertion could not see.

        The count is not restored in a wider form ("at most two") either. A number is a
        fact about today's file that drifts the next time a queue needs a workflow, and a
        guard that has to be edited for a legal change teaches everyone to edit guards.

    `uuid4()` DOES appear elsewhere in the file, in `enqueue_turn_window`, and that is not
    a violation: it is an ordinary async function called from an HTTP handler, not a
    workflow body, and the value it mints is consumed by the enqueue in the same call. The
    rule is about what replays, so the assertion is about what replays.

    THE IDENTITY HALF WAS INVERTED BY docs/TASKS.md#t-f0-06, AND INVERTING IT MADE IT
    STRONGER
        It used to read "no step mints an identifier" as a FAILURE - the turn id had to be
        generated inside a `@DBOS.step()`, because a step result is recorded and therefore
        replays the same. That was true and it was not enough: the id the caller was handed
        by `POST /turns` was the workflow's, and the id the audit rows, the correlation
        table and `DecideApproval`'s wake-up used was the minted one. Two identifiers
        wearing one type name, joined by nothing, and every lookup across them came back
        empty forever without raising.

        So `_step_new_turn_id` now READS the running workflow's durable id instead, and a
        minted identifier in a step has become the defect rather than the requirement. The
        assertion is inverted to match, and the property it defends is unchanged and now
        provable rather than merely recorded: a durable workflow id is the key the
        operation log is stored under, so it cannot differ between the run that crashed and
        the run that recovers it.

        The inversion alone would be satisfied by a step that takes the id as an argument
        or invents nothing at all, so the behavioural half is asserted beside it: reaching
        that step without a running workflow RAISES, naming `enqueue_turn`. That is what
        makes "the id comes from the workflow" a fact rather than an absence.
    """
    tree = ast.parse(_WORKFLOW_SOURCE.read_text(encoding="utf-8"))

    workflows = _functions_decorated_with(tree, "DBOS.workflow")
    steps = _functions_decorated_with(tree, "DBOS.step")

    assert [node.name for node in workflows] and "run_turn_workflow" in [
        node.name for node in workflows
    ], (
        f"{_WORKFLOW_SOURCE.name} declares no @DBOS.workflow() called run_turn_workflow, "
        f"only {[node.name for node in workflows]}. There is no turn to make durable and "
        "nothing below this line is testing anything. docs/TASKS.md#t-f2-01"
    )
    assert steps, (
        f"{_WORKFLOW_SOURCE.name} declares no @DBOS.step(). The turn id, the clock and any "
        "randomness have nowhere deterministic to live. CLAUDE.md non-negotiable #2"
    )

    in_a_body = {
        node.name: sorted(_nondeterministic_calls(node))
        for node in workflows
        if _nondeterministic_calls(node)
    }
    assert in_a_body == {}, (
        f"these are workflow BODIES and they call {in_a_body}. Every one of those returns "
        "a different value on replay, so the recovered turn takes a different branch from "
        "the one that crashed. Move it into a @DBOS.step(). CLAUDE.md non-negotiable #2"
    )

    in_steps = {call.rsplit(".", 1)[-1] for node in steps for call in _nondeterministic_calls(node)}
    assert in_steps & _IDENTITY_LEAVES == set(), (
        f"a @DBOS.step() mints an identifier ({sorted(in_steps & _IDENTITY_LEAVES)}). Since "
        "docs/TASKS.md#t-f0-06 the domain TurnId IS the workflow's durable id, read off the "
        "running workflow - so a minted one is a THIRD identifier addressing no workflow, "
        "which is the defect that anchor removed, not the rule it kept."
    )
    with pytest.raises(turn_workflow.NotInsideAWorkflowError) as refused:
        asyncio.run(turn_workflow._step_new_turn_id())
    assert "enqueue_turn" in str(refused.value), (
        f"the refusal does not say how a turn is started: {refused.value}. The id comes "
        "from the running workflow, so a caller that reached this step without one bypassed "
        "the queue and needs to be told which door to use."
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


# The peer-ask mechanism's tool name, SPELT rather than imported - the same choice
# test_communicable_suspension.py makes and for the same reason. If this test imported the
# constant the production code matches on, the two would agree by construction and the
# assertion would restate the import instead of pinning the behaviour.
_ASK_PEER = "ask_peer"


def _peer_pending(tool_call_id: str) -> PendingRequest:
    """What an `ask_peer` (docs/TASKS.md#t-f9-04) suspension actually looks like.

    `PendingKind.DELEGATION`, exactly as `domain/turn.py::PendingKind` documents since
    `t-f9-08`. THIS FIXTURE USED TO SAY `PendingKind.EVIDENCE`, quoting the very docstring
    that `t-f9-08` rewrote to add the kind this fixture was built to prove didn't exist -
    the two drifted apart, and the drift was silent because `EVIDENCE` is legitimately
    human-answerable (`HUMAN_ANSWERABLE[PendingKind.EVIDENCE] is True`), so the workflow
    correctly published it and the test failed for being wrong, not for catching a
    regression. Production was never broken; only this fixture still wore the discriminator
    `t-f9-08` retired.
    """
    return PendingRequest(
        kind=PendingKind.DELEGATION,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name=_ASK_PEER,
        arguments={"target": "personal-assistant-of-alice", "question": "which window?"},
        reason="I am checking on that now and will come back to you as soon as I have an answer.",
    )


def _delegated_pending_under_an_unrelated_name(tool_call_id: str) -> PendingRequest:
    """A DELEGATION that does not carry the `ask_peer` tool name.

    Exists to pin `t-f9-08`'s actual point: `_answerable_by_a_human` reads `kind`, never
    `tool_name`. If exclusion were still keyed on the string `"ask_peer"` - the retired
    discriminator - this request would slip through and reach a human, because nothing
    about its `tool_name` matches that string. Its kind is the only reason it must be
    excluded.
    """
    return PendingRequest(
        kind=PendingKind.DELEGATION,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="some_other_delegated_tool",
        arguments={},
        reason="another agent is handling this",
    )


# Deliberately NOT in sorted order. Whatever order the runner produced, the workflow body
# must dispatch in one that does not depend on it - CLAUDE.md non-negotiable #7.
def _suspended(turn_id: TurnId) -> TurnOutcome:
    return TurnOutcome(turn_id=turn_id, pending=(_pending("tc-b"), _pending("tc-a")))


def _suspended_on_a_peer_and_a_human(turn_id: TurnId) -> TurnOutcome:
    """One suspension carrying both kinds of wait: a peer's answer and a human's - plus a
    second DELEGATION wearing an unrelated tool name, so no assertion built on this outcome
    can be satisfied by matching `tool_name == "ask_peer"` instead of `kind == DELEGATION`.

    Both in one outcome on purpose. A filter that dropped the whole publish whenever a
    peer ask was present would pass a peer-only test and still lose the approval a person
    is standing by to give.
    """
    return TurnOutcome(
        turn_id=turn_id,
        pending=(
            _peer_pending("tc-peer"),
            _pending("tc-a"),
            _delegated_pending_under_an_unrelated_name("tc-other"),
        ),
    )


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


class _ReplaySafeChannel:
    """The channel a finished turn leaves on here: it records, and produces NOTHING.

    `_step_deliver` (t-f3-07) is deliberately NOT swapped by `_install` below. Every other
    step is replaced so the body's decisions can be recorded, but delivery is left real,
    because "the finished turn is handed to a channel" is one of the decisions the body
    makes and a stubbed step would prove it about the stub instead. Left real, it resolves
    its channel through the module binding, so this test has to wire one or every run dies
    with `TurnWorkflowNotWiredError` before it reaches the comparison.

    WHAT THIS CHANNEL MUST NOT DO, AND WHY IT IS SPELT OUT
        This is a REPLAY-DETERMINISM test (CLAUDE.md non-negotiable #2). A channel that
        produced a value - a send timestamp, a platform message id, a retry count - would
        put a fresh value into the run each time, and the two passes would then differ for
        a reason that has nothing to do with the body. Appending to a list is the whole
        implementation on purpose: it observes, it decides nothing, and it returns None.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _RecordingMailbox:
    """An `AgentMailbox` that records what left and answers like the real adapter.

    Minted ids are sequential rather than random: this file compares two runs of the same
    body, and a uuid here would differ between them for a reason that has nothing to do
    with the workflow. The peer's bytes are stored VERBATIM and wrapped on read, which is
    what `ports/agent_mailbox.py` requires of every implementation.
    """

    def __init__(self) -> None:
        self.asks: list[tuple[AgentId, str, int]] = []
        self._answers: dict[str, str] = {}

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        return policy.peers

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
        # t-f11-50. The port's own default, restated rather than chosen: `None` means the
        # asker is UNKNOWN, never "anyone may ask". A double that invented an identity
        # here would let the answering side's `callee_policy.may_ask(caller)` pass in a
        # test and refuse in production - a double arguing the opposite of the code.
        asker: AgentId | None = None,
    ) -> str:
        del policy, from_session, turn_id, asker
        self.asks.append((target, question, hop))
        return f"corr-{len(self.asks)}"

    async def answer(self, correlation_id: str, answer: str) -> None:
        self._answers.setdefault(correlation_id, answer)

    async def read_answer(self, correlation_id: str) -> str | None:
        raw = self._answers.get(correlation_id)
        return None if raw is None else wrap_peer_answer(raw)


class _InBandResume:
    """`ResumeTurn` as `_step_refuse_peers` reaches it, recording what it was handed.

    `_step_resume` is swapped out by `_install`, so this is resolved on ONE path only: a
    peer ask the gate refused, resolved in-band rather than waited on. It returns the same
    outcome the caller's `resumes_to` produces, so the body reaches an answer down either
    route and the decision sequence stays the thing under test.
    """

    def __init__(self, resumes_to: Callable[[TurnId], TurnOutcome]) -> None:
        self._resumes_to = resumes_to
        self.resolutions: list[ToolResolution] = []

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[ToolResolution, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        del session, profile_id, caller
        self.resolutions.extend(resolutions)
        return self._resumes_to(turn_id)


# The asking profile is the one `_request()` runs under; the peer is the one
# `_peer_pending` addresses. Both sides name each other, because `hop_limit.authorise_hop`
# asks both - a one-sided allowlist is bypassable by whoever controls the other side.
_PEER_TARGET = "personal-assistant-of-alice"


def _peer_profiles() -> dict[str, AgentProfile]:
    return {
        _request().profile_id: AgentProfile(
            id=_request().profile_id,
            persona="asks",
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=(AgentRef(agent_id=AgentId(_PEER_TARGET), display_name="Alice's PA"),),
                max_hops=1,
            ),
        ),
        _PEER_TARGET: AgentProfile(
            id=_PEER_TARGET,
            persona="answers",
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=(
                    AgentRef(
                        agent_id=AgentId(_request().profile_id), display_name="The asker"
                    ),
                ),
                max_hops=1,
            ),
        ),
    }


@dataclass(frozen=True, slots=True)
class _Wiring:
    """What `_install` bound that a test may want to read back."""

    mailbox: _RecordingMailbox
    resume: _InBandResume


def _install(
    monkeypatch: pytest.MonkeyPatch,
    log: _StepLog,
    *,
    mint: Callable[[], TurnId],
    resumes_to: Callable[[TurnId], TurnOutcome],
    suspends_to: Callable[[TurnId], TurnOutcome] = _suspended,
) -> _Wiring:
    """Replace every step and the durable receive with a recorder over `log`.

    The steps are module GLOBALS on purpose: the body resolves them at call time, so this
    swaps them without touching the body, and what is exercised is the body's own decision
    sequence rather than any step's work.

    THE PEER SEAT AND THE RESUME SEAT ARE REAL, AND `_step_ask_peers` IS NOT SWAPPED
        Unlike the steps above, the peer steps are left alone: which requests leave as
        questions is a decision the BODY makes, and a stubbed step would prove it about the
        stub. That makes the seats mandatory - `_require_peer_seat` raises
        `TurnWorkflowNotWiredError` on the first turn that genuinely asks one, which is
        correct and loud and is exactly what this file used to die of. So a mailbox and the
        two profiles are bound here, and `resume_turn` with them: `_step_refuse_peers`
        resolves a refused ask through the real `ResumeTurn` seat, and that seat has no
        silent default either.
    """

    async def new_turn_id() -> Any:
        return await log.call("_step_new_turn_id", "", mint)

    async def start(turn_id: TurnId, request: TurnRequest) -> Any:
        return await log.call("_step_start", str(turn_id), lambda: suspends_to(turn_id))

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

    # `_step_deliver` stays real (see `_ReplaySafeChannel`), so the workflow's
    # collaborators have to exist. Registered under the channel the request arrives on,
    # read off the request rather than restated, so the two cannot drift apart and leave
    # this test passing against `UnknownChannelError` instead of a delivery.
    #
    # `monkeypatch.setattr` rather than `bind_dependencies()`, for the reason
    # test_delivery.py gives: the binding is a process-wide module global by design, so a
    # test that called the real binder would leave its fakes wired for every test that runs
    # after it. monkeypatch puts the previous value back.
    #
    # `start_turn` is never resolved - `_step_start` is swapped above and it is the only
    # step that reaches for it - so there is no use case to build here and none is faked.
    mailbox = _RecordingMailbox()
    resume_turn = _InBandResume(resumes_to)
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", None),
            channels=ChannelRegistry(((_request().caller.channel, _ReplaySafeChannel()),)),
            resume_turn=cast("Any", resume_turn),
            peers=turn_workflow.PeerSeat(
                mailbox=cast("Any", mailbox), profiles=_peer_profiles()
            ),
        ),
    )
    return _Wiring(mailbox=mailbox, resume=resume_turn)


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


@pytest.mark.phase("F3")
def test_a_peer_ask_is_never_put_in_front_of_a_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request only another AGENT can resolve is never published ahead of - or beside -
    one a human can actually answer, and the reason it is excluded is its KIND, not its
    tool name.

    `ask_peer` (docs/TASKS.md#t-f9-04) suspends the turn and is answered by the peer's own
    turn, never by a human. Publishing it asks somebody a question they have no way to
    answer, and the turn then waits three days on a person who cannot help while the peer's
    answer arrives on the same durable topic anyway.

    THE DISCRIMINATOR IS `PendingKind.DELEGATION`, NOT THE STRING `"ask_peer"` (t-f9-08)
        `_suspended_on_a_peer_and_a_human` carries a SECOND delegation
        (`_delegated_pending_under_an_unrelated_name`) that does not wear the `ask_peer`
        tool name at all. It must be excluded too, or this test would only be proving that
        one specific tool name is blocklisted - which is the exact defect `t-f9-08`
        removed - rather than that the whole `PendingKind.DELEGATION` kind is.

    WHERE EACH DELEGATION ACTUALLY GOES, NOW THAT `_step_ask_peers` IS BOUND (t-f11-49)
        The two delegations do NOT have the same fate, and the assertion below says so
        rather than averaging them. `tc-peer` carries the model's own `target` and
        `question`, so it leaves as a real `AgentMailbox.ask()`. `tc-other` carries
        `arguments={}`, which `_step_ask_peers` refuses as malformed rather than coercing -
        an empty question asked of "another agent" bills a peer turn for a question nobody
        can answer - and a refused ask has no answer coming, so the body resolves it IN
        BAND through `_step_refuse_peers` instead of entering the durable wait for it.

        So this test no longer reaches `recv_async` at all, and asserting that it does
        would be asserting the opposite of the behaviour that keeps a turn from spending
        three days waiting on a question that was never asked. What survives unweakened is
        the property this test exists for: neither delegation was put in front of a person.
    """
    body = _workflow_body()

    log = _StepLog()
    wiring = _install(
        monkeypatch,
        log,
        mint=_minter(),
        resumes_to=_finished,
        suspends_to=_suspended_on_a_peer_and_a_human,
    )
    outcome = asyncio.run(body(_request()))

    publishes = [label for name, label in log.decisions if name == "_step_publish"]
    assert publishes == ["tc-a"], (
        f"the workflow published {publishes} to a human. 'tc-peer' and 'tc-other' are both "
        "PendingKind.DELEGATION - another agent answers them and no person ever can, so a "
        "human was sent a question that hangs forever. Publish only what a human can act "
        "on, decided by kind, never by tool name."
    )

    assert [target for target, _, _ in wiring.mailbox.asks] == [AgentId(_PEER_TARGET)], (
        f"the mailbox was asked {wiring.mailbox.asks}. Dropping a peer ask from the PUBLISH "
        "must not drop the ask itself: 'tc-peer' names a target and a question, and the "
        "turn it belongs to suspends until another agent answers it."
    )
    resolved = [str(resolution.tool_call_id) for resolution in wiring.resume.resolutions]
    assert resolved == ["tc-other"], (
        f"the refused ask resolved {resolved}. 'tc-other' is a DELEGATION with no target "
        "and no question, so nothing will ever answer it - a turn that entered the durable "
        "wait for it would sit there for THREE_DAYS and then report that nobody replied, "
        "to a question that was never asked."
    )
    assert outcome.result is not None


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_the_ask_a_human_reads_names_no_tool_and_no_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAUDE.md non-negotiable #11, on the OUTBOUND half nobody had closed.

    The transcript projection was fixed for exactly this (docs/TASKS.md#t-f10-01): a USER
    sees `PENDING_PLACEHOLDER` and never `PENDING_REQUEST`, so the tool name never reaches
    them through a read. The channel message is the same fact through a different door -
    `ChannelHumanGateway` addresses the SESSION on the configured channel, which is the
    user's own conversation, not an operator console.

    BOTH halves are asserted because either one alone is passable by a wrong fix. Going
    silent hides the tool name and makes the conversation look broken; naming the tool
    tells them what to answer and leaks which one it is.

    `test_human_gateway.py` owns the neighbouring property - no argument VALUE on the wire -
    and needs Postgres to say it. This one is about the NAMES, and the renderer is pure, so
    it runs anywhere.
    """
    del monkeypatch  # kept so the signature matches its neighbours; nothing is patched

    leaky = PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId("call-7"),
        tool_name="issue_refund",
        arguments={"amount_usd": "250.00", "api_key": "sk-live-not-a-real-token"},
        reason="a refund over 200 USD needs a human",
    )
    correlation_id = "Zm9yLXRoZS10ZXN0"

    text = gateway.render_ask(leaky, correlation_id).text

    for leaked in (leaky.tool_name, *leaky.arguments):
        assert leaked not in text, (
            f"the channel message names {leaked!r}: {text!r}. A user must not see WHICH "
            "tool is pending - CLAUDE.md non-negotiable #11, the same leak the transcript "
            "projection closed at docs/TASKS.md#t-f10-01."
        )
    assert correlation_id in text, (
        f"the message carries no correlation handle: {text!r}. Without it the human has "
        "nothing to answer against and POST /decisions/{corr_id} cannot route the reply."
    )
    assert leaky.reason in text, (
        f"the reason was dropped: {text!r}. For an approval the reason is the whole of "
        "what the person decides on; blanking it turns a four-eyes rule into a rubber stamp."
    )
    assert text.strip(), (
        "the message is empty. Hiding the suspension entirely is the other half of "
        "non-negotiable #11 broken - the user must see THAT something is pending."
    )


# ===========================================================================
# THE ACCEPTANCE CRITERIA OF F2, F3 AND F7
#
# docs/TASKS.md#t-f2-02, #t-f2-07, #t-f2-08, #t-f2-09, #t-f3-12, #t-f7-09.
#
# Everything above this line runs anywhere in milliseconds. Everything below needs a real
# Postgres and a real DBOS, and the first two need a real process that is really killed.
# ===========================================================================

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# This file's own DBOS system database, for the reason every launch helper in this
# directory gives: a run killed here - and two of these tests kill one on purpose - leaves
# rows behind, and destroying them must not touch state another file owns.
_DBOS_DATABASE = "agent_core_durability_test"

# The DBOS application NAME, which `_sys_db.get_pending_workflows` filters recovery on
# alongside the version. Both child processes must claim the same one or the second never
# sees what the first left behind - which is the deadlock t-f2-12 is about, reproduced by
# a typo instead of by a deploy.
_DBOS_APP_NAME = "agent-core-durability"

# `Core/`, so a child process can find `tests.fakes` and `agent_core` the way pytest does.
_CORE_DIR = Path(__file__).resolve().parents[2]

_ANSWER = "the account is frozen"
_RESUMED_ANSWER = "the account is frozen, and a human said so"
_APPROVAL_NOTE = "approved: the customer called in"

# A tool_call_id shaped like a PROVIDER's, not like a tidy test fixture. Mixed case, an
# underscore, a dash and a dot - every character class that a well-meaning normaliser
# lower-cases, strips or re-encodes on the way through. CLAUDE.md non-negotiable #5: a
# mangled id is dropped by Pydantic AI WITHOUT an exception, so nothing here may compare
# normalised forms.
_PROVIDER_TOOL_CALL_ID = "call_9aF-Zx.Q7_tB3"

# What a killed child leaves behind for the parent to read. A file, because a process that
# is killed cannot report anything afterwards and a pipe it never flushed says nothing.
_LEDGER = "ledger.log"
_READY = "ready.txt"
_DONE = "done.txt"
_RESOLUTIONS = "resolutions.json"

# Deadlines. NONE of these is a tolerance an assertion leans on: every property below is
# proved by something happening, and a slow machine only reaches it later. They exist so a
# recovery that never happens fails with a sentence instead of hanging the suite.
_CHILD_DEADLINE_SECONDS = 180.0
_RENDEZVOUS_DEADLINE_SECONDS = 45.0
_POLL_SECONDS = 0.05

# Wide enough that three consecutive local enqueues are unambiguously inside it. A
# PRECONDITION of the scenario ("these messages arrived close together"), never the
# measurement.
_WIDE_WINDOW_SECONDS = 8.0

# Used where a turn must actually be observed to run, and awaited through its handle
# rather than slept past.
_NARROW_WINDOW_SECONDS = 0.5


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _dbos_conninfo() -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{_DBOS_DATABASE}"


def _drop_databases() -> None:
    """Destroy this file's DBOS state before a scenario starts. NOT tidiness.

    `agent-core-turns` is partitioned with `partition_concurrency=1` and DBOS's
    partitioned-dequeue probe is unscoped by application version, so one PENDING row from
    an earlier run occupies its session's only slot and every later turn for that session
    is enqueued and never dequeued, with nothing raising. `test_delivery.py`'s barrier
    assertion refuses any launch helper in this directory that skips this call.

    THE TWO TESTS BELOW THAT KILL A PROCESS MAKE THIS MANDATORY RATHER THAN PRUDENT. They
    deliberately leave a PENDING row behind, twice; the second child is supposed to recover
    it, and every later run of this file must start from nothing or it would be recovering
    the last run's turn instead of its own.

    The destructive statement is deliberately the ONLY place in this module that spells it,
    so the guard reading this file is reading an executable call and not a comment about
    one - see the note on that guard in the report for this anchor.
    """
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DBOS_DATABASE}_dbos_sys", _DBOS_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def _durability_settings() -> Settings:
    """The `Settings` both this process and every child build their DBOS config from.

    Constructed rather than read from the environment on purpose: `Settings.from_env`
    would pick up `DBOS_CONDUCTOR_KEY` if the machine had one, and a conductor makes DBOS
    skip local, version-scoped recovery entirely (`_dbos.py`) and mint a per-process
    executor id. The crash tests below are ABOUT that local recovery path, so the one
    thing they must not do is silently take the other one.
    """
    return Settings(app_conninfo=_dbos_conninfo(), dbos_app_name=_DBOS_APP_NAME)


class _Dbos:
    """A launched DBOS for the length of one block, destroyed however the block ends.

    THE CONFIG COMES FROM `composition.dbos_config` AND IS NOT ASSEMBLED HERE
        docs/TASKS.md#t-f2-12 put the pinned `application_version` in exactly one place,
        and docs/TASKS.md#t-f2-13's `launch_dbos` exists so no launcher can hand DBOS a
        second opinion. A test that wrote `{"name": ..., "database_url": ...}` by hand
        would launch WITHOUT the pin - and would then pass while proving the opposite of
        what the crash tests below are for, because an unpinned version is an md5 of the
        workflow sources and both children compute the same one anyway.

        Going through `dbos_config` means the pin is what the children actually run on,
        so if somebody unpins it these tests are what goes red.
    """

    def __init__(self, *, drop: bool = True) -> None:
        self._drop = drop

    async def __aenter__(self) -> None:
        from dbos import DBOS

        if self._drop:
            _drop_databases()
        DBOS(config=cast("Any", dbos_config(_durability_settings())))
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


def _durability_request(
    session: str, *, tenant: str = "t-dur", text: str = "freeze it"
) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId(tenant)),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="http",
            tenant_id=TenantId(tenant),
            roles=frozenset({"operator"}),
        ),
        profile_id="p-1",
        input=UserInput(text=text),
    )


# ---------------------------------------------------------------------------
# What a child process records. Files, not objects: the point of these two tests is that
# the process holding the objects is killed.
# ---------------------------------------------------------------------------


def _note(directory: Path, line: str) -> None:
    """Append one observation. Opened and closed per line so a kill cannot lose it."""
    with (directory / _LEDGER).open("a", encoding="utf-8") as ledger:
        ledger.write(f"{line}\n")


def _ledger(directory: Path) -> list[str]:
    path = directory / _LEDGER
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


class _LedgerStartTurn:
    """Stands in for `StartTurn`, and its tool's side effect is a line in a file.

    THE SIDE EFFECT HAS TO BE OBSERVABLE FROM ANOTHER PROCESS OR THE TEST PROVES NOTHING
        "The tool did not run twice" is only an assertion if a second execution would
        leave a second trace. An in-memory counter dies with the process that is killed,
        so the counter is a file and the tool's execution is an append to it.

    A turn's tools run INSIDE `_step_start` - that is the one step that awaits the
    application layer - so this is where the turn's observable work happens and this is
    what must not be re-executed on recovery.
    """

    def __init__(self, directory: Path, *, suspend_on: str | None = None) -> None:
        self._directory = directory
        self._suspend_on = suspend_on

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        _note(self._directory, "tool")
        if self._suspend_on is None:
            return TurnOutcome(turn_id=turn_id, result=TurnResult(text=_ANSWER))
        return TurnOutcome(turn_id=turn_id, pending=(_pending(self._suspend_on),))


class _LedgerChannel:
    """Delivery, recorded - and, on the crashing child, the place the process is caught.

    `hold=True` parks the turn INSIDE `_step_deliver`, which is the one moment that makes
    the crash test mean anything: `_step_start` has returned, so its result is in the
    durable log, while this step's has not. Recovery must therefore replay the first and
    re-execute the second, and the ledger shows exactly that - one `tool`, two `deliver`.
    """

    def __init__(self, directory: Path, *, hold: bool) -> None:
        self._directory = directory
        self._hold = hold

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        _note(self._directory, "deliver")
        if not self._hold:
            return
        (self._directory / _READY).write_text("held", encoding="utf-8")
        while True:  # pragma: no cover - the process is killed here
            await asyncio.sleep(_POLL_SECONDS)


class _LedgerGateway:
    """`HumanGateway`, recorded. Publishing is what tells the parent to pull the plug."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        for request in requests:
            _note(self._directory, f"publish:{request.tool_call_id}")
        (self._directory / _READY).write_text("published", encoding="utf-8")

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return None


class _LedgerResume:
    """`ResumeTurn`, recorded, including the resolutions it was handed VERBATIM."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[ToolResolution, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        _note(self._directory, "resume")
        (self._directory / _RESOLUTIONS).write_text(
            json.dumps(
                [
                    {
                        "tool_call_id": str(resolution.tool_call_id),
                        "approved": resolution.approved,
                        "payload": resolution.payload
                        if isinstance(resolution.payload, str | None)
                        else repr(resolution.payload),
                    }
                    for resolution in resolutions
                ]
            ),
            encoding="utf-8",
        )
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=_RESUMED_ANSWER))


def _child_dependencies(
    directory: Path, *, suspend_on: str | None, hold_delivery: bool, resume: bool
) -> turn_workflow.TurnWorkflowDependencies:
    return turn_workflow.TurnWorkflowDependencies(
        start_turn=cast("Any", _LedgerStartTurn(directory, suspend_on=suspend_on)),
        channels=ChannelRegistry((("http", _LedgerChannel(directory, hold=hold_delivery)),)),
        human_gateway=cast("HumanGateway", _LedgerGateway(directory)),
        resume_turn=cast("Any", _LedgerResume(directory)) if resume else None,
    )


async def _run_child(
    mode: str, directory: Path, turn_id: TurnId, tool_call_id: ToolCallId
) -> None:
    """The child process's whole life. Four modes, two scenarios, one module.

    The dependencies are bound BEFORE `DBOS.launch()`, and that ordering is the point in
    the two recovering modes: startup recovery begins inside `launch`, so a process that
    wired its collaborators afterwards would race a recovered workflow's first step
    against its own composition root.
    """
    from dbos import DBOS, WorkflowHandleAsync

    if mode == "crash-turn":
        turn_workflow.bind_dependencies(
            _child_dependencies(directory, suspend_on=None, hold_delivery=True, resume=False)
        )
        async with _Dbos(drop=False):
            handle = await turn_workflow.enqueue_turn(
                _durability_request(str(turn_id)), turn_id=turn_id
            )
            await handle.get_result()  # pragma: no cover - killed inside _step_deliver
        return

    if mode == "recover-turn":
        turn_workflow.bind_dependencies(
            _child_dependencies(directory, suspend_on=None, hold_delivery=False, resume=False)
        )
        async with _Dbos(drop=False):
            recovered: WorkflowHandleAsync[TurnOutcome] = await DBOS.retrieve_workflow_async(
                str(turn_id)
            )
            outcome = await recovered.get_result()
            (directory / _DONE).write_text(
                "" if outcome.result is None else outcome.result.text, encoding="utf-8"
            )
        return

    if mode == "crash-approval":
        turn_workflow.bind_dependencies(
            _child_dependencies(
                directory, suspend_on=str(tool_call_id), hold_delivery=False, resume=False
            )
        )
        async with _Dbos(drop=False):
            handle = await turn_workflow.enqueue_turn(
                _durability_request(str(turn_id)), turn_id=turn_id
            )
            await handle.get_result()  # pragma: no cover - killed inside the durable wait
        return

    if mode == "recover-approval":
        turn_workflow.bind_dependencies(
            _child_dependencies(
                directory, suspend_on=str(tool_call_id), hold_delivery=False, resume=True
            )
        )
        async with _Dbos(drop=False):
            waiting: WorkflowHandleAsync[TurnOutcome] = await DBOS.retrieve_workflow_async(
                str(turn_id)
            )
            await turn_workflow.signal_decision(turn_id, tool_call_id, True, _APPROVAL_NOTE)
            outcome = await waiting.get_result()
            (directory / _DONE).write_text(
                "" if outcome.result is None else outcome.result.text, encoding="utf-8"
            )
        return

    raise SystemExit(f"unknown child mode {mode!r}")


class _Child:
    """One real subprocess, spawned from this very module, killable at a chosen moment.

    `sys.executable <this file> <mode> ...` rather than a fixture or a thread. A thread
    cannot be killed the way a worker dies, and `DBOS.destroy()` DRAINS - which is the one
    behaviour that must not happen, because the whole defect class is what an undrained
    worker leaves behind.

    Output goes to a file rather than a pipe: a killed child never closes its pipe, and a
    parent blocked on `communicate()` would hang the suite instead of failing it.
    """

    def __init__(
        self, mode: str, directory: Path, turn_id: TurnId, tool_call_id: ToolCallId
    ) -> None:
        self._mode = mode
        self._directory = directory
        self._log_path = directory / f"{mode}.out"
        self._turn_id = turn_id
        self._tool_call_id = tool_call_id
        self._log: Any = None
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> _Child:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(_CORE_DIR), environment.get("PYTHONPATH")) if part
        )
        environment["AGENT_CORE_TEST_ADMIN_DATABASE_URL"] = _ADMIN_CONNINFO
        self._log = self._log_path.open("wb")
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                self._mode,
                str(self._directory),
                str(self._turn_id),
                str(self._tool_call_id),
            ],
            cwd=str(_CORE_DIR),
            env=environment,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        return self

    def __exit__(self, *_: object) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=30)
        if self._log is not None:
            self._log.close()

    @property
    def _running(self) -> subprocess.Popen[bytes]:
        assert self._process is not None
        return self._process

    def output(self) -> str:
        if self._log is not None:
            self._log.flush()
        if not self._log_path.exists():
            return "<no output>"
        return self._log_path.read_text(encoding="utf-8", errors="replace")

    def wait_for_file(self, name: str, what: str) -> None:
        deadline = time.monotonic() + _CHILD_DEADLINE_SECONDS
        while not (self._directory / name).exists():
            assert self._running.poll() is None, (
                f"the {self._mode!r} process exited with {self._running.returncode} before "
                f"{what}. Its output was:\n{self.output()}"
            )
            assert time.monotonic() < deadline, (
                f"waited {_CHILD_DEADLINE_SECONDS:.0f}s and {what} never happened in the "
                f"{self._mode!r} process. Its output was:\n{self.output()}"
            )
            time.sleep(_POLL_SECONDS)

    def wait_for_exit(self, what: str) -> None:
        try:
            self._running.wait(timeout=_CHILD_DEADLINE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
            self._running.kill()
            raise AssertionError(
                f"the {self._mode!r} process never finished after {what}. Its output "
                f"was:\n{self.output()}"
            ) from None
        assert self._running.returncode == 0, (
            f"the {self._mode!r} process exited with {self._running.returncode}. Its "
            f"output was:\n{self.output()}"
        )

    def kill(self) -> None:
        """No signal handler, no flush, no DBOS.destroy(). A worker dying, not leaving."""
        self._running.kill()
        self._running.wait(timeout=30)


# ---------------------------------------------------------------------------
# t-f2-02 - the crash-injection test the whole durability phase exists for
# ---------------------------------------------------------------------------


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_killing_the_process_midturn_does_not_rerun_the_tool(tmp_path: Path) -> None:
    """docs/TASKS.md#t-f2-02. F2 is done when this passes and not before.

    THE SHAPE, AND WHY THE KILL LANDS WHERE IT DOES
        A turn runs its tools inside `_step_start`; delivery is the step after it. The
        child is parked INSIDE `_step_deliver`, so at the moment it is killed exactly one
        step's result is in the durable log and the next one's is not. Recovery must
        therefore do two different things to two adjacent steps, and the ledger can tell
        them apart:

          - `tool` appears ONCE across both processes. `_step_start` completed, so its
            result is replayed out of the operation log and the tool does not run again.
            That is the criterion, in one number.
          - `deliver` appears TWICE. `_step_deliver` did not complete, so recovery
            re-executes it - the at-least-once delivery window `turn_workflow.py` documents
            deliberately rather than hides. Asserting it proves the workflow really
            REPLAYED rather than being resumed from somewhere convenient; a run that
            re-executed nothing would not have recovered at all.

    IF THE SECOND PROCESS HANGS, THAT IS THE t-f2-12 DEADLOCK AND NOT A SLOW MACHINE
        A PENDING row holds its session's only partition slot, and startup recovery only
        looks at rows whose `application_version` matches the one this process claims. Both
        children build their config through `composition.dbos_config`, which pins that
        version. Unpin it and this test stops passing - which is the whole reason it goes
        through the composition root instead of assembling a config of its own.
    """
    _drop_databases()
    turn_id = TurnId(f"turn-{uuid4().hex}")
    tool_call_id = ToolCallId(_PROVIDER_TOOL_CALL_ID)

    with _Child("crash-turn", tmp_path, turn_id, tool_call_id) as crashing:
        crashing.wait_for_file(_READY, "the turn ran its tool and reached delivery")
        crashing.kill()

    mid_flight = _ledger(tmp_path)
    assert mid_flight.count("tool") == 1, (
        f"the ledger reads {mid_flight} before the restart. The scenario requires exactly "
        "one execution to have happened at the moment of the kill; anything else means the "
        "test killed the wrong moment and nothing below is about recovery."
    )

    with _Child("recover-turn", tmp_path, turn_id, tool_call_id) as recovering:
        recovering.wait_for_file(_DONE, "the recovered turn completed")
        recovering.wait_for_exit("the recovered turn completed")

    recovered = _ledger(tmp_path)
    assert recovered.count("tool") == 1, (
        f"the tool ran {recovered.count('tool')} times across a crash and a restart "
        f"(ledger: {recovered}). A step whose result is already in the durable log must "
        "not be re-executed on recovery - re-running it freezes the account twice, and "
        "nothing anywhere raises. CLAUDE.md non-negotiable #2, docs/TASKS.md#t-f2-02."
    )
    assert recovered.count("deliver") == 2, (
        f"delivery ran {recovered.count('deliver')} times (ledger: {recovered}). The "
        "crashed process died INSIDE _step_deliver, so that step's result was never "
        "recorded and recovery must re-execute it. Once means the workflow never replayed "
        "and the turn above finished for some other reason."
    )
    assert (tmp_path / _DONE).read_text(encoding="utf-8") == _ANSWER, (
        "the recovered turn produced no answer. Surviving the crash is only half of it: "
        "the turn has to COMPLETE, or the user is left with a workflow that recovered into "
        "silence."
    )


# ---------------------------------------------------------------------------
# t-f3-12 - the three F3 criteria
# ---------------------------------------------------------------------------


@pytest.mark.phase("F3")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_an_approval_survives_a_redeploy_and_resumes_long_afterwards(tmp_path: Path) -> None:
    """docs/TASKS.md#t-f3-12, first of three. F3 is done when this passes.

    A turn suspends on an approval, the process holding it is KILLED, another process comes
    up on the same pinned application version, and the approval arriving there resumes the
    turn that the dead process started.

    WHAT "24h LATER" IS AND IS NOT PROVED BY
        Nothing here waits a day, and nothing fakes the clock either: the deadline lives
        inside DBOS's own system database, so a faked clock in this process would fake the
        assertion rather than the wait. The two halves that DO decide whether a real
        approval survives a real night are both asserted:

          - the wait outlives the PROCESS, which is what a redeploy actually is; and
          - its deadline is `THREE_DAYS`, passed explicitly. `DBOS.recv` defaults to SIXTY
            SECONDS (docs/FIELD-NOTES.md, dbos 2.31.1), and a durable wait that quietly
            became a one-minute wait would pass every test on a fast machine and expire
            every approval given over a lunch break.

    THE SECOND PROCESS RE-RUNS NOTHING, AND THAT IS ASSERTED TOO
        `tool` and `publish` each appear exactly once across both processes. The recovered
        workflow replays `_step_start` and `_step_publish` out of the operation log instead
        of running the tool a second time and asking the same person the same question
        twice - which is how one action collects two conflicting approvals.
    """
    assert turn_workflow.THREE_DAYS >= 24 * 60 * 60, (
        f"the durable wait is {turn_workflow.THREE_DAYS}s, which is less than the 24 hours "
        "F3's criterion names. DBOS.recv defaults to 60 seconds; a wait shorter than a "
        "working day expires before the human it is waiting for reads the message."
    )

    _drop_databases()
    turn_id = TurnId(f"turn-{uuid4().hex}")
    tool_call_id = ToolCallId(_PROVIDER_TOOL_CALL_ID)

    with _Child("crash-approval", tmp_path, turn_id, tool_call_id) as crashing:
        crashing.wait_for_file(_READY, "the approval was published and the turn began waiting")
        crashing.kill()

    with _Child("recover-approval", tmp_path, turn_id, tool_call_id) as recovering:
        recovering.wait_for_file(_DONE, "the approved turn completed")
        recovering.wait_for_exit("the approved turn completed")

    recorded = _ledger(tmp_path)
    assert (tmp_path / _DONE).read_text(encoding="utf-8") == _RESUMED_ANSWER, (
        f"the turn did not resume on the approval (ledger: {recorded}). The decision was "
        "sent to the durable id the dead process's workflow ran under; if it reached "
        "nothing, the send addressed a workflow nobody created and the turn slept until it "
        "expired, with the human told their approval was accepted. docs/TASKS.md#t-f0-06"
    )
    assert recorded.count("resume") == 1, (
        f"the resume step ran {recorded.count('resume')} times (ledger: {recorded})."
    )
    assert recorded.count("tool") == 1, (
        f"the tool ran {recorded.count('tool')} times across the redeploy (ledger: "
        f"{recorded}). The suspended turn's work is already in the durable log; re-running "
        "it on recovery performs the action the human was still being asked about."
    )
    assert recorded.count(f"publish:{tool_call_id}") == 1, (
        f"the approval was published {recorded.count(f'publish:{tool_call_id}')} times "
        f"(ledger: {recorded}). Asking the same person the same question again after a "
        "restart is how one action collects two conflicting answers."
    )

    resolutions = json.loads((tmp_path / _RESOLUTIONS).read_text(encoding="utf-8"))
    assert [entry["tool_call_id"] for entry in resolutions] == [str(tool_call_id)], (
        f"the resumed turn was handed {resolutions}. The id a human approved must be the "
        "id the model issued."
    )
    assert [entry["approved"] for entry in resolutions] == [True], (
        f"the approval arrived as {resolutions}; a decision that loses its answer on the "
        "wire resumes the turn on something nobody said."
    )
    assert [entry["payload"] for entry in resolutions] == [_APPROVAL_NOTE], (
        f"the human's note was lost on the durable wire: {resolutions}. The note IS the "
        "tool result the model receives, so dropping it answers the agent with nothing."
    )


class _SuspendsOnOneApproval:
    """`StartTurn`, in-process: one suspension on a chosen `tool_call_id`, then finished.

    `runs` is counted under a lock because DBOS runs a dequeued workflow on its own
    executor thread, and a lost increment here would read as "the turn only started once"
    - a false green on the one property two of the tests below are about.
    """

    def __init__(self, tool_call_id: str) -> None:
        self._tool_call_id = tool_call_id
        self._lock = threading.Lock()
        self.runs = 0

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        with self._lock:
            self.runs += 1
        return TurnOutcome(turn_id=turn_id, pending=(_pending(self._tool_call_id),))


class _RecordingGateway:
    """`HumanGateway`, in-process, and the signal that the turn is now waiting.

    It counts ROUNDS, not just "has it asked yet". A turn may suspend again after being
    resumed, and the test below that drives a second round has to send the second decision
    only once the second question exists - otherwise it is racing the workflow rather than
    following it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.published: list[PendingRequest] = []
        self.rounds = 0

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        with self._lock:
            self.published.extend(requests)
            self.rounds += 1

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return None

    def asked_rounds(self) -> int:
        with self._lock:
            return self.rounds


class _RecordingResume:
    """`ResumeTurn`, in-process. Records what crossed the durable wire, unedited.

    `suspends_again_on` makes the FIRST resume return a second suspension, which is what
    puts the workflow back into `DBOS.recv_async`. That second wait is the only place a
    duplicate decision can actually be observed - a turn that finishes on its first resume
    never reads the durable topic again, so a duplicate sits unread and "resolved twice is
    a no-op" would be true of any implementation, idempotent or not.
    """

    def __init__(self, *, suspends_again_on: str | None = None) -> None:
        self._lock = threading.Lock()
        self._suspends_again_on = suspends_again_on
        self.calls: list[tuple[TurnId, tuple[ToolResolution, ...], CallerIdentity]] = []

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[ToolResolution, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        with self._lock:
            self.calls.append((turn_id, resolutions, caller))
            again = self._suspends_again_on if len(self.calls) == 1 else None
        if again is not None:
            return TurnOutcome(turn_id=turn_id, pending=(_pending(again),))
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=_RESUMED_ANSWER))

    def resolved_ids(self) -> list[str]:
        with self._lock:
            return [
                str(resolution.tool_call_id)
                for _, resolutions, _ in self.calls
                for resolution in resolutions
            ]


class _NowhereChannel:
    """A registered channel that records nothing and produces nothing."""

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        return None


def _approval_dependencies(
    started: _SuspendsOnOneApproval,
    asked: _RecordingGateway,
    resumed: _RecordingResume,
) -> turn_workflow.TurnWorkflowDependencies:
    return turn_workflow.TurnWorkflowDependencies(
        start_turn=cast("Any", started),
        channels=ChannelRegistry((("http", _NowhereChannel()),)),
        human_gateway=cast("HumanGateway", asked),
        resume_turn=cast("Any", resumed),
    )


async def _wait_for(flag: threading.Event, what: str) -> None:
    """Block the driving loop until `flag` is set. The deadline turns a hang into a sentence."""
    deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
    while not flag.is_set():
        assert time.monotonic() < deadline, (
            f"waited {_RENDEZVOUS_DEADLINE_SECONDS:.0f}s and {what} never happened."
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _wait_for_round(asked: _RecordingGateway, round_number: int, what: str) -> None:
    """Block the driving loop until the turn has asked a human `round_number` times."""
    deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
    while asked.asked_rounds() < round_number:
        assert time.monotonic() < deadline, (
            f"waited {_RENDEZVOUS_DEADLINE_SECONDS:.0f}s and {what} never happened."
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _drive_an_approval(
    turn_id: TurnId,
    tool_call_id: ToolCallId,
    asked: _RecordingGateway,
    *,
    decisions: int,
    then: ToolCallId | None = None,
) -> TurnOutcome:
    """Start a turn, wait until it is genuinely suspended, then resolve it `decisions` times.

    Every decision is sent AFTER the publish it answers is observed, so "the turn was
    waiting" is causal rather than timed - a decision sent before the workflow suspended
    would be proving something else entirely.

    `then` drives a SECOND round: the turn suspends again on that id, so the workflow
    re-enters the durable wait. That second wait is where a duplicate of the first decision
    would be read, which is the only place "resolving twice is a no-op" can be observed at
    all.
    """
    async with _Dbos():
        handle = await turn_workflow.enqueue_turn(
            _durability_request(str(turn_id)), turn_id=turn_id
        )
        await _wait_for_round(asked, 1, "the turn published its approval and began waiting")
        for _ in range(decisions):
            await turn_workflow.signal_decision(turn_id, tool_call_id, True, _APPROVAL_NOTE)
        if then is not None:
            await _wait_for_round(asked, 2, "the resumed turn suspended again and asked once more")
            await turn_workflow.signal_decision(turn_id, then, True, _APPROVAL_NOTE)
        return await handle.get_result()


@pytest.mark.phase("F3")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_tool_call_id_round_trips_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/TASKS.md#t-f3-12, second of three. CLAUDE.md non-negotiable #5.

    The id belongs to the PROVIDER. It is minted by the model, published to a human,
    carried across a durable boundary that serialises and deserialises it, and handed back
    to the runner - and at the far end it must be the same BYTES. A regenerated, re-cased
    or re-encoded id is dropped by Pydantic AI without an exception: the agent never sees
    its answer, asks the same question again, and the only symptom is a provider 400 much
    later or a conversation that loops.

    SO THE COMPARISON IS ON BYTES AND THE FIXTURE IS NOT TIDY. `_PROVIDER_TOOL_CALL_ID`
    carries mixed case, an underscore, a dash and a dot, because a lower-casing or
    character-stripping normaliser is invisible against an id that is all lowercase
    letters - which is what every hand-written `"call-1"` in this suite is.

    IT IS ASSERTED AT BOTH ENDS. The id a human is asked about and the id the runner is
    resumed with are two different hops through two different mechanisms; pinning only the
    second would miss a publish that renamed it.
    """
    started = _SuspendsOnOneApproval(_PROVIDER_TOOL_CALL_ID)
    asked = _RecordingGateway()
    resumed = _RecordingResume()
    monkeypatch.setattr(
        turn_workflow, "_dependencies", _approval_dependencies(started, asked, resumed)
    )

    turn_id = TurnId(f"turn-{uuid4().hex}")
    outcome = asyncio.run(
        _drive_an_approval(turn_id, ToolCallId(_PROVIDER_TOOL_CALL_ID), asked, decisions=1)
    )

    assert [str(request.tool_call_id) for request in asked.published] == [
        _PROVIDER_TOOL_CALL_ID
    ], (
        f"the human was asked about {[str(r.tool_call_id) for r in asked.published]}. The "
        "correlation a decision is routed back on is keyed by this id; a rewritten one "
        "resolves to nothing and the answer never reaches the turn."
    )
    assert resumed.calls, "the turn never resumed, so no id crossed the durable boundary."

    _, resolutions, _ = resumed.calls[0]
    round_tripped = str(resolutions[0].tool_call_id)
    assert round_tripped.encode("utf-8") == _PROVIDER_TOOL_CALL_ID.encode("utf-8"), (
        f"the tool_call_id came back as {round_tripped!r}, not {_PROVIDER_TOOL_CALL_ID!r}. "
        "Pydantic AI drops a resolution whose id does not match a call it issued, and it "
        "drops it SILENTLY - so the agent asks the same question forever and no test "
        "anywhere goes red. CLAUDE.md non-negotiable #5."
    )
    assert outcome.result is not None


@pytest.mark.phase("F3")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_resolving_the_same_request_twice_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/TASKS.md#t-f3-12, third of three.

    Humans double-click and retry logic re-posts. The second resolution of one
    `(turn_id, tool_call_id)` must not wake the turn a second time - a resumed turn runs
    the approved tool, and running it twice performs the action twice.

    THE GUARANTEE IS DBOS'S AND IT IS NOT REBUILT. `DBOS.send` takes an `idempotency_key`,
    and `signal_decision` keys it on `(turn_id, tool_call_id)` - the CALL, never the
    correlation handle, because a crashed publish can hand out a second handle for the same
    call. The second send collides on the notification table's unique index and does
    nothing (docs/FIELD-NOTES.md). An in-memory cache in this repository would be a third
    copy of that rule, and a lie the moment there are two processes.

    THE TURN HAS TO SUSPEND A SECOND TIME OR THIS TEST PROVES NOTHING, AND THAT IS NOT A
    COMPLICATION FOR ITS OWN SAKE
        A turn that finishes on its first resume never reads the durable topic again, so a
        duplicate decision simply sits in the notification table unread - and "resolving
        twice is a no-op" then holds against ANY implementation, idempotent or not. Removing
        `signal_decision`'s `idempotency_key` was tried against exactly that shape and the
        test stayed green, which is the definition of a test without teeth.

        So the resume suspends AGAIN, on a second call, and the workflow re-enters
        `DBOS.recv_async`. That second wait is the one moment a duplicate can be observed:
        without the idempotency key the stale copy of the FIRST decision is what the second
        wait reads, the turn resolves the same call twice, and the second question - the one
        a human is now standing in front of - is never answered at all.

    THE ASSERTION IS ON WHICH IDS WERE RESOLVED, NOT ON A COUNT. Both decisions say
    "approved", so counting resumes or comparing answers would pass against a turn that
    answered the first question twice and the second one never.
    """
    second_call = ToolCallId("call_2nd-Bq.7Xm")
    started = _SuspendsOnOneApproval(_PROVIDER_TOOL_CALL_ID)
    asked = _RecordingGateway()
    resumed = _RecordingResume(suspends_again_on=str(second_call))
    monkeypatch.setattr(
        turn_workflow, "_dependencies", _approval_dependencies(started, asked, resumed)
    )

    turn_id = TurnId(f"turn-{uuid4().hex}")
    outcome = asyncio.run(
        _drive_an_approval(
            turn_id,
            ToolCallId(_PROVIDER_TOOL_CALL_ID),
            asked,
            decisions=2,
            then=second_call,
        )
    )

    assert resumed.resolved_ids() == [_PROVIDER_TOOL_CALL_ID, str(second_call)], (
        f"the turn resolved {resumed.resolved_ids()}. The first decision was sent twice and "
        "the duplicate must be a no-op: seeing it twice means the approved call ran again "
        "and the second question - which a human had already answered - was dropped on the "
        "floor. `DBOS.send`'s idempotency_key is what prevents it; docs/FIELD-NOTES.md."
    )
    assert started.runs == 1, (
        f"the turn started {started.runs} times. A second resolution must not start a "
        "second turn either."
    )
    assert outcome.result is not None and outcome.result.text == _RESUMED_ANSWER


# ---------------------------------------------------------------------------
# t-f2-07, t-f2-08, t-f2-09 - the three coalescing and serialisation criteria
#
# The MECHANISMS these assert are also driven from tests/integration/test_coalescing.py,
# which owns t-f2-03, t-f2-05 and t-f2-11 - the anchors that BUILT them. These are the
# acceptance criteria stated as F2's own done-when conditions, and two of them say
# something that file does not: t-f2-07 counts MODEL CALLS rather than turns, which needs
# the real `StartTurn` and a runner that can be counted, and t-f2-08 pins the parallel and
# the serial half in one run.
# ---------------------------------------------------------------------------

_PROFILE = AgentProfile(id="p-1", persona="You handle accounts.", model="m")

_RULES = (
    PolicyRule(
        rule_id="r-freeze",
        tool_pattern="freeze_*",
        effect=Effect.ALLOW,
        reason="Freezing is reversible.",
    ),
)


class _NoCompaction:
    """`ContextEngine` that accounts and never compresses. F5 owns the real one."""

    def on_session_start(self, session: SessionRef) -> None:
        return None

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        return None

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        return False

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        raise NotImplementedError("not reachable: should_compress is always False")

    def on_session_end(self, session: SessionRef) -> None:
        return None


class _NoSkills:
    """`SkillRegistry` with nothing in it. F6 owns the real one."""

    async def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        return ()

    async def read(self, name: str) -> str:
        raise NotImplementedError("not reachable: nothing is indexed")


class _Buffer:
    """The pending-input buffer, as both halves of the coalescing contract see it.

    Read-and-remove in ONE critical section, exactly as `PgPendingInputBuffer.drain` does
    it in a single `DELETE ... RETURNING`: a read followed by a separate removal can hand
    one sentence to two turns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.appended: list[tuple[SessionRef, UserInput]] = []

    async def append(self, session: SessionRef, message: UserInput) -> None:
        with self._lock:
            self.appended.append((session, message))

    async def drain(self, session: SessionRef) -> tuple[UserInput, ...]:
        with self._lock:
            mine = tuple(
                message for buffered, message in self.appended if buffered == session
            )
            self.appended = [pair for pair in self.appended if pair[0] != session]
        return mine


async def _await_turn_behind(turn_id: str) -> None:
    """Wait for the turn `turn_id` names, causally and never by clock.

    Two hops: the window workflow deliberately does NOT await the turn (holding its
    deduplication id for the length of the turn is what would make a mid-turn message
    vanish), so the window has to close and enqueue first, and only the turn's own handle
    completes when the turn has actually run.
    """
    from dbos import DBOS, WorkflowHandleAsync

    window: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(
        turn_workflow.window_id_for_turn(TurnId(turn_id))
    )
    enqueued = str(await window.get_result())
    assert enqueued == turn_id, (
        f"the window enqueued turn {enqueued}, not {turn_id}. The two ids are derived from "
        "each other (docs/TASKS.md#t-f0-06); if they can disagree, the id the caller was "
        "answered with is filed under nothing."
    )
    turn: WorkflowHandleAsync[TurnOutcome] = await DBOS.retrieve_workflow_async(enqueued)
    await turn.get_result()


async def _drive_coalesced_messages(
    starter: routes.TurnStarter, requests: tuple[TurnRequest, ...]
) -> list[str]:
    async with _Dbos():
        ids = [str((await starter(request)).turn_id) for request in requests]
        await _await_turn_behind(ids[0])
        return ids


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_three_messages_in_the_window_produce_one_turn_and_one_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-07, and it is the MODEL CALL half that needs saying.

    One turn is the observable fact; one model call is the reason D19 asks for it. Three
    sentences answered as three turns cost three model calls to produce one useful answer,
    and answer the first two before the user has finished asking. Counting turns cannot
    tell those apart from a turn that restarted, so this drives the REAL `StartTurn` over
    a runner whose invocations are counted: `FakeAgentRunner.run_calls` is the model.

    AND THE SAVING IS A LOSS UNLESS ALL THREE SENTENCES REACH THAT ONE CALL.
    `duplication_policy="return-existing"` keeps the FIRST message's arguments and
    discards the rest, so the other two live in the buffer until `_step_drain_pending`
    folds them back in - behind the first, in arrival order, once each. A drain that
    replaced the input would lose the first sentence and still show one model call.
    """
    request = _durability_request("s-1")
    runner = FakeAgentRunner(
        TurnOutcome(turn_id=TurnId("replaced-by-the-workflow"), result=TurnResult(text=_ANSWER))
    )
    use_case = StartTurn(
        runner=runner,
        tools=FakeToolProvider(toolset=object(), tool_names=("freeze_account",)),
        policy=FakeToolPolicy(RuleSet.for_caller(request.caller, _RULES)),
        store=FakeConversationStore(),
        audit=FakeAuditSink(),
        context=_NoCompaction(),
        skills=_NoSkills(),
        profiles={_PROFILE.id: _PROFILE},
    )
    buffer = _Buffer()
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=use_case,
            channels=ChannelRegistry((("http", _NowhereChannel()),)),
            pending_inputs=buffer,
        ),
    )

    session = f"s-{uuid4().hex[:12]}"
    sentences = ("hola", "queria preguntar algo", "sobre mi pedido 123")
    starter = routes.coalescing_turn_starter(
        buffer=cast("Any", buffer), window_seconds=_WIDE_WINDOW_SECONDS
    )
    requests = tuple(_durability_request(session, text=sentence) for sentence in sentences)

    ids = asyncio.run(_drive_coalesced_messages(starter, requests))

    assert len(set(ids)) == 1, (
        f"three messages sent inside a {_WIDE_WINDOW_SECONDS:.0f}s window opened "
        f"{len(set(ids))} windows ({sorted(set(ids))}). Either coalescing is not happening, "
        "or this machine took longer than the window to run three local enqueues - the "
        "second is a stalled machine, not a defect."
    )
    assert len(runner.run_calls) == 1, (
        f"three coalesced messages produced {len(runner.run_calls)} model calls. One turn "
        "is one model call; three is three, for one answer, and the first two answer a "
        "question the user had not finished asking. D19, docs/TASKS.md#t-f2-07"
    )

    _, ran, _, _ = runner.run_calls[0]
    assert ran.input.text.split("\n") == list(sentences), (
        f"the one model call saw {ran.input.text!r}. The first sentence travels in the "
        "enqueued request and the other two are drained from the buffer behind it, in "
        "arrival order and once each - a drain that REPLACED the input would lose the "
        "first, and one that repeated it would answer the same sentence twice."
    )
    assert buffer.appended == [], (
        f"{len(buffer.appended)} messages are still buffered after the turn ran; the next "
        "turn on this session would answer them a second time."
    )


class _SessionOccupancy:
    """Stands in for `StartTurn` and records who was inside which partition, when.

    NOTHING HERE IS TIMED, AND THAT IS DELIBERATE - the first version of this property
    elsewhere in the suite slept inside each turn and compared spans, and failed under
    load because `dbos.Queue` polls once a second, so two enqueues straddling a tick are
    dequeued a second apart. Widening the tolerance only moves the load at which it lies.

    Parallelism is a RENDEZVOUS: every turn blocks until `partitions_expected` distinct
    partitions are inside at the same moment, so it can never be reached by luck and being
    slow does not make it fail. Serialisation is an OCCUPANCY COUNT taken on entry, so a
    second turn of one partition entering while the first is still inside is recorded
    exactly, whatever the clock says.
    """

    def __init__(self, *, partitions_expected: int) -> None:
        self._partitions_expected = partitions_expected
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}
        self._released = False
        self.ran: list[str] = []
        self.overlapped: list[str] = []
        self.timed_out = False

    def _enter(self, partition: str) -> None:
        with self._lock:
            depth = self._in_flight.get(partition, 0) + 1
            self._in_flight[partition] = depth
            if depth > 1:
                self.overlapped.append(partition)
            if sum(1 for count in self._in_flight.values() if count > 0) >= (
                self._partitions_expected
            ):
                self._released = True

    def _leave(self, partition: str) -> None:
        with self._lock:
            self._in_flight[partition] -= 1
            self.ran.append(partition)

    def _may_go(self) -> bool:
        with self._lock:
            return self._released

    def _give_up(self) -> None:
        # Latched, and it releases everybody: one turn waiting out the deadline is a
        # readable failure, three of them in series is a hung suite.
        with self._lock:
            self.timed_out = True
            self._released = True

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        partition = turn_workflow.session_partition_key(request.session)
        self._enter(partition)
        try:
            deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
            while not self._may_go():
                if time.monotonic() >= deadline:
                    self._give_up()
                    break
                await asyncio.sleep(_POLL_SECONDS)
        finally:
            self._leave(partition)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=_ANSWER))


async def _drive_partitions(requests: tuple[TurnRequest, ...]) -> None:
    async with _Dbos():
        handles = [await turn_workflow.enqueue_turn(request) for request in requests]
        for handle in handles:
            await handle.get_result()


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_two_sessions_run_in_parallel_while_two_messages_on_one_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-08, both halves in one run, because each is the other's trap.

    Serialising a session is the point: two turns of one conversation read and write the
    same history, so the second starts from a transcript the first has not finished
    writing. Serialising EVERYTHING is the same fix applied one level too wide - every
    tenant then waits behind every other tenant's slowest turn - and it looks perfectly
    healthy under any single-session test.

    So both are asserted against one dispatch: three messages, two of them on one session.
    A queue that ran everything in single file would satisfy the serial half and never
    reach the rendezvous; a queue with no partitioning would reach the rendezvous and
    record an overlap.
    """
    held = _SessionOccupancy(partitions_expected=2)
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", held),
            channels=ChannelRegistry((("http", _NowhereChannel()),)),
        ),
    )

    tenant = f"t-{uuid4().hex[:12]}"
    # A literal tuple and never a set: CLAUDE.md non-negotiable #7 applies to what a test
    # dispatches as much as to what a workflow does.
    requests = (
        _durability_request("s-1", tenant=tenant, text="one"),
        _durability_request("s-1", tenant=tenant, text="two"),
        _durability_request("s-2", tenant=tenant, text="three"),
    )

    asyncio.run(_drive_partitions(requests))

    assert len(held.ran) == len(requests), (
        f"{len(held.ran)} of {len(requests)} enqueued turns ran. A turn that never leaves "
        "the queue is not serialisation, it is a deadlock."
    )
    assert held.overlapped == [], (
        f"two turns of partition(s) {sorted(set(held.overlapped))} were inside the session "
        "at the same time. One session runs one turn at a time, or the second turn reads a "
        "history the first has not finished writing."
    )
    assert not held.timed_out, (
        f"no moment existed at which two different sessions were running together, after "
        f"waiting {_RENDEZVOUS_DEADLINE_SECONDS:.0f}s for one. They are different "
        "conversations, and serialising them makes every tenant wait behind every other "
        "tenant's slowest turn - which nothing in a single-session test would show."
    )


class _BlockingStartTurn:
    """Holds the FIRST turn inside the session slot until it is let out.

    The mid-turn test needs a moment that provably IS mid-turn. Sleeping and hoping is the
    mistake this file's header is about, so the turn SIGNALS that it is inside and then
    waits: the second message is sent between those two events, which makes "mid-turn"
    causal instead of timed.

    Cross-thread primitives, not asyncio ones: DBOS runs a dequeued workflow on its own
    executor, so the event the test sets is not on the loop the workflow runs on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started: list[TurnRequest] = []
        self.inside = threading.Event()
        self.release = threading.Event()
        self.timed_out = False

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        with self._lock:
            self.started.append(request)
            hold = len(self.started) == 1
        if hold:
            self.inside.set()
            deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
            while not self.release.is_set():
                if time.monotonic() >= deadline:
                    self.timed_out = True
                    break
                await asyncio.sleep(_POLL_SECONDS)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=_ANSWER))


async def _drive_a_mid_turn_message(
    starter: routes.TurnStarter,
    held: _BlockingStartTurn,
    first: TurnRequest,
    second: TurnRequest,
) -> tuple[str, str]:
    async with _Dbos():
        opened = str((await starter(first)).turn_id)

        # The window has to have CLOSED and the turn have started before the second
        # message is sent, or this would be testing coalescing again.
        await _wait_for(held.inside, "the first turn reached _step_start")

        follow_up = str((await starter(second)).turn_id)

        held.release.set()
        await _await_turn_behind(opened)
        await _await_turn_behind(follow_up)
        return opened, follow_up


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_message_arriving_mid_turn_becomes_a_follow_up_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-09. Coalescing is bounded by the WINDOW, not by the turn.

    A message that arrives while the turn is already running has missed the window, and it
    must open a new one: the partitioned queue then serialises the follow-up turn behind
    the running one, which is D19's "collect" mode and needs no extra code.

    THE FAILURE THIS FORBIDS IS SILENT AND IS ONE LINE AWAY. A deduplication id is released
    when its workflow COMPLETES, so a window workflow that awaited its turn would hold the
    key for the length of the turn - and this message would then join a window whose turn
    has already drained the buffer. Nothing raises. The sentence sits in a table and the
    user is never answered.
    """
    held = _BlockingStartTurn()
    buffer = _Buffer()
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", held),
            channels=ChannelRegistry((("http", _NowhereChannel()),)),
            pending_inputs=buffer,
        ),
    )
    starter = routes.coalescing_turn_starter(
        buffer=cast("Any", buffer), window_seconds=_NARROW_WINDOW_SECONDS
    )

    session = f"s-{uuid4().hex[:12]}"
    first = _durability_request(session, text="freeze it")
    second = _durability_request(session, text="actually, wait")

    opened, follow_up = asyncio.run(_drive_a_mid_turn_message(starter, held, first, second))

    assert not held.timed_out, (
        "the first turn was never released; it waited out the deadline instead, so nothing "
        "below is about the property this test is named for."
    )
    assert follow_up != opened, (
        "a message that arrived while the turn was RUNNING was handed the running turn's "
        "window back. Its window had already closed, so the sentence joins a turn that has "
        "already read the buffer and is never answered - the exact vanishing D19 calls out."
    )
    assert [request.input.text for request in held.started] == ["freeze it", "actually, wait"], (
        f"the turns ran on {[request.input.text for request in held.started]}. Expected two "
        "turns: the first carrying the first message and the follow-up carrying the "
        "mid-turn one. One turn means the second message vanished; a first turn carrying "
        "both means the window did not close when the turn started."
    )


# ---------------------------------------------------------------------------
# t-f7-09 - the F7 criterion
# ---------------------------------------------------------------------------


@pytest.mark.phase("F7")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_an_evidence_request_resumes_with_the_uploaded_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f7-09. The agent asks for a photo and the turn resumes holding it.

    The whole F7 round trip, durably: a tool defers asking for evidence, the turn suspends
    and a person is asked, the file arrives from somewhere else entirely - another device,
    minutes or hours later - and the deferred call is resolved WITH that image rather than
    with a description of it.

    EXACTLY HOW FAR THIS REACHES, SAID PLAINLY RATHER THAN IMPLIED
        It ends where the durable boundary ends: at the resolution `_step_resume` hands the
        application layer. `ResumeTurn` step 3 is what then turns a `MediaRef` into bytes or
        a signed URL for the provider, and `tests/unit/test_resume_turn.py` pins that half -
        duplicating it here would need a media store and would prove it about a fake.

        TWO THINGS WERE UNWIRED ABOVE IT AND BOTH NOW EXIST, WHICH IS WHY THIS TEST
        CHANGED SHAPE RATHER THAN STAYING AS IT WAS:

          1. `POST /evidence/{corr_id}` validated and stored and then stopped. t-f7-11 gave
             it the `signal_evidence` seat, and `composition._pg_evidence_signal` binds it
             to the same function this test calls - so the driver below now stands exactly
             where the route stands instead of standing in for it.
          2. `HumanAnswer.note` was declared `str | None`, and an image is not a string, so
             this test compiled only through a `cast`. The field is `HumanPayload`
             (`str | MediaRef | None`) now and the cast is gone - see
             `_drive_an_evidence_upload`.
    """
    started = _SuspendsOnEvidence(_PROVIDER_TOOL_CALL_ID)
    asked = _RecordingGateway()
    resumed = _RecordingResume()
    monkeypatch.setattr(
        turn_workflow, "_dependencies", _approval_dependencies(cast("Any", started), asked, resumed)
    )

    uploaded = MediaRef(
        media_id=MediaId("sha256:1f0c"),
        kind=MediaKind.IMAGE,
        mime_type="image/jpeg",
        size_bytes=48_122,
        sha256="1f0c",
        filename="parcel.jpg",
    )
    turn_id = TurnId(f"turn-{uuid4().hex}")
    outcome = asyncio.run(
        _drive_an_evidence_upload(turn_id, ToolCallId(_PROVIDER_TOOL_CALL_ID), asked, uploaded)
    )

    assert [request.kind for request in asked.published] == [PendingKind.EVIDENCE], (
        f"the human was asked {[r.kind for r in asked.published]}. An evidence request is "
        "human-answerable and must reach a person, or nobody ever uploads anything."
    )
    assert resumed.calls, (
        "the turn never resumed on the upload. The image was stored and the turn is still "
        "waiting - which from the user's side is a system that asked for a photo, was given "
        "one, and went silent. docs/TASKS.md#t-f7-09"
    )

    _, resolutions, _ = resumed.calls[0]
    assert [str(resolution.tool_call_id) for resolution in resolutions] == [
        _PROVIDER_TOOL_CALL_ID
    ], "the deferred call the image answers must be the call the model issued."
    assert [resolution.payload for resolution in resolutions] == [uploaded], (
        f"the turn resumed with {[r.payload for r in resolutions]} instead of the uploaded "
        "image. `ResumeTurn` turns a MediaRef into the bytes the model sees; a payload that "
        "is not one arrives at the provider as a description of a photo rather than a photo."
    )
    assert outcome.result is not None


class _SuspendsOnEvidence:
    """`StartTurn`, in-process: one suspension asking a human for a photo."""

    def __init__(self, tool_call_id: str) -> None:
        self._tool_call_id = tool_call_id
        self._lock = threading.Lock()
        self.runs = 0

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        with self._lock:
            self.runs += 1
        return TurnOutcome(
            turn_id=turn_id,
            pending=(
                PendingRequest(
                    kind=PendingKind.EVIDENCE,
                    tool_call_id=ToolCallId(self._tool_call_id),
                    tool_name="request_evidence",
                    arguments={"kind": "photo"},
                    reason="a photo of the parcel as it arrived",
                ),
            ),
        )


async def _drive_an_evidence_upload(
    turn_id: TurnId,
    tool_call_id: ToolCallId,
    asked: _RecordingGateway,
    uploaded: MediaRef,
) -> TurnOutcome:
    """Start a turn that asks for a photo, then deliver the stored image to it.

    `signal_evidence` and not `signal_decision`, and the difference is the point:
    `signal_decision` is the APPROVAL door - it takes `approved` and a `note` and nothing
    else. An evidence upload is the same durable mechanism (D9: approval and evidence are
    one flow) addressed at the same topic, with a stored reference instead of a yes.

    THE `cast` IS GONE, AND ITS ABSENCE IS THE ASSERTION
        This used to call `DBOS.send_async` by hand with `note=cast("Any", uploaded)`,
        because `HumanAnswer.note` was declared `str | None` and a `MediaRef` did not fit
        it - so the transport carried the image while no type-checked production caller
        could write the line. t-f7-11 widened the field to `HumanPayload`
        (`str | MediaRef | None`) and gave the send its own door, so this test now drives
        the SAME function `composition._pg_evidence_signal` binds behind
        `POST /evidence/{corr_id}`. The `cast` was the marker for an unwired seat; keeping
        it after the seat was wired would go on proving the property about a hand-rolled
        send that production does not use.
    """
    async with _Dbos():
        handle = await turn_workflow.enqueue_turn(
            _durability_request(str(turn_id)), turn_id=turn_id
        )
        await _wait_for_round(asked, 1, "the turn asked for evidence and began waiting")
        await turn_workflow.signal_evidence(turn_id, tool_call_id, uploaded)
        return await handle.get_result()


if __name__ == "__main__":  # pragma: no cover - the child process's entry point
    # Reached only by `_Child`, which spawns this module as a script precisely so the
    # process can be killed without a handler. Four arguments, no parsing library: a
    # crash test whose child needs configuring is a crash test nobody can read.
    asyncio.run(
        _run_child(
            sys.argv[1], Path(sys.argv[2]), TurnId(sys.argv[3]), ToolCallId(sys.argv[4])
        )
    )
