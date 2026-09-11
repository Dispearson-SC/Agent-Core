"""The door: a human types at the console and the peer loop actually runs.

Phase:   F12 - agent-to-agent orchestration
Tasks:   docs/TASKS.md#t-f11-51
Covers:  adapters/driving/cli/console.py (`:mode`, the durable turn, both banners),
         agent_core/main.py (build_console's durable seats, run_console's engine),
         agent_core/composition.py (the `cli` channel),
         Core/policy/rules.yaml (the scope of the `ask_peer` grant)

WHY THIS FILE EXISTS WHEN test_peer_end_to_end.py IS GREEN

    That file proves the loop: the ask leaves, a worker runs the answering agent under its
    own identity, the answer resumes the waiting turn under the provider's original
    `tool_call_id`, and both allowlists hold. It proves it by calling
    `turn_workflow.enqueue_turn` itself - so what it certifies is the MACHINERY, and the
    machinery had no door. `_step_ask_peers` lives in the DBOS workflow; the console called
    `StartTurn` directly, so an `ask_peer` typed at the console suspended and no mailbox was
    ever asked. `POST /turns` does go through the workflow and arrives on channel `http`,
    where the grant scoped to `cli` does not match. Two correct decisions that did not
    compose - the tenth instance in this build of a mechanism that works with no way in.

    So this file refuses to reach past the console. It drives `read_line` and `write_line`,
    which is what a terminal drives, and every fact it asserts is read off the transcript an
    operator would have been looking at.

THE CONSOLE KEEPS BOTH PATHS, AND THE POINT IS THAT IT SAYS WHICH ONE IS ON

    Direct mode is why a REPL answers immediately and it is not a defect to be removed. The
    console starts in it, `:mode durable` switches, `:mode direct` switches back, the prompt
    carries the active mode on every line, and each switch reprints what that mode does and
    does not exercise. A console that silently exercises less than the operator thinks is
    the failure this whole phase is made of, so the banner is asserted in BOTH modes below.

HOW THE PEER WORKER RUNS HERE, AND WHAT AN OPERATOR RUNS IN PRODUCTION

    A durable console turn parks on `DBOS.recv_async` until somebody answers the ask it put
    on the mailbox. Nothing in the console process claims that ask. In PRODUCTION the
    operator starts a SECOND process beside the console:

        python -m agent_core peer-worker --agent billing_specialist

    and the console is started with the durable path and the role the shipped grant names:

        python -m agent_core console --durable --profile support_triage --role operator

    `Core/policy/rules.yaml` scopes `peers-ask-from-the-operator-console` to
    `subject_roles: [operator]` on `channels: [cli]`, and an absent role does not widen a
    grant - it fails to match it. A console started without `--role operator` therefore gets
    `ask_peer` refused, which is the rule working; `:policy ask_peer` says so at the prompt.

    HERE the worker is `run_peer_worker` - the same production loop, with the `keep_going`
    and `write_line` seams it declares - driven as an asyncio task beside `Console.run()` in
    this one process, so the test needs one process instead of two. Nothing about the loop is
    replaced: the queue is `container.mailbox`, the runner is `DirectTurnRunner`, and `wake`
    is `turn_workflow.signal_peer_answer`. The console's turn does not know or care which
    process answered it.

WHAT IS FAKED, AND WHAT THAT DOES NOT PROVE

    THE MODEL, for both agents, exactly as `test_peer_end_to_end.py` fakes it and for the
    same reason: a live call here would buy a second opinion about a provider and nothing
    about whether a typed question reaches the mailbox. Everything else is production's - a
    real PostgreSQL, real migrations, the shipped `Core/policy/rules.yaml` reconciled at
    startup, the real `PgToolPolicy`, the real `PgAgentMailbox`, a real DBOS workflow with a
    real `recv_async`/`send_async` round trip, the shipped profiles, and `main.build_console`
    wiring every seat from the ONE container.

    NOTHING FAKES THE `cli` CHANNEL, and that is deliberate. `test_peer_end_to_end.py` adds a
    recording channel under that id because `build_channel_registry` had no entry for it; a
    console-driven turn that needed the same favour would be this file supplying the
    collaborator production forgot - the shape docs/STATE.md has now recorded nine times.
    `_step_deliver` is the workflow's last step, so a missing `cli` entry kills the turn
    after the model has been paid for, and the run below simply would not finish.

    A PLACEHOLDER `MINIMAX_API_KEY`. The preflight asks whether the credential VARIABLE is
    set, never what it holds, and this run never reaches a provider. No credential value is
    read, printed or asserted on anywhere in this file.

SKIP GUARD
    Skips when no PostgreSQL instance is reachable. CI without one must not fail.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core import composition
from agent_core import main as main_module
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driving.peers import worker as peer_worker
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.adapters.driving.workflow.bootstrap import launch_dbos
from agent_core.composition import Container
from agent_core.domain.peers import AgentId
from agent_core.domain.turn import ToolCallId

pytestmark = [pytest.mark.phase("F12")]

# The two shipped profiles that are a real peer pair.
_ASKER = "support_triage"
_PEER = "billing_specialist"

# What the console attributes a turn to. Spelt rather than imported, so this run and
# `Core/policy/rules.yaml` agree by AGREEMENT and not by construction - a test that imported
# the scope could not notice the scope moving.
_OPERATOR_CHANNEL = "cli"
_OPERATOR_ROLE = "operator"

_LEDGER_TOOL = "account_history"
_ACCOUNT = "acc-1"

# The PROVIDER's id for the asker's deferred call - mixed case, an underscore, a hyphen and
# a dot, so a `.strip()`, a `.lower()` or a fresh uuid anywhere on the path shows up as a
# dropped result rather than as an exception (CLAUDE.md non-negotiable #5).
_PROVIDER_TOOL_CALL_ID = ToolCallId("call_C0ns-Ole.d_9xZ")

_QUESTION = "Was account acc-1 charged twice in March?"
_PEER_ANSWER = "The March pair is one charge and one authorisation hold."
_A_FINAL = "Billing checked the ledger: the second line is a hold, not a charge."

# What the operator types. `:mode` with no argument first, so the transcript carries the
# console's answer to "which half of the system am I judging?" before anything is spent.
_TYPED_QUESTION = "was this customer charged twice in March?"
_SCRIPT: tuple[str, ...] = (
    ":mode",
    _TYPED_QUESTION,
    ":audit",
    ":mode direct",
    ":mode durable",
    ":quit",
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# This file's own application database, for the reason every launch helper in this directory
# gives: DBOS derives its system database from it, and a run that failed here must be
# destroyable without touching state another file owns.
_APP_DATABASE = "agent_core_console_durable"

_CREDENTIAL_VARIABLE = "MINIMAX_API_KEY"
_CREDENTIAL_PLACEHOLDER = "placeholder-this-run-calls-no-provider"

_SESSION_DEADLINE_SECONDS = 180.0
_POLL_SECONDS = 0.05


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _conninfo(database: str) -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{database}"


def _drop_databases() -> None:
    """Destroy this file's state before the run. NOT tidiness.

    `agent-core-turns` is partitioned with `partition_concurrency=1`, so one PENDING row
    left by an earlier run holds its session's only slot and every later turn for that
    session is enqueued and never dequeued, with nothing raising. On a file whose subject is
    a turn that parks and wakes, that reads as the property under test passing.
    """
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_APP_DATABASE}_dbos_sys", f"{_APP_DATABASE}_dbos", _APP_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _TwoAgents:
    """One `FunctionModel` body serving both turns, told apart by the TOOLS on offer.

    Both profiles name the same model id, so the factory cannot tell them apart and neither
    can this function - except by what it was handed. `account_history` is in
    `billing_specialist`'s resolved toolset and in nothing else, which makes the
    discriminator a fact about the profile under test rather than a flag this file sets.
    """

    __name__ = "console_durable_model"

    def __init__(self) -> None:
        self.asker_requests: list[list[ModelMessage]] = []
        self.peer_requests: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered = {tool.name for tool in info.function_tools}
        if _LEDGER_TOOL in offered:
            self.peer_requests.append(list(messages))
            return self._peer(len(self.peer_requests))
        self.asker_requests.append(list(messages))
        return self._asker(len(self.asker_requests))

    def _asker(self, call: int) -> ModelResponse:
        if call == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="ask_peer",
                        args={"target": _PEER, "question": _QUESTION},
                        tool_call_id=str(_PROVIDER_TOOL_CALL_ID),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(_A_FINAL)])

    def _peer(self, call: int) -> ModelResponse:
        if call == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=_LEDGER_TOOL,
                        args={"account_id": _ACCOUNT},
                        tool_call_id="call_billing_reads_the_ledger",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(_PEER_ANSWER)])


def _install_scripted_model(model: _TwoAgents, patch: pytest.MonkeyPatch) -> None:
    """Hand every runner this process builds a `FunctionModel` instead of a provider.

    `PydanticAgentRunner`'s `model_factory` is a constructor seam that exists for exactly
    this, and `composition.py` leaves it at the default. Patching the name composition
    resolves means `build_container` stays the real one: every other adapter, the policy
    engine included, is production's.
    """
    real = runner_module.PydanticAgentRunner

    def factory(model_id: str, base_url: str | None) -> Model:
        del model_id, base_url  # both profiles name one model; see `_TwoAgents`
        return FunctionModel(model)

    def build(**kwargs: Any) -> runner_module.PydanticAgentRunner:
        return real(**kwargs, model_factory=factory)

    patch.setattr(composition, "PydanticAgentRunner", build)


class _Keyboard:
    """The operator's keystrokes, and the echo a terminal would have shown.

    It stands in for the console's injected `read_line`, so the console is driven through
    exactly the seam a piped script uses. The PROMPT is echoed onto the same transcript as
    the console's own output, which is how the mode indicator on the prompt becomes an
    asserted fact rather than a claim: an operator reads it on every line they type.
    """

    def __init__(self, lines: list[str], commands: Sequence[str]) -> None:
        self._lines = lines
        self._commands = tuple(commands)
        self._index = 0
        self.prompts: list[str] = []
        self.marks: list[tuple[str, int]] = []

    def __call__(self, prompt: str) -> str | None:
        if self._index >= len(self._commands):
            return None
        command = self._commands[self._index]
        self._index += 1
        self.prompts.append(prompt)
        self._lines.append(f"{prompt}{command}")
        # The index just past the echo, so a test can ask for one command's output
        # without parsing prose - the console prints `->` itself, so a slice that
        # looked for the next prompt-shaped line would cut a tool call in half.
        self.marks.append((command, len(self._lines)))
        return command


@dataclass(frozen=True, slots=True)
class _Session:
    """One whole console session, sliceable by the command that produced each part."""

    lines: tuple[str, ...]
    prompts: tuple[str, ...]
    marks: tuple[tuple[str, int], ...]
    model: _TwoAgents
    container: Container

    def after(self, command: str) -> tuple[str, ...]:
        """Everything the console printed in answer to one command."""
        positions = [index for name, index in self.marks if name == command]
        assert positions, (
            f"{command!r} is not in the script, so nothing can be asserted about it. "
            f"Scripted: {[name for name, _ in self.marks]}"
        )
        start = positions[0]
        later = [index for _, index in self.marks if index > start]
        # -1 drops the next echo line itself, which the keyboard printed.
        end = min(later) - 1 if later else len(self.lines)
        return self.lines[start : max(start, end)]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def banner(self) -> str:
        """Everything printed before the first prompt."""
        return "\n".join(self.lines[: self.marks[0][1] - 1]) if self.marks else self.text


async def _drain_the_peer_queue(container: Container, stop: list[str]) -> None:
    """The production worker loop, with the seams it declares, beside the console.

    `container.mailbox` and `DirectTurnRunner(container.start_turn)` are the two seats
    `main.run_peer_worker_process` builds, and `wake` is the `signal_peer_answer` it binds.
    `keep_going` and `write_line` are the seams `run_peer_worker` declares so the loop can
    stop after one ask instead of polling forever - see this module's docstring for the two
    commands an operator runs instead.
    """
    queue = container.mailbox
    assert isinstance(queue, peer_worker.PeerQueue), (
        "the container's mailbox cannot be claimed from, so there is no queue to drain - "
        "the same refusal `main.run_peer_worker_process` makes at startup."
    )
    await peer_worker.run_peer_worker(
        queue,
        peer_worker.DirectTurnRunner(container.start_turn),
        targets=(AgentId(_PEER),),
        wake=turn_workflow.signal_peer_answer,
        idle_seconds=_POLL_SECONDS,
        keep_going=lambda: not stop,
        write_line=stop.append,
    )


@pytest.fixture(scope="module")
def console_session() -> Iterator[_Session]:
    """ONE console session, from an empty instance to a finished delegated turn.

    Module scoped because it bootstraps a database and drives a DBOS workflow: the clauses
    below are facts about one run an operator sat in front of, not independent checks.

    `monkeypatch` is function scoped and cannot be used here, so a module-scoped
    `MonkeyPatch` is created by hand and undone in the `finally`.
    """
    from dbos import DBOS

    patch = pytest.MonkeyPatch()
    app_conninfo = _conninfo(_APP_DATABASE)
    overrides = {
        "AGENT_CORE_DATABASE_URL": app_conninfo,
        "AGENT_CORE_AUDIT_DATABASE_URL": app_conninfo,
        "AGENT_CORE_ADMIN_DATABASE_URL": _ADMIN_CONNINFO,
        _CREDENTIAL_VARIABLE: _CREDENTIAL_PLACEHOLDER,
    }

    model = _TwoAgents()
    lines: list[str] = []
    container: Container | None = None
    launched = False
    try:
        for name, value in overrides.items():
            patch.setenv(name, value)
        _drop_databases()
        _install_scripted_model(model, patch)

        container = asyncio.run(composition.start_container())
        container.domain_pool.open()
        container.audit_pool.open()
        launch_dbos(container)
        launched = True

        keyboard = _Keyboard(lines, _SCRIPT)
        console = main_module.build_console(
            container,
            profile_id=_ASKER,
            subject_id=_OPERATOR_ROLE,
            roles=frozenset({_OPERATOR_ROLE}),
            # The flag half of `python -m agent_core console --durable`. The OTHER half is
            # the durable engine, which this fixture launched above exactly as
            # `run_console` launches it - see `test_the_shipped_console_command_launches_
            # the_durable_engine` for the production command making both decisions at once.
            durable=True,
            write_line=lines.append,
            read_line=keyboard,
        )

        async def drive() -> None:
            stop: list[str] = []
            draining = asyncio.create_task(_drain_the_peer_queue(container, stop))
            try:
                await asyncio.wait_for(console.run(), timeout=_SESSION_DEADLINE_SECONDS)
            finally:
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)

        asyncio.run(drive())
        yield _Session(
            lines=tuple(lines),
            prompts=tuple(keyboard.prompts),
            marks=tuple(keyboard.marks),
            model=model,
            container=container,
        )
    finally:
        if launched:
            DBOS.destroy()
        if container is not None:
            container.audit_pool.close()
            container.domain_pool.close()
        patch.undo()
        _drop_databases()


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


@_needs_postgres
@pytest.mark.silent
def test_a_question_typed_at_the_console_reaches_the_peer_loop(
    console_session: _Session,
) -> None:
    """t-f11-51: the whole anchor, asserted from the transcript and nowhere else.

    In direct mode the asker's `ask_peer` call suspends the turn and no mailbox is ever
    asked - `_step_ask_peers` lives in the DBOS workflow, and a console that calls
    `StartTurn` directly never reaches it. So the proof that the durable path is real is
    that the PEER's model ran at all: the only thing that starts it is a worker claiming an
    ask that a workflow step put on the mailbox.
    """
    assert console_session.model.peer_requests, (
        "the peer's model was never called, so no ask ever reached the mailbox. A turn "
        "typed at the console still suspends on ask_peer and nothing asks anybody - which "
        "is t-f11-51 exactly as it was filed.\n" + console_session.text
    )

    answer = console_session.after(_TYPED_QUESTION)
    assert any(_A_FINAL in line for line in answer), (
        "the console never printed the asker's finished answer. The durable turn was "
        "enqueued but the operator was left holding a turn id:\n" + "\n".join(answer)
    )


@_needs_postgres
def test_the_console_says_which_mode_it_is_in_at_all_times(
    console_session: _Session,
) -> None:
    """Not only at startup. A mode that is legible once is a mode an operator forgets.

    The prompt is the only line an operator reads on EVERY interaction, so the active mode
    rides on it; `:mode` with no argument answers the same question on demand.
    """
    assert console_session.prompts, "the console never asked for a line"
    assert console_session.prompts[0].startswith(f"{_ASKER} [durable]"), (
        f"the first prompt was {console_session.prompts[0]!r}. The console was started "
        "with the durable flag, and a console that does not carry its mode on the prompt "
        "exercises less than the operator thinks, silently."
    )
    assert any(prompt.startswith(f"{_ASKER} [direct]") for prompt in console_session.prompts), (
        f"no prompt ever showed the direct mode: {console_session.prompts}. `:mode direct` "
        "did not put the fast path back - and the durable path is an ADDITION, not a "
        "replacement: trading one good property for another is not an upgrade."
    )
    assert console_session.prompts[-1].startswith(f"{_ASKER} [durable]"), (
        f"the last prompt was {console_session.prompts[-1]!r}, so the second `:mode "
        "durable` did not switch back. The mode has to be movable in both directions or "
        "one of the two paths is unreachable for the rest of the session."
    )

    asked = "\n".join(console_session.after(":mode"))
    assert "direct" in asked and "durable" in asked, (
        "`:mode` with no argument must answer which mode is active and name the other "
        f"one, so an operator can find the switch without :help:\n{asked}"
    )


@_needs_postgres
@pytest.mark.silent
def test_the_banner_tells_the_truth_in_both_modes(console_session: _Session) -> None:
    """Untrue honesty is worse than none, and this banner is the only honesty there is.

    The startup banner is the durable one here, because the console was started with the
    flag. It must NOT start claiming coalescing, which is opened by `POST /turns` through
    `enqueue_turn_window` and is still never run - and it must name the second process
    without which a delegated turn parks until its reply timeout. Switching to direct must
    restate the three guarantees THAT mode gives up, in the console's own words.
    """
    banner = console_session.banner
    assert "coalescing" in banner, (
        "the durable-mode banner does not mention coalescing. The per-session window is "
        "opened by POST /turns (enqueue_turn_window); a console turn enqueues the turn "
        f"itself, so the window still never runs and the banner has to say so:\n{banner}"
    )
    assert "peer-worker" in banner, (
        "the durable-mode banner does not say that something must drain the peer queue. An "
        "operator whose turn parks forever deserves the command, not a surprise:\n" + banner
    )

    back = "\n".join(console_session.after(":mode direct"))
    assert back.strip(), "`:mode direct` printed nothing at all"
    for guarantee in ("durability", "coalescing", "publication"):
        assert guarantee in back, (
            f"switching to direct mode did not restate that {guarantee} is not exercised "
            f"there. That sentence is the only honesty this console has:\n{back}"
        )
    assert "ask_peer" in back, (
        "the direct-mode notice does not say that an ask_peer call merely SUSPENDS there "
        "and asks nobody. That is the gap t-f11-51 was filed for, and an operator who "
        f"switches back must not rediscover it by waiting:\n{back}"
    )

    forward = "\n".join(console_session.after(":mode durable"))
    assert "coalescing" in forward, (
        "switching back to durable mode printed no notice of its own. Every switch has to "
        f"reprint what the new mode does not run:\n{forward}"
    )


@_needs_postgres
def test_a_console_turn_has_a_channel_to_leave_on(console_session: _Session) -> None:
    """`_step_deliver` is the workflow's LAST step, and it looks up `caller.channel`.

    The console stamps `cli` on every turn. With no entry under that id the turn dies with
    `UnknownChannelError` after the model has been paid for - so the run above finishing at
    all is most of this assertion, and the registry is read directly to say WHY.

    `http` is registered as a deliberate pull-mode no-op because the answer is retrieved
    rather than pushed. A polling console is the same shape, so it takes the same class
    rather than a second no-op written for the occasion.
    """
    registry = console_session.container.channels
    assert _OPERATOR_CHANNEL in registry, (
        f"nothing is registered under {_OPERATOR_CHANNEL!r}, the channel the console "
        f"stamps on every turn. Registered: {list(registry.channel_ids())}."
    )
    assert isinstance(registry.get(_OPERATOR_CHANNEL), composition.PullModeChannel), (
        f"the {_OPERATOR_CHANNEL!r} channel is "
        f"{type(registry.get(_OPERATOR_CHANNEL)).__name__}, not PullModeChannel. A second "
        "no-op class would be reusable for the case that must stay loud - a channel that "
        "genuinely cannot deliver."
    )


@_needs_postgres
def test_the_shipped_console_command_launches_the_durable_engine(
    console_session: _Session,
) -> None:
    """`python -m agent_core console --durable`, and the composition decision it makes.

    `run_console` declines to launch DBOS because the console calls `StartTurn` directly, so
    there is no queue to dequeue and no workflow to recover. In durable mode that reason no
    longer holds: the turn is enqueued onto a queue that does not exist until the engine is
    launched, and it is THIS process that has to dequeue it.

    Driven last on purpose - it closes the pools the fixture opened on its way out.
    """
    patch = pytest.MonkeyPatch()
    launched: list[Container] = []
    built: list[dict[str, Any]] = []

    class _NoopConsole:
        async def run(self) -> None:
            return None

    async def already_started() -> Container:
        return console_session.container

    def record_console(container: Container, **kwargs: Any) -> Any:
        del container
        built.append(kwargs)
        return _NoopConsole()

    patch.setattr(main_module, "start_container", already_started)
    patch.setattr(main_module, "refuse_unless_ready", lambda settings, **kw: None)
    patch.setattr(main_module, "launch_dbos", launched.append)
    patch.setattr(main_module, "build_console", record_console)
    try:
        main_module.main(["console", "--durable", "--profile", _ASKER])
    except SystemExit as refused:
        pytest.fail(
            "`python -m agent_core console --durable` was refused by the command line "
            f"({refused}). The durable path an operator can type is the whole of "
            "t-f11-51: without the flag there is no way in from a terminal."
        )
    finally:
        patch.undo()

    assert launched, (
        "`run_console` launched no durable engine for `--durable`. `enqueue_turn` puts the "
        "turn on a queue that does not exist until `launch_dbos` runs, and it is this "
        "process that has to dequeue it - so the first durable turn would fail at the "
        "enqueue, from the prompt, with the operator holding nothing."
    )
    assert built and built[-1].get("durable") is True, (
        f"`build_console` was called with {built[-1] if built else None}. The flag has to "
        "reach the console or the process launches an engine nothing uses and starts the "
        "operator in the mode they did not ask for."
    )
