"""A fresh clone, an empty PostgreSQL instance, a credentials file - and ONE command.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-01, #t-f11-02, #t-f11-03, #t-f11-05
Covers:  agent_core/composition.py (Settings.from_env, start_container, the tool registry),
         adapters/driven/policy_fs/loader.py, Core/policy/rules.yaml

WHY THIS FILE EXISTS
    Every step below is one a human performed BY HAND the first time anyone tried to use
    this system, and nothing complained about any of them (docs/ROADMAP.md, F11):

        CREATE DATABASE agent_core_app and ..._dbos   - start_container migrates only
        export AGENT_CORE_DATABASE_URL, MINIMAX_API_KEY - the process never read `.env`
        INSERT INTO policy_rules                       - nothing in production writes it
        (and fraud_analyst died at StartTurn step 3 on an unregistered toolset)

    Each of the four was invisible because a TEST supplied the missing collaborator: the
    suite created its own databases, read its own `.env`, seeded its own rules and named
    its own toolsets. That is the pattern docs/STATE.md has now recorded six times, and
    the only way to close it is a test that walks the last mile from OUTSIDE - starting
    the production startup path against a database that does not exist yet.

WHAT "EMPTY POSTGRES INSTANCE" MEANS HERE
    Every `agent_core_cold_start_test*` database is DROPPED before each cold-start
    assertion and dropped again afterwards, so what `start_container` faces is a server
    with none of them on it at all - not a truncated schema. A leftover
    `schema_migrations` row would make the applier skip the very migration this asserts it
    applies, and a leftover database would hide the whole of t-f11-02.

    The cleanup enumerates what is on the server rather than naming the databases it
    expects. It used to name them, and t-f11-20 then moved one of the names: the fixture
    went on dropping `<app>_dbos` and `<app>_dbos_dbos_sys`, neither of which anything
    creates any more, and leaked the `<app>_dbos_sys` that a cold start really does make on
    every run. A cleanup written as a list of names is only empty until a name moves.

NOTHING HERE PRINTS, LOGS OR ASSERTS ON A CREDENTIAL VALUE.
    Two of the four rows above are about secrets, so the test that proves they load has to
    be the last place that would leak one. The `.env` assertions below use a synthetic
    connection string written into `tmp_path`; the repository's real `.env` is never read
    by this module, and no assertion message formats a value.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.persistence_pg import migrations

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DATABASE = "agent_core_cold_start_test"

# DERIVED, NEVER SPELLED OUT (t-f11-20). DBOS appends `_dbos_sys` to the app database it
# is handed and opens that; the repo's old `<app>_dbos` convention named a database
# nothing ever connected to. Asking the module that does the deriving means this test
# cannot end up pinning a name production stopped using - which is exactly what it was
# doing before this anchor.
_DBOS_SYSTEM_DATABASE = migrations.dbos_system_database(_APP_DATABASE)

# `Core/tests/integration/this_file.py` -> `Core/`, two parents up.
_CORE_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_PROFILES = _CORE_ROOT / "profiles"
_SHIPPED_POLICY = _CORE_ROOT / "policy"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _conninfo_for(database: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{database}\1", _ADMIN_CONNINFO)


def _drop_databases() -> None:
    """Leave the instance EMPTY of this test's databases. Executed, never merely described.

    Every database whose name starts with `_APP_DATABASE` goes, whoever created it and
    under whatever convention - see this module's docstring for why a list of names was
    not good enough. The pattern is built from a constant defined here, so nothing an
    operator or an environment variable controls reaches the statement.

    `WITH (FORCE)` because a pool this test opened - or the DBOS system database created
    beside the app one - may still hold a session, and a cleanup that can be blocked by
    the thing it is cleaning up is not a cleanup.
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


@pytest.fixture
def empty_instance() -> Any:
    """An instance with no `agent_core_cold_start_test*` database on it, before AND after."""
    _drop_databases()
    try:
        yield _conninfo_for(_APP_DATABASE)
    finally:
        _drop_databases()


def _database_exists(database: str) -> bool:
    with psycopg.connect(_ADMIN_CONNINFO) as admin:
        return (
            admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
            ).fetchone()
            is not None
        )


def _settings_type() -> Any:
    return composition.Settings


def _admin_field_name() -> str:
    """The `Settings` field t-f11-02 adds, stated as a claim rather than an AttributeError."""
    fields = set(getattr(composition.Settings, "__dataclass_fields__", {}))
    assert "admin_conninfo" in fields, (
        "Settings carries no admin connection string, so nothing in production can create "
        "the two logical databases: `CREATE DATABASE agent_core_app` stays a step a human "
        "performs by hand and nothing complains (docs/TASKS.md#t-f11-02)."
    )
    return "admin_conninfo"


def _start(settings: Any) -> Any:
    container: Any = asyncio.run(composition.start_container(settings))
    return container


def _policy_rows(conninfo: str) -> dict[str, dict[str, Any]]:
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute("SELECT rule_id, definition FROM policy_rules").fetchall()
    return {str(row[0]): dict(row[1]) for row in rows}


def _write_env_file(directory: Path, values: dict[str, str]) -> Path:
    path = directory / ".env"
    path.write_text(
        "# a credentials file, exactly as a fresh clone gets one\n"
        + "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# t-f11-01 - the process reads the credentials file, and never over a real variable
# ---------------------------------------------------------------------------


def test_settings_read_the_credentials_file_when_nothing_is_exported(tmp_path: Path) -> None:
    """docs/TASKS.md#t-f11-01. No database needed: this is a pure read of two mappings.

    A fresh clone has a `.env` and an empty shell. Until this anchor the process read only
    the shell, so every credential had to be exported by hand at a terminal - and the only
    thing that ever read the file was the test suite.
    """
    env_file = _write_env_file(
        tmp_path, {"AGENT_CORE_DATABASE_URL": "postgresql://from-the-file/app"}
    )

    settings = composition.Settings.from_env(environ={}, env_file=env_file)

    assert settings.app_conninfo == "postgresql://from-the-file/app", (
        "Settings.from_env ignored the credentials file, so a fresh clone still has to "
        "export every variable by hand (docs/TASKS.md#t-f11-01)."
    )


def test_a_real_environment_variable_always_beats_the_credentials_file(tmp_path: Path) -> None:
    """The file is a FALLBACK. A deployment that exports a variable has said something."""
    env_file = _write_env_file(
        tmp_path, {"AGENT_CORE_DATABASE_URL": "postgresql://from-the-file/app"}
    )

    settings = composition.Settings.from_env(
        environ={"AGENT_CORE_DATABASE_URL": "postgresql://exported/app"},
        env_file=env_file,
    )

    assert settings.app_conninfo == "postgresql://exported/app", (
        "the credentials file overrode an exported variable. A file that wins over the "
        "environment silently repoints a production process at a developer's database."
    )


def test_a_missing_credentials_file_is_not_an_error(tmp_path: Path) -> None:
    """Production sets real variables and ships no `.env`. That must start, not raise."""
    settings = composition.Settings.from_env(
        environ={"AGENT_CORE_DATABASE_URL": "postgresql://exported/app"},
        env_file=tmp_path / "nothing-here.env",
    )

    assert settings.app_conninfo == "postgresql://exported/app"


def test_loading_the_credentials_file_echoes_no_value_anywhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A loader that logs what it loaded puts every secret in the deployment's log store.

    The value below is synthetic and is asserted on only as a substring that must NOT
    appear; nothing here reads the repository's real credentials file.
    """
    secret = "s3cr3t-value-that-must-never-be-echoed"
    env_file = _write_env_file(
        tmp_path,
        {
            "AGENT_CORE_DATABASE_URL": f"postgresql://user:{secret}@example.test/app",
            "TELEGRAM_BOT": secret,
        },
    )

    with caplog.at_level(logging.DEBUG):
        composition.Settings.from_env(environ={}, env_file=env_file)

    captured = capsys.readouterr()
    assert secret not in caplog.text, "a credential value reached the log"
    assert secret not in captured.out + captured.err, "a credential value reached stdout"


# ---------------------------------------------------------------------------
# t-f11-02 - bootstrap is opt-in, and it is idempotent
# ---------------------------------------------------------------------------


@_needs_postgres
def test_a_cold_start_creates_the_databases_migrates_and_puts_the_rules_in_force(
    empty_instance: str,
) -> None:
    """F11's own criterion, minus the operator: ONE command against an EMPTY instance.

    Afterwards the two logical databases exist, the whole schema is migrated, the shipped
    policy rules are in force, and every shipped profile is servable. Not one line of SQL
    was written to get there.
    """
    admin = _admin_field_name()
    settings = _settings_type()(
        app_conninfo=empty_instance,
        **{admin: _ADMIN_CONNINFO},
    )

    assert not _database_exists(_APP_DATABASE), "the fixture left a database behind"

    container = _start(settings)

    assert _database_exists(_APP_DATABASE), (
        "startup did not create the app database. `CREATE DATABASE agent_core_app` is "
        "still a step a human performs by hand (docs/TASKS.md#t-f11-02)."
    )
    assert _database_exists(_DBOS_SYSTEM_DATABASE), (
        f"startup created the app database and not {_DBOS_SYSTEM_DATABASE}, so the "
        "durable engine has nowhere to keep its workflow state. That is the database DBOS "
        "derives from the app URL and opens (t-f11-20); a deployment whose app role may "
        "not CREATE DATABASE gets a warning from DBOS and a broken start without it."
    )

    with psycopg.connect(empty_instance) as conn:
        applied = {
            row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
        }

    assert applied == {m.id for m in migrations.discover_app_migrations()}, (
        "a cold start did not leave the schema migrated"
    )

    rows = _policy_rows(empty_instance)
    assert rows, (
        "policy_rules is EMPTY after a cold start, so this deployment denies every tool "
        "with `[no matching rule]` and has no supported way to change that except raw "
        "SQL (docs/TASKS.md#t-f11-03)."
    )

    # t-f11-05: every shipped profile resolves its toolsets, at LOAD.
    #
    # The expected ids come from the directory, not from a list written here. A literal
    # list pins WHICH agents this deployment ships - configuration, not behaviour - and
    # this assertion had already gone stale once by the time the peer profiles landed.
    # Derived, it still says everything it is worth saying: every shipped file loaded, and
    # each one is keyed by the id inside it rather than quietly by its filename.
    assert set(container.profiles) == {
        path.stem for path in _SHIPPED_PROFILES.glob("*.yaml")
    }, "a shipped profile file did not become a servable profile under its own id"
    for profile in container.profiles.values():
        names = asyncio.run(container.tools.tool_names_for(profile))
        assert names, (
            f"profile {profile.id!r} loaded and resolves to NO tools, so every turn dies "
            "at StartTurn step 3 (docs/TASKS.md#t-f11-05)."
        )


@_needs_postgres
def test_starting_a_second_time_changes_nothing(empty_instance: str) -> None:
    """Idempotent means the second start is a no-op - schema AND policy alike.

    A restart is the ordinary case. A policy applier that inserted again, or a bootstrap
    that failed because the database it wanted already existed, would make every deploy a
    manual step.
    """
    admin = _admin_field_name()
    settings = _settings_type()(
        app_conninfo=empty_instance,
        **{admin: _ADMIN_CONNINFO},
    )

    _start(settings)
    with psycopg.connect(empty_instance) as conn:
        first_migrations = [
            row[0]
            for row in conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
        ]
    first_rules = _policy_rows(empty_instance)

    _start(settings)  # must not raise, must not duplicate
    with psycopg.connect(empty_instance) as conn:
        second_migrations = [
            row[0]
            for row in conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
        ]
    second_rules = _policy_rows(empty_instance)

    assert second_migrations == first_migrations, "the second start changed the applied set"
    assert len(second_migrations) == len(set(second_migrations)), (
        "a migration id was recorded twice"
    )
    assert second_rules == first_rules, "the second start changed the rules in force"


@_needs_postgres
def test_without_an_admin_url_a_missing_database_fails_naming_the_exact_command(
    empty_instance: str,
) -> None:
    """The app must never REQUIRE superuser - so the absent case has to be a good failure.

    `start_container` deliberately does not create databases, and its reason survives: a
    deployment that hands its application superuser credentials has a bigger problem than
    a missing table. What changes is what an operator is told when the database is not
    there: the exact command, not a driver error three layers down.
    """
    settings = _settings_type()(app_conninfo=empty_instance)

    with pytest.raises(Exception) as refused:  # noqa: B017 - the TYPE is not the claim
        _start(settings)

    message = str(refused.value)
    assert _APP_DATABASE in message, "the failure does not name the database that is missing"
    assert "CREATE DATABASE" in message.upper(), (
        "a missing database fails with a driver error that names no remedy. An operator "
        "reading this has to know Postgres to guess the next step "
        "(docs/TASKS.md#t-f11-02)."
    )
    assert "AGENT_CORE_ADMIN_DATABASE_URL" in message, (
        "the failure never mentions the optional admin URL that would have created the "
        "database, so the opt-in bootstrap is undiscoverable."
    )
    assert not _database_exists(_APP_DATABASE), (
        "startup created a database with no admin URL configured, which means the app "
        "role needs CREATEDB. The app must never REQUIRE superuser."
    )


# ---------------------------------------------------------------------------
# t-f11-03 - policy as reviewed configuration
# ---------------------------------------------------------------------------


def _policy_loader() -> Any:
    """The adapter, resolved by name so its absence is an ASSERTION, not an ImportError."""
    try:
        return importlib.import_module("agent_core.adapters.driven.policy_fs.loader")
    except ModuleNotFoundError:
        pytest.fail(
            "there is no adapters/driven/policy_fs/loader.py, so nothing in production "
            "writes policy_rules and a fresh deployment denies every tool until someone "
            "runs an INSERT by hand (docs/TASKS.md#t-f11-03)."
        )


def test_the_shipped_rules_file_exercises_all_three_effects() -> None:
    """No database needed. An operator must see ALLOW, NEEDS_APPROVAL and DENY on day one.

    A uniform wall teaches an operator that the policy engine is a switch. Three effects on
    a vertical they can actually run teach them it is a decision - and the policy engine is
    a silent-bug area (CLAUDE.md), where the only check that ever happens is a human
    reading the rules.
    """
    loader = _policy_loader()
    load = getattr(loader, "load_policy_rules", None)
    assert callable(load), "policy_fs/loader.py exposes no load_policy_rules"

    assert _SHIPPED_POLICY.is_dir(), (
        f"{_SHIPPED_POLICY} does not exist, so the deployment ships no reviewed policy at "
        "all and the first thing an operator meets is `deny [no matching rule]`."
    )

    rules = load(_SHIPPED_POLICY)
    effects = {rule.effect.value for rule in rules}

    assert effects == {"allow", "needs_approval", "deny"}, (
        f"the shipped rules exercise only {sorted(effects)}. docs/ROADMAP.md F11 asks for "
        "all three, so an operator sees a decision rather than a uniform wall."
    )

    delivery_tools = {"orders_lookup", "routing_estimate", "pricing_quote", "pricing_apply"}
    covered = {rule.tool_pattern for rule in rules} & delivery_tools
    assert covered == delivery_tools, (
        f"the shipped rules say nothing about {sorted(delivery_tools - covered)}, which "
        "the delivery profile offers - so those tools are denied by default and the "
        "vertical cannot be driven on day one."
    )
    assert all(rule.rule_id for rule in rules), "a rule with no rule_id is unauditable"
    assert all(rule.reason for rule in rules), (
        "a rule with no reason gives the model nothing on DENY and the human nothing on "
        "NEEDS_APPROVAL (domain/policy.py, PolicyDecision.reason)."
    )


@_needs_postgres
def test_a_rule_removed_from_the_file_stops_being_in_force(
    empty_instance: str, tmp_path: Path
) -> None:
    """An applier that only ever ADDS is a permission nobody can revoke.

    This is the half that makes the file reviewable: the diff a reviewer reads has to be
    the whole of what is in force afterwards, deletions included.
    """
    admin = _admin_field_name()
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    both = """
rules:
  - rule_id: kept
    tool_pattern: orders_lookup
    effect: allow
    reason: reading an order is safe
  - rule_id: revoked
    tool_pattern: pricing_apply
    effect: allow
    reason: granted by mistake
"""
    only_one = """
rules:
  - rule_id: kept
    tool_pattern: orders_lookup
    effect: allow
    reason: reading an order is safe
"""
    (policy_dir / "rules.yaml").write_text(both, encoding="utf-8")

    settings = _settings_type()(
        app_conninfo=empty_instance,
        profiles_dir=_SHIPPED_PROFILES,
        policy_dir=policy_dir,
        **{admin: _ADMIN_CONNINFO},
    )
    _start(settings)
    assert set(_policy_rows(empty_instance)) == {"kept", "revoked"}

    (policy_dir / "rules.yaml").write_text(only_one, encoding="utf-8")
    _start(settings)

    assert set(_policy_rows(empty_instance)) == {"kept"}, (
        "a rule deleted from the reviewed file is still in force in the database. An "
        "applier that only ever adds grants permissions nobody can revoke without SQL "
        "(docs/TASKS.md#t-f11-03)."
    )


def test_the_policy_adapter_is_the_only_thing_that_parses_the_rules() -> None:
    """`yaml` is banned outside `adapters/` (CLAUDE.md, pyproject TID251).

    Stated here as well as in the layer test because this anchor is the one that adds a
    second YAML reader to the tree, and the first one had to be caught by a guard.
    """
    source = (
        _CORE_ROOT / "src" / "agent_core" / "composition.py"
    ).read_text(encoding="utf-8")
    assert not re.search(r"^\s*import yaml", source, re.MULTILINE), (
        "composition.py imports yaml directly. Parsing belongs to the driven adapter."
    )


# ---------------------------------------------------------------------------
# t-f11-05 - a profile that parses is not a profile that works
# ---------------------------------------------------------------------------


def test_the_fraud_toolset_is_registered_so_the_shipped_profile_can_run() -> None:
    """No database needed. `fraud_analyst` parsed, got a version, and died on every turn.

    `UnknownToolsetError` at `StartTurn` step 3, because `fraud` was absent from the
    registry the provider resolves against - while the package sat implemented and tested
    in the tree. Shipping a profile no deployment can serve is the sixth instance of a
    collaborator only the tests supplied (docs/STATE.md).
    """
    container = composition.build_container(
        composition.Settings(profiles_dir=_SHIPPED_PROFILES)
    )

    fraud = container.profiles["fraud_analyst"]
    names = asyncio.run(container.tools.tool_names_for(fraud))

    assert set(names) == {
        "sql_readonly",
        "account_history",
        "case_notes_append",
        "freeze_account",
    }, (
        f"fraud_analyst resolves to {sorted(names)}. The `fraud` package is registered "
        "nowhere the composition root reads (docs/TASKS.md#t-f11-05)."
    )


def test_a_profile_naming_an_unregistered_toolset_is_refused_at_load(tmp_path: Path) -> None:
    """At LOAD, naming the profile and the toolset - never at turn three.

    A profile that parses is not a profile that works. The failure an operator gets today
    arrives after the container built, after the turn started, and after the model was
    already paid for; it names a toolset and leaves them to work out which profile asked.
    """
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "unservable.yaml").write_text(
        "id: unservable\n"
        "persona: |\n"
        "  You are a profile nobody can serve.\n"
        "model: minimax/MiniMax-M3\n"
        "toolsets:\n"
        "  - nothing_registered_under_this_name\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception) as refused:  # noqa: B017 - the TYPE is not the claim
        composition.build_container(composition.Settings(profiles_dir=profiles_dir))

    message = str(refused.value)
    assert "unservable" in message, "the refusal does not name the profile"
    assert "nothing_registered_under_this_name" in message, (
        "the refusal does not name the toolset that is missing"
    )
