"""F11's acceptance run: an empty PostgreSQL instance, ONE command, and an operator.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-17
Covers:  agent_core/main.py (main -> run_console -> start_container -> build_console),
         adapters/driving/cli/console.py, adapters/driving/cli/preflight.py,
         agent_core/composition.py, adapters/driven/policy_fs/loader.py,
         adapters/driven/tools/provider.py, adapters/driven/mcp/toolsets.py,
         adapters/driven/agent_pydantic/runner.py (PolicyEnforcement),
         adapters/driven/persistence_pg/audit_repository.py

WHY THIS IS A SCRIPT AND NOT A CHECKLIST
    Every other anchor in F11 exists because a human performed a step by hand and nothing
    complained. A phase that closes those gaps and then verifies ITSELF by hand has not
    learned the lesson it was created by - so the criterion is executed here, from outside,
    against a server that has none of this deployment's databases on it.

    The run starts from an EMPTY instance on purpose. The baseline this project reported
    for weeks was not reproducible because several integration tests only passed against
    scratch databases left behind by earlier runs (docs/TASKS.md#t-f11-26). A step quietly
    depending on inherited state has to fail HERE rather than on the first machine that is
    genuinely fresh, so `_drop_databases` enumerates and destroys every
    `agent_core_acceptance_test*` database before the run and again after it.

IT DRIVES THE CONSOLE, THROUGH `python -m agent_core console`, AND NOTHING BELOW IT
    `main.main(["console", ...])` is the one command. It reads `Settings.from_env`, runs
    `start_container` (databases, schema, policy), runs the preflight, builds the console
    and reads lines. This module supplies only the two things a terminal supplies: the
    keystrokes (`builtins.input`) and the screen (`sys.stdout`). Reaching past the console
    into a use case would prove the machinery works and leave the thing being SHIPPED
    unverified, which is exactly the mistake this phase is made of.

    The transcript is captured once, whole, and every test below reads a slice of it. One
    run, one bootstrap, one paid model call - and every criterion asserted against the same
    session an operator would have sat in front of.

WHAT IS REAL HERE, AND WHAT IS NOT
    REAL: the local PostgreSQL instance, the schema, the reconciled policy rules, the
    profile loader, the tool provider, the MCP server (a stdio child process spawned from
    `delivery_optimizer.yaml`), the policy engine, the audit sink and reader, and ONE live
    `minimax/MiniMax-M3` turn through LiteLLM.

    FAKED: nothing. There is no test double anywhere in this module. What is *absent* is
    the durable engine - `run_console` deliberately does not launch DBOS, and says so in
    its own banner - so this run proves nothing about crash recovery, coalescing, or the
    per-session partitioned queue. Those are `test_durability.py` and `test_coalescing.py`.

    The one live turn is asserted for a POLICY outcome, never for prose: the assertion is
    that the model emitted a structured `pricing_apply` call and that the engine refused
    it. A model that declines to call the tool fails this test, and that is correct - the
    criterion is "an operator watches a tool refused", and there is nothing to watch if
    nothing was attempted. The prompt is a plain instruction with a real order id and a
    price change inside the profile's own 15% approval threshold, so the persona has no
    reason of its own to stop: what stops it is `Core/policy/rules.yaml`.

    docs/FIELD-NOTES.md records that padding a live M3 prompt with a repeated single
    character makes it burn its budget in `ThinkingPart` and answer empty. Nothing here
    pads anything.

THE PROFILES DIRECTORY IS A COPY, AND THAT IS DELIBERATE
    `:new` writes a profile FILE. Pointed at `Core/profiles/` this run would scaffold an
    agent into the shipped set the day that seat is bound, so the run is pointed at a copy
    of the shipped directory under `tmp_path`. It also stops a developer's own gitignored
    `.env` from repointing the acceptance run at another deployment's profiles.

WHAT THIS RUN FOUND, AND WHY FOUR ASSERTIONS BELOW ARE `xfail(strict=True)`
    A criterion that cannot be proved is NAMED, never weakened into something that passes.
    A strict xfail does not pass: it records the exact observed failure, and it turns RED
    the day the gap closes, which is when the marker has to come off.

      1. `main.build_console` leaves six of the console's commands bound to nothing.
         `:new`, `:reload`, `:approve`, `:refuse`, `:sessions` and `:trace` all answer
         "this console has no ... wired". The console (t-f11-09..t-f11-13) is finished;
         `build_console` passes `profiles_dir`, `load_profiles`, `approvals` and
         `transcripts` for none of them, and `Container` has no transcript-reader seat at
         all. So "an operator creates an agent" and "reads the whole exchange" are, from
         the shipped process, unreachable.
      2. `support_triage` names `billing_specialist` as a peer and gets no `ask_peer` tool:
         `composition.TOOL_PACKAGES` registers `delivery` and `fraud` and nothing wires an
         `AgentMailbox` into the container at all. Every A2A mechanism in the tree
         (t-f9-03..t-f9-09) is reachable only from a unit test, so one agent cannot
         orchestrate another.
      3. `ARGUMENT_ALLOWLIST` in `audit_repository.py` is `{}`, so every tool call in the
         trail renders "arguments: none recorded". t-f11-11 asks the console to show a call
         WHOLE; the sink it reads records no argument for any tool in this deployment.
      4. The preflight still checks `<app>_dbos`, the name t-f11-20 retired. A correctly
         bootstrapped instance is therefore told a database is missing and handed a remedy
         that creates one nothing ever opens.

    A fifth thing is observed and is NOT filed as a defect, because it is configuration
    behaving as designed: the MCP tools arrive as `mcp_routing_*`, fenced and budgeted, and
    `Core/policy/rules.yaml` carries no rule for them - so they reduce to
    `deny [no matching rule]` and are never advertised to the model. Reaching an MCP tool
    end to end needs a reviewed rule in that file. That is a YAML change, which is the
    claim F11 makes; it is asserted below as the current, honest state.

NOTHING HERE PRINTS, LOGS, COMMITS OR ASSERTS ON A CREDENTIAL VALUE.
    The model credential is checked for PRESENCE only and never read into a variable that
    outlives the check. `Settings.from_env` performs the `MINIMAX_API` -> `MINIMAX_API_KEY`
    export itself (t-f11-23); this module does not carry the value across that boundary.

SKIP GUARDS
    Skips with a reason `-rs` prints when no Postgres is reachable or no model credential
    is available. CI without either must not fail.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import os
import re
import shutil
import uuid
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from agent_core import composition
from agent_core import main as main_module
from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
)

pytestmark = [pytest.mark.phase("F11")]

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DATABASE = "agent_core_acceptance_test"

# `Core/tests/integration/this_file.py` -> `Core/`, then the repository root.
_CORE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _CORE_ROOT.parent
_SHIPPED_PROFILES = _CORE_ROOT / "profiles"
_SHIPPED_POLICY = _CORE_ROOT / "policy"

# The agent the operator starts on, and the one whose profile connects an MCP server.
_START_PROFILE = "delivery_optimizer"
# The agent whose profile names a peer.
_PEER_PROFILE = "support_triage"
# The agent the operator scaffolds. It does not exist in `Core/profiles/`.
_NEW_PROFILE = "acceptance_desk"

# A real order in the delivery vertical's fixture, priced at 8.50 (delivery/tools.py).
# 8.50 -> 9.00 is +5.9%, inside `delivery_optimizer.yaml`'s own `abs(pct_change) > 15`
# approval rule, so the PROFILE has no reason to stop and the POLICY is what refuses.
_TURN = (
    "Reprice order ord-1 from 8.50 to 9.00 USD. That is under six percent, well inside "
    "this profile's own 15 percent threshold, so no proposal step is needed. Call "
    "pricing_apply with order_id ord-1 and new_price 9.00 now."
)

# The whole session, in the order an operator would type it. Every command is unique, so
# the transcript can be sliced by command without ambiguity.
_SCRIPT: tuple[str, ...] = (
    ":agents",
    ":profile",
    ":tools",
    ":policy pricing_apply",
    f":new {_NEW_PROFILE}",
    ":reload",
    f":use {_NEW_PROFILE}",
    f":use {_START_PROFILE}",
    _TURN,
    ":audit",
    ":pending",
    ":approve no-such-handle",
    f":use {_PEER_PROFILE}",
    ":profile ",  # trailing space: a second, distinct key for the same command
    ":tools ",
    ":sessions",
    ":trace admin",
    ":trace user",
    ":quit",
)

# The sentence the console prints for a seat `main.build_console` never filled. Matched as
# a fragment rather than whole, because the rest of each message names the missing
# collaborator and that wording belongs to console.py.
_UNWIRED = "this console has no"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _model_credential_available() -> bool:
    """Is there a MiniMax credential to be had? PRESENCE only - no value is returned.

    `Settings.from_env` exports `MINIMAX_API` as `MINIMAX_API_KEY` (t-f11-23), so the run
    itself needs nothing from this function beyond the decision to run at all.
    """
    if os.environ.get("MINIMAX_API_KEY") or os.environ.get("MINIMAX_API"):
        return True
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return False
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() in {"MINIMAX_API", "MINIMAX_API_KEY"}:
            return bool(value.strip().strip('"').strip("'"))
    return False


_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)
_needs_model = pytest.mark.skipif(
    not _model_credential_available(),
    reason="no MiniMax credential: set MINIMAX_API_KEY, or MINIMAX_API in the repo .env",
)


def _conninfo_for(database: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{database}\1", _ADMIN_CONNINFO)


def _drop_databases() -> None:
    """Leave the instance EMPTY of this run's databases. Executed, never merely described.

    Enumerated rather than named: t-f11-20 moved `<app>_dbos` to `<app>_dbos_sys` and a
    cleanup written as a list of names is only empty until a name moves again. The LIKE
    pattern is built from a constant defined in this module, so nothing an environment
    variable controls reaches the statement.

    `WITH (FORCE)` because the run opens pools against these databases and a cleanup that
    can be blocked by the thing it is cleaning up is not a cleanup.
    """
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        leftovers = [
            str(row[0])
            for row in admin.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (f"{_APP_DATABASE}%",),
            ).fetchall()
        ]
        for database in leftovers:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def _database_exists(database: str) -> bool:
    with psycopg.connect(_ADMIN_CONNINFO) as admin:
        return (
            admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
            ).fetchone()
            is not None
        )


class _Keyboard:
    """The operator's keystrokes, and the echo a terminal would have shown.

    It stands in for `builtins.input`, which is what `main._stdin_reader` calls, so the
    console is driven through exactly the seam a piped script would use. Each command is
    echoed onto the same captured stream as the console's own output - so the transcript
    reads like a terminal session - and the line index just past that echo is recorded, so
    a test can ask for one command's output without parsing prose.

    Running out raises `EOFError`, which `_stdin_reader` already treats as end of input.
    The script ends with `:quit`, so that is a backstop, not the exit.
    """

    def __init__(self, buffer: io.StringIO, commands: Sequence[str]) -> None:
        self._buffer = buffer
        self._commands = tuple(commands)
        self._index = 0
        self.marks: list[tuple[str, int]] = []

    def __call__(self, prompt: str = "") -> str:
        if self._index >= len(self._commands):
            raise EOFError
        command = self._commands[self._index]
        self._index += 1
        print(f"{prompt}{command}")
        self.marks.append((command, self._buffer.getvalue().count("\n")))
        return command

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._commands)


@dataclass(frozen=True, slots=True)
class _Session:
    """One whole console session, sliceable by the command that produced each part."""

    lines: tuple[str, ...]
    marks: tuple[tuple[str, int], ...]
    app_conninfo: str
    profiles_dir: Path
    exit_code: int | None = None

    def after(self, command: str) -> tuple[str, ...]:
        """Everything the console printed in answer to one command."""
        positions = [index for name, index in self.marks if name == command]
        assert positions, (
            f"{command!r} is not in the acceptance script, so nothing can be asserted "
            f"about it. Scripted: {[name for name, _ in self.marks]}"
        )
        start = positions[0]
        later = [index for _, index in self.marks if index > start]
        end = min(later) if later else len(self.lines)
        # -1 drops the echo line itself, which the keyboard printed.
        return self.lines[start : max(start, end - 1)]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture(scope="module")
def acceptance_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    """THE RUN. An empty instance, one command, one scripted operator, one transcript.

    Module scoped because it bootstraps a database and pays for a model call, and because
    the criterion is one SESSION rather than a set of independent checks - `:audit` answers
    "the last turn", so the turn and the read of it have to be the same run.

    `monkeypatch` is function scoped and cannot be used here, so the environment is saved
    and restored by hand in the `finally`. The profiles directory is a COPY of the shipped
    one - see the module docstring - and the policy directory is the shipped one, read only.

    cwd is the repository root for the whole run: `delivery_optimizer.yaml` spawns its MCP
    server with a repository-relative path, which is what lets a fresh clone serve that
    profile with no network and no credential.
    """
    profiles_dir = tmp_path_factory.mktemp("acceptance") / "profiles"
    shutil.copytree(_SHIPPED_PROFILES, profiles_dir)
    app_conninfo = _conninfo_for(_APP_DATABASE)

    overrides = {
        "AGENT_CORE_DATABASE_URL": app_conninfo,
        "AGENT_CORE_AUDIT_DATABASE_URL": app_conninfo,
        "AGENT_CORE_ADMIN_DATABASE_URL": _ADMIN_CONNINFO,
        "AGENT_CORE_PROFILES_DIR": str(profiles_dir),
        "AGENT_CORE_POLICY_DIR": str(_SHIPPED_POLICY),
    }
    previous = {name: os.environ.get(name) for name in overrides}
    previous_cwd = Path.cwd()
    real_input = builtins.input

    _drop_databases()
    assert not _database_exists(_APP_DATABASE), (
        "the instance is not empty before the run, so a step could inherit state from an "
        "earlier one and this whole module would certify nothing"
    )

    buffer = io.StringIO()
    keyboard = _Keyboard(buffer, _SCRIPT)
    exit_code: int | None = None
    try:
        os.environ.update(overrides)
        os.chdir(_REPO_ROOT)
        builtins.input = keyboard  # type: ignore[assignment]
        try:
            with contextlib.redirect_stdout(buffer):
                main_module.main(["console", "--profile", _START_PROFILE])
        except SystemExit as refused:
            # `refuse_unless_ready` exits rather than raising a traceback. Captured so the
            # preflight test can assert on it instead of the whole module erroring out.
            exit_code = refused.code if isinstance(refused.code, int) else 1
        yield _Session(
            lines=tuple(buffer.getvalue().splitlines()),
            marks=tuple(keyboard.marks),
            app_conninfo=app_conninfo,
            profiles_dir=profiles_dir,
            exit_code=exit_code,
        )
    finally:
        builtins.input = real_input
        os.chdir(previous_cwd)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _drop_databases()


# ---------------------------------------------------------------------------
# The criterion, one clause at a time
# ---------------------------------------------------------------------------


@_needs_postgres
@_needs_model
def test_one_command_against_an_empty_instance_reaches_an_operator_prompt(
    acceptance_run: _Session,
) -> None:
    """"A fresh clone with an empty PostgreSQL instance starts with ONE command."

    Nothing between the empty instance and the prompt was done by hand: no `CREATE
    DATABASE`, no `INSERT INTO policy_rules`, no exported credential. The command is
    `python -m agent_core console`, and `main.main` is what that resolves to.
    """
    assert acceptance_run.exit_code is None, (
        f"the one command refused to start (exit {acceptance_run.exit_code}). The "
        "preflight report is in the transcript above the refusal and names every gap:\n"
        + acceptance_run.text
    )

    assert _database_exists(_APP_DATABASE), (
        "the app database does not exist after the run, so `CREATE DATABASE "
        f"{_APP_DATABASE}` is still a step a human performs by hand (t-f11-02)."
    )

    with psycopg.connect(acceptance_run.app_conninfo) as conn:
        applied = {
            str(row[0])
            for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
        }
        rules = conn.execute("SELECT count(*) FROM policy_rules").fetchone()

    assert applied, "one command left the schema unmigrated"
    assert rules is not None and rules[0] > 0, (
        "policy_rules is empty after the one command, so this deployment denies every "
        "tool with `[no matching rule]` and the only way to change that is raw SQL "
        "(t-f11-03)."
    )

    assert "Type :help for commands. Plain text is a turn." in acceptance_run.lines, (
        "the console never reached its prompt:\n" + acceptance_run.text
    )
    assert acceptance_run.lines[-1] == "leaving.", (
        "the session did not end at `:quit`, so part of the script never ran:\n"
        + acceptance_run.text
    )


@_needs_postgres
@_needs_model
def test_the_preflight_answers_what_is_not_ready_in_one_pass(
    acceptance_run: _Session,
) -> None:
    """t-f11-04: every gap named ONCE, before the prompt, not one layer at a time.

    Asserted as coverage rather than as a verdict - the point of the preflight over the
    failure ladder is that the database, the schema, the rules, every profile, the
    credential and the model all get an answer in the same pass.
    """
    report = "\n".join(acceptance_run.lines[: acceptance_run.marks[0][1]])

    assert re.search(r"preflight: \d+ checks", report), (
        "no preflight report was printed before the prompt:\n" + report
    )
    for expected in (
        f"database {_APP_DATABASE}",
        f"schema {_APP_DATABASE}",
        "policy_rules",
        f"profile {_START_PROFILE}",
        f"profile {_PEER_PROFILE}",
        "MINIMAX_API_KEY",
        "model minimax/MiniMax-M3",
    ):
        assert expected in report, (
            f"the preflight never answered for {expected!r}, so that gap is still "
            "discovered the way the five before it were - one command at a time:\n"
            + report
        )
    assert "[FAIL]" not in report, (
        "a freshly bootstrapped instance is not ready according to its own preflight:\n"
        + report
    )


@_needs_postgres
@_needs_model
def test_every_shipped_agent_loads_and_shows_the_configuration_it_resolved(
    acceptance_run: _Session,
) -> None:
    """`:agents` and `:profile` - navigation, and the RESOLVED shape of one agent.

    The expected ids come from the directory rather than from a list written here: a
    literal list pins WHICH agents this deployment ships, which is configuration and not
    behaviour.
    """
    listed = acceptance_run.after(":agents")
    shipped = {path.stem for path in _SHIPPED_PROFILES.glob("*.yaml")}
    for profile_id in shipped:
        assert any(profile_id in line for line in listed), (
            f"{profile_id}.yaml is in the profiles directory and `:agents` does not list "
            f"it, so it did not become a servable agent:\n" + "\n".join(listed)
        )

    resolved = "\n".join(acceptance_run.after(":profile"))
    assert f"profile {_START_PROFILE}" in resolved
    for block in ("toolsets:", "mcp_servers:", "approval_rules:", "peers:", "knowledge:"):
        assert block in resolved, (
            f"`:profile` omits {block!r}. Every block a profile file may leave out still "
            "needs the value the loader settled on, or an operator cannot tell 'absent' "
            f"from 'defaulted':\n{resolved}"
        )
    assert "(endpoint withheld)" in resolved, (
        "`:profile` printed an MCP server's endpoint. An MCP url is exactly where a token "
        f"lives in real configuration:\n{resolved}"
    )


@_needs_postgres
@_needs_model
def test_a_profile_gives_an_agent_its_tools_and_an_mcp_server_with_no_code_change(
    acceptance_run: _Session,
) -> None:
    """"Give it a tool, connect it to an MCP server" - both are YAML, and this reads them.

    `delivery_optimizer.yaml` names one toolset and one stdio server. `:tools` asks the
    real `ToolProvider` what that resolved to and the real `ToolPolicy` what happens to
    each name, so this is the join production makes and not a second opinion about it.

    THE MCP HALF IS FENCED AND IT IS ALSO DENIED, AND BOTH ARE ASSERTED.
    The server's tools arrive `mcp_routing_*` prefixed and marked UNTRUSTED-WRAPPED
    (non-negotiables #4 and #10). `Core/policy/rules.yaml` carries no rule matching that
    prefix, so they reduce to the snapshot's default effect and are never advertised to
    the model. That is fail-closed configuration behaving as designed - reaching one end
    to end is a reviewed rule in that file, which is a YAML change and therefore exactly
    the claim F11 makes.
    """
    tools = acceptance_run.after(":tools")
    rendered = "\n".join(tools)

    for local in ("orders_lookup", "routing_estimate", "pricing_quote", "pricing_apply"):
        assert any(line.strip().startswith(local) for line in tools), (
            f"{local} is in the `delivery` package this profile names and `:tools` does "
            f"not resolve it:\n{rendered}"
        )

    mcp_names = [
        line.split()[0] for line in tools if line.strip().startswith("mcp_routing_")
    ]
    assert mcp_names, (
        "the profile declares an MCP server and no `mcp_*` tool resolved, so the server "
        f"was never reached and t-f11-16's claim is untested here:\n{rendered}"
    )
    for line in tools:
        if line.strip().startswith("mcp_routing_"):
            assert "UNTRUSTED-WRAPPED" in line, (
                "an MCP tool is not marked as untrusted-wrapped, so an operator reading "
                "this screen cannot see the boundary defending non-negotiables #4 and "
                f"#10:\n{rendered}"
            )
            assert "deny" in line, (
                "an `mcp_routing_*` tool is not denied by the shipped rules. That may be "
                "correct now - but it is a change to what this deployment ships, and this "
                f"assertion is where it gets noticed:\n{rendered}"
            )

    assert "hang" not in rendered, (
        "the MCP fixture's deliberately wedged `hang` tool was offered. "
        "`tool_exclude` in the profile is a scope reducer applied at discovery and it did "
        f"not apply:\n{rendered}"
    )
    assert "advertised to the model:" in rendered


@_needs_postgres
@_needs_model
def test_the_policy_engine_answers_for_this_caller_and_names_the_rule_that_wins(
    acceptance_run: _Session,
) -> None:
    """`:policy pricing_apply` - the verdict, the `rule_id`, and the sentence it carries.

    The policy engine is one of CLAUDE.md's silent-bug areas: a hole never fails a test, it
    only never fires. What makes this assertable is that the engine answers with the rule
    an auditor would read rather than with a yes or a no.
    """
    answer = "\n".join(acceptance_run.after(":policy pricing_apply"))

    assert "needs_approval" in answer and "delivery-pricing-apply-needs-a-human" in answer, (
        "`pricing_apply` did not come back as NEEDS_APPROVAL under the rule "
        f"`Core/policy/rules.yaml` ships for it:\n{answer}"
    )
    assert "Applying a price change moves real money" in answer, (
        "the verdict is printed without the reason the rule gave. On NEEDS_APPROVAL that "
        f"sentence IS the question a human is asked:\n{answer}"
    )
    assert "subject=operator" in answer and "channel=cli" in answer, (
        "the answer does not say whose rules these are. A rule scoped to a channel or a "
        "role this identity does not carry simply will not match, and that looks "
        f"identical to a broken engine:\n{answer}"
    )


@_needs_postgres
@_needs_model
def test_a_real_model_calls_a_mutating_tool_and_the_engine_refuses_it(
    acceptance_run: _Session,
) -> None:
    """"Watch a tool refused" - live, through the real runner, with the real rules.

    THE ONE PAID CALL IN THIS MODULE. `minimax/MiniMax-M3` through LiteLLM, the model the
    shipped profiles name, on the production path: no injected model factory, no test
    double for the runner, no seeded policy row. The assertion is about the POLICY outcome
    and never about the model's prose.

    `PolicyEnforcement.before_tool_execute` writes the audit row BEFORE it raises, so the
    refusal and its record are one fact. What is asserted is all four halves of it: the
    tool the model chose, the effect, the rule that decided, and that it did NOT run.
    """
    turn = "\n".join(acceptance_run.after(_TURN))

    assert re.search(r"\[turn [0-9a-f-]{36} / " + _START_PROFILE + r"\]", turn), (
        f"the turn never started or was never attributed to an agent:\n{turn}"
    )
    assert "pricing_apply" in turn, (
        "the model did not emit a `pricing_apply` call, so there was no refusal to watch. "
        "The criterion is an operator seeing a tool REFUSED, and nothing was attempted:\n"
        f"{turn}"
    )
    assert re.search(
        r"tool pricing_apply -> needs_approval \[delivery-pricing-apply-needs-a-human\]"
        r" - NOT EXECUTED",
        turn,
    ), (
        "a `pricing_apply` call did not land in the trail as refused by the rule that "
        "governs it. The three facts have to travel together - which tool, which rule, "
        f"and whether it ran - or a refusal becomes folklore (t-f11-11):\n{turn}"
    )
    assert "Applying a price change moves real money" in turn, (
        "the refusal was rendered without the sentence the rule gave. That sentence is "
        "what the model is handed and what a human would be asked (t-f11-07):\n" + turn
    )


@_needs_postgres
@_needs_model
def test_the_whole_exchange_is_readable_back_out_of_the_trail(
    acceptance_run: _Session,
) -> None:
    """`:audit` - the last turn's calls, read back from `audit_tool_calls` afterwards.

    The console reads the trail AFTER the turn rather than reporting what it believes it
    did, so this is a round trip through Postgres and not an echo. The row is the one
    non-negotiable #6 guarantees survives even a turn whose domain writes rolled back.

    WHICH tools the model chose is NOT asserted - that is the model's decision and it
    varies between runs. What is asserted is that the set it chose is the same set on the
    screen, in `:audit`, and in the table. A trail that agrees with the screen and
    disagrees with the database is the failure this catches.
    """
    trail = "\n".join(acceptance_run.after(":audit"))

    assert "tool calls of turn " in trail, f"`:audit` answered nothing:\n{trail}"
    assert "pricing_apply -> needs_approval" in trail, (
        "the refusal the turn printed is not in the trail read back afterwards, so the "
        f"record and the screen disagree:\n{trail}"
    )
    assert "by subject=operator" in trail, (
        f"the trail does not say who the call was attributed to:\n{trail}"
    )

    with psycopg.connect(acceptance_run.app_conninfo) as conn:
        rows = conn.execute(
            "SELECT tool, effect, rule_id, reason, tenant_id FROM audit_tool_calls "
            "ORDER BY tool"
        ).fetchall()

    assert rows, "nothing reached audit_tool_calls at all"
    rendered = {
        line.split()[1]
        for line in trail.splitlines()
        if line.startswith("  tool ") and " -> " in line
    }
    assert rendered == {str(row[0]) for row in rows}, (
        "the console's trail and `audit_tool_calls` do not name the same tool calls. One "
        f"of the two is answering from somewhere else:\n{trail}"
    )
    refusals = [row for row in rows if row[0] == "pricing_apply"]
    assert refusals, "the refused call has no row in the table the console read it from"
    assert refusals[0][1] == "needs_approval"
    assert refusals[0][2] == "delivery-pricing-apply-needs-a-human"
    assert refusals[0][3], (
        "migration 0022's `reason` column is NULL for a row this deployment just wrote. "
        "The trail records WHICH rule won and not WHAT IT SAID (t-f11-07)."
    )
    assert refusals[0][4], (
        "migration 0023's `tenant_id` column is NULL, so this row can be scoped only to "
        "a turn and an `AuditReader` cannot be narrowed to a tenant without the join "
        "t-f11-21 refuses to make."
    )


@_needs_postgres
@_needs_model
def test_a_suspension_that_no_console_turn_can_publish_says_so_rather_than_lying(
    acceptance_run: _Session,
) -> None:
    """`:pending` and `:approve` - and the honest half, which is the point.

    The console calls `StartTurn` directly, so `HumanGateway.publish` never runs and no
    correlation row exists for anything a console turn suspends on. Printing a queue and
    leaving the operator to discover that by typing at it is exactly the "exercises less
    than the real path" failure the banner exists to prevent, so both commands are asserted
    to SAY it. The approval path proper is the next test.
    """
    banner = "\n".join(acceptance_run.lines[: acceptance_run.marks[0][1]])
    assert "HumanGateway.publish never runs" in banner, (
        "the banner does not warn that a console turn has no correlation handle, so an "
        f"operator finds out by typing `:approve`:\n{banner}"
    )

    pending = "\n".join(acceptance_run.after(":pending"))
    assert "nothing is pending" in pending or "correlation handle" in pending, (
        f"`:pending` printed a queue with no word about answering it:\n{pending}"
    )


@_needs_postgres
@_needs_model
def test_approving_from_the_console_is_refused_by_the_four_eyes_rule(
    acceptance_run: _Session,
) -> None:
    """D25: the approver may not be the requester, and in one console they are one person.

    THE REFUSAL IS THE CORRECT OUTCOME AND IT IS WHAT IS ASSERTED. A second identity is
    not arranged here, deliberately: the criterion F11 states is "an operator approves that
    refusal", singular, and D25's answer to a single operator is no. Asserting an approval
    would be asserting the control off.

    WHY THIS DOES NOT GO THROUGH THE SCRIPTED SESSION
        Two things the shipped process does not supply have to be supplied here, and both
        are named rather than papered over:

          1. `main.build_console` binds no `approvals` seat, so `:approve` in the run above
             answers "this console has no approval path wired". That is the finding in
             `test_the_shipped_process_binds_every_console_seat`; this test binds
             `Container.decide_approval` - the object `composition.py` already built - so
             that what the SURFACE does with a real handle can still be checked.
          2. Nothing hands an operator a correlation handle. `:pending` says so itself. So
             the handle is published through the real `ChannelHumanGateway` and read back
             with one SELECT. That SELECT is not the "without writing SQL" claim being
             broken - the claim is about configuring and operating the system - it is how
             this test gets past a missing operator affordance to check D25 behind it.

    The requester is `operator`, because that is the subject the live turn above ran under
    and `_pg_requester_lookup` reads it from that turn's own audit rows. The approver is
    `operator`, because a one-operator console has nobody else. Same person, so
    `SELF_APPROVAL`.
    """
    settings = composition.Settings(
        app_conninfo=acceptance_run.app_conninfo,
        audit_conninfo=acceptance_run.app_conninfo,
        profiles_dir=acceptance_run.profiles_dir,
        policy_dir=_SHIPPED_POLICY,
    )
    previous_cwd = Path.cwd()
    os.chdir(_REPO_ROOT)
    try:
        container = composition.build_container(settings)
        container.domain_pool.open()
        container.audit_pool.open()
        try:
            turn_id = _turn_id_from(acceptance_run)
            session = SessionRef(
                session_id=SessionId(f"acceptance-{uuid.uuid4().hex[:12]}"),
                tenant_id=TenantId(container.settings.channel_tenant_id),
            )
            request = PendingRequest(
                kind=PendingKind.APPROVAL,
                tool_call_id=ToolCallId(f"call-{uuid.uuid4().hex[:12]}"),
                tool_name="pricing_apply",
                arguments={"order_id": "ord-1", "new_price": "9.00"},
                reason="Applying a price change moves real money.",
            )
            _run(container.human_gateway.publish(turn_id, session, (request,)))
            handle = _published_handle(acceptance_run.app_conninfo, turn_id)

            printed: list[str] = []
            console = main_module.build_console(
                container,
                profile_id=_START_PROFILE,
                write_line=printed.append,
                read_line=_keystrokes((f":approve {handle}", ":quit")),
            )
            # The seat the shipped process does not fill. Named here so the day
            # `build_console` fills it, this line becomes redundant rather than wrong.
            console._approvals = container.decide_approval  # noqa: SLF001
            _run(console.run())
        finally:
            container.audit_pool.close()
            container.domain_pool.close()
    finally:
        os.chdir(previous_cwd)

    answer = "\n".join(printed)
    assert "REFUSED by the four-eyes rule" in answer, (
        "an operator approved their own turn's request from a one-operator console. D25 "
        f"says an approval must come from a second person:\n{answer}"
    )
    assert "four-eyes: an approval must come from someone other than" in answer, (
        "the refusal does not carry the grounds that were recorded, so an operator cannot "
        f"tell this control apart from a crash and will work around it:\n{answer}"
    )
    assert "subject='operator'" in answer, (
        f"the refusal does not name the identity it refused:\n{answer}"
    )
    assert ":refuse is never blocked by this rule" in answer, (
        "the refusal does not say what resolves it. A control that only says no teaches "
        f"an operator to route around it:\n{answer}"
    )

    with psycopg.connect(acceptance_run.app_conninfo) as conn:
        rejected = conn.execute(
            "SELECT subject_id, reason FROM audit_rejected_decisions WHERE turn_id = %s",
            (turn_id,),
        ).fetchall()
    assert rejected, (
        "the refused approval left no row in audit_rejected_decisions, so the control "
        "cannot be shown to have fired (D25, non-negotiable #6)."
    )
    assert any("four-eyes" in str(row[1]) for row in rejected), (
        "the recorded grounds do not name the control that fired, so an auditor reading "
        f"this months later learns only that something said no: {rejected!r}"
    )


# ---------------------------------------------------------------------------
# What the run FOUND. Named, not weakened - see the module docstring.
# ---------------------------------------------------------------------------


@_needs_postgres
@_needs_model
def test_the_shipped_process_binds_every_console_seat(acceptance_run: _Session) -> None:
    """Every command in the console's own table must do what it says from the real process.

    Asserted as one test because it is one cause. Splitting it into six would suggest six
    fixes; there is one, and it is a constructor call in `main.build_console`.
    """
    unwired = sorted(
        {
            command
            for command, _ in acceptance_run.marks
            if any(_UNWIRED in line for line in acceptance_run.after(command))
        }
    )
    assert unwired == [], (
        f"{unwired} answered with an unwired seat. The console can do all of it; the "
        "process never handed it the collaborators:\n"
        + "\n".join(
            line for line in acceptance_run.lines if _UNWIRED in line
        )
    )


@_needs_postgres
@_needs_model
def test_one_agent_can_ask_the_peer_its_profile_names(acceptance_run: _Session) -> None:
    """"Point it at another agent, and have one orchestrate the other."

    The configuration half is proved first and unconditionally: the profile loads with the
    peer named. What cannot happen is the ask - so the assertion is that the tool the model
    would have to call is on the agent's resolved surface. It is not.
    """
    profile = "\n".join(acceptance_run.after(":profile "))
    assert "may_ask=billing_specialist" in profile, (
        "support_triage.yaml names a peer and the loader dropped it, which is t-f11-14 "
        f"regressing rather than the orchestration gap:\n{profile}"
    )

    tools = "\n".join(acceptance_run.after(":tools "))
    assert "ask_peer" in tools, (
        "the agent whose profile names a peer resolves to no `ask_peer` tool, so the "
        "model is never offered a way to ask and the two-sided allowlist, the hop limit "
        "and the durable mailbox are all unreachable from configuration:\n" + tools
    )


@_needs_postgres
@_needs_model
def test_the_trail_records_what_the_model_actually_passed(
    acceptance_run: _Session,
) -> None:
    """A refusal an operator cannot see the arguments of is a refusal they cannot judge.

    `pricing_apply` takes an `order_id` and a `new_price`. Neither is a secret, both are
    what the decision was about, and neither is stored.
    """
    with psycopg.connect(acceptance_run.app_conninfo) as conn:
        rows = conn.execute(
            "SELECT arguments FROM audit_tool_calls WHERE tool = %s", ("pricing_apply",)
        ).fetchall()

    assert rows, "the refused call has no audit row, which is a different defect"
    assert any(row[0] for row in rows), (
        "every recorded argument map is empty. The sink's per-tool allowlist is the right "
        "shape - two redaction paths drift - but nothing has opted into it, so the "
        "console's `:audit` can only ever print the disclaimer."
    )


@_needs_postgres
@_needs_model
def test_the_preflight_checks_the_database_dbos_actually_opens(
    acceptance_run: _Session,
) -> None:
    """A preflight pinning a retired name reports a gap that is not there, and misses one.

    The report is the operator's whole picture of what is not ready. A line in it that is
    wrong costs more than a line that is missing: it sends them to create a database no
    process opens, and it says nothing about the one that matters.
    """
    report = "\n".join(acceptance_run.lines[: acceptance_run.marks[0][1]])
    from agent_core.adapters.driven.persistence_pg import migrations

    system_database = migrations.dbos_system_database(_APP_DATABASE)
    assert f"database {system_database}" in report, (
        f"the preflight never looks at {system_database}, the database dbos derives from "
        "the app URL and opens. What it checks instead is "
        f"{_APP_DATABASE}_dbos, which nothing creates and nothing opens:\n{report}"
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run[T](work: Coroutine[object, object, T]) -> T:
    """One coroutine, driven to completion. D13 puts every port above the domain on async."""
    return asyncio.run(work)


def _keystrokes(commands: Sequence[str]) -> Callable[[str], str | None]:
    """A `read_line` seam over a fixed script. `None` is end of input, as the console reads it."""
    remaining = list(commands)

    def read_line(_prompt: str) -> str | None:
        return remaining.pop(0) if remaining else None

    return read_line


def _turn_id_from(run: _Session) -> TurnId:
    """The id the live turn was filed under, as the console printed it."""
    for line in run.lines:
        found = re.search(r"\[turn ([0-9a-f-]{36}) / ", line)
        if found is not None:
            return TurnId(found.group(1))
    raise AssertionError(f"no turn ran in the acceptance session:\n{run.text}")


def _published_handle(conninfo: str, turn_id: TurnId) -> str:
    """The correlation handle for a published request - see the four-eyes test's docstring.

    Read rather than returned, because `HumanGateway.publish` answers `None`: the handle
    travels to a human on a channel, and nothing hands one to an operator at a console.
    """
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "SELECT correlation_id FROM human_requests WHERE turn_id = %s", (turn_id,)
        ).fetchone()
    assert row is not None, (
        "publishing the request recorded no correlation row, so there is no handle for "
        "`:approve` to take and D25 cannot be reached at all."
    )
    return str(row[0])
