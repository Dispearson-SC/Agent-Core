"""A credential the file has and the process cannot see.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-23, #t-f11-24
Covers:  agent_core/composition.py (`_export_mapped_credentials`, `Settings.from_env`,
         `_redacted_target`, `start_container`'s `MissingDatabaseError`), `.env.example`

WHY THIS FILE EXISTS
    t-f11-23: `Settings.from_env` layers `.env` under the environment for ITS OWN fields
    and exports nothing. litellm resolves MiniMax's credential by reading
    `os.environ["MINIMAX_API_KEY"]` directly, on a path that never goes through `Settings`
    at all - and this repository's `.env` carries the same value under `MINIMAX_API`. So a
    machine with a perfectly good credentials file still could not make a model call until
    a human exported `MINIMAX_API_KEY` by hand - docs/ROADMAP.md's F11 table, second row.

    t-f11-24: the checked-in `.env.example` must default to a server that actually exists
    for a fresh clone (never the remote instance), and a database missing at the wrong
    host must say WHICH host it tried - "the database does not exist" against the wrong
    server is, on its own account, the most confusing failure in this phase.

NOTHING HERE PRINTS, LOGS, COMMITS OR ASSERTS ON A CREDENTIAL VALUE. Every secret below is
synthetic, asserted only as a substring that must NOT appear, and `monkeypatch` restores
`os.environ` after every test - this module mutates the real process environment on
purpose (that is what `_export_mapped_credentials` is for) and must never leak it forward.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from agent_core import composition

_CORE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _CORE_ROOT.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _write_env_file(directory: Path, values: dict[str, str]) -> Path:
    path = directory / ".env"
    path.write_text(
        "# a credentials file, exactly as a fresh clone gets one\n"
        + "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# t-f11-23 - the name the file carries becomes the name litellm reads
# ---------------------------------------------------------------------------


def test_a_file_only_credential_becomes_visible_under_the_name_litellm_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` carries `MINIMAX_API`; litellm resolves the client's key from
    `os.environ["MINIMAX_API_KEY"]` directly (docs/FIELD-NOTES.md). A machine with only the
    file set must still be able to make a model call - nothing exported by hand.
    """
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    secret = "file-only-synthetic-minimax-key"
    env_file = _write_env_file(tmp_path, {"MINIMAX_API": secret})

    composition.Settings.from_env(environ={}, env_file=env_file)

    assert os.environ.get("MINIMAX_API_KEY") == secret, (
        "the credentials file carries MINIMAX_API and litellm reads MINIMAX_API_KEY "
        "straight from os.environ - a machine with a complete .env still cannot make a "
        "model call unless from_env maps the two (docs/TASKS.md#t-f11-23)."
    )


def test_an_operators_own_export_is_never_overwritten_by_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who exported the wire name themselves said something on purpose.

    A file silently repointing an already-set MINIMAX_API_KEY would override a value the
    operator configured deliberately - the same ordering rule from_env already keeps
    between the file and the real environment for its OWN fields.
    """
    monkeypatch.setenv("MINIMAX_API_KEY", "operator-exported-value")
    env_file = _write_env_file(tmp_path, {"MINIMAX_API": "file-value-must-not-win"})

    composition.Settings.from_env(environ={}, env_file=env_file)

    assert os.environ["MINIMAX_API_KEY"] == "operator-exported-value", (
        "the file's MINIMAX_API overwrote an operator's own MINIMAX_API_KEY export."
    )


def test_no_real_wire_name_is_touched_when_the_file_has_no_value_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mapping with nothing to map must not invent an empty credential."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    env_file = _write_env_file(tmp_path, {"AGENT_CORE_DATABASE_URL": "postgresql://x/y"})

    composition.Settings.from_env(environ={}, env_file=env_file)

    assert "MINIMAX_API_KEY" not in os.environ, (
        "MINIMAX_API_KEY appeared in the environment though the file named no MINIMAX_API "
        "value at all."
    )


def test_the_mapping_never_logs_or_prints_the_credential_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The value crosses from the file to `os.environ` and nowhere else observable."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    secret = "s3cr3t-minimax-value-that-must-never-be-echoed"
    env_file = _write_env_file(tmp_path, {"MINIMAX_API": secret})

    with caplog.at_level("DEBUG"):
        composition.Settings.from_env(environ={}, env_file=env_file)

    captured = capsys.readouterr()
    assert secret not in caplog.text, "the mapped credential value reached the log"
    assert secret not in captured.out + captured.err, (
        "the mapped credential value reached stdout/stderr"
    )


# ---------------------------------------------------------------------------
# t-f11-24 - the checked-in default points at a server that exists, and a
# database missing at the wrong one says which server it tried
# ---------------------------------------------------------------------------


def test_env_example_default_database_points_at_a_local_server() -> None:
    """The shipped default must be the server a fresh clone actually has.

    Pointing a checked-in default at a remote instance where the database was never
    created is exactly what made "the database does not exist" the most confusing failure
    in this phase - a fresh clone cannot tell a missing database from a wrong host.
    """
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^AGENT_CORE_DATABASE_URL=(\S+)$", text, re.MULTILINE)
    assert match, ".env.example sets no default for AGENT_CORE_DATABASE_URL at all"

    parsed = conninfo_to_dict(match.group(1))
    assert parsed.get("host") in ("localhost", "127.0.0.1"), (
        f".env.example's default AGENT_CORE_DATABASE_URL names host {parsed.get('host')!r}, "
        "not the local server a fresh clone actually has (docs/TASKS.md#t-f11-24)."
    )


def test_env_example_ships_no_secret_value() -> None:
    """A clone copies this file. Every credential must ship blank, not merely example-y."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for name in ("MINIMAX_API", "MINIMAX_API_BASE", "GEMINI_API_KEY", "TELEGRAM_BOT"):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith(f"{name}="):
                assert stripped == f"{name}=", (
                    f".env.example ships a non-blank value for {name!r} on an "
                    f"uncommented line: {stripped!r}. NO SECRET VALUE may live in this "
                    "file (docs/TASKS.md#t-f11-24)."
                )


def test_redacted_target_names_the_server_and_never_a_credential() -> None:
    """t-f11-24's `_redacted_target`: host and port, always; a password, never.

    Pure function, no database needed - `_database_name` in the module already parses
    conninfo this way for t-f11-02, so this is the sibling that must do it for the
    OPERATOR-FACING host/port rather than the database name.
    """
    conninfo = "postgresql://produser:should-never-appear-773@db.example.test:6543/appdb"

    target = composition._redacted_target(conninfo)

    assert "should-never-appear-773" not in target, "a password reached the exception text"
    assert "produser" not in target, "a username reached the exception text"
    assert "db.example.test" in target, "the redacted target does not name the host"
    assert "6543" in target, "the redacted target does not name the port"


@_needs_postgres
def test_a_missing_database_names_the_host_and_port_it_tried() -> None:
    """A reachable server with no admin URL and a database that is not there.

    "the database does not exist" reads the same whether it is missing on the host you
    meant or you are pointed at the wrong host entirely - t-f11-24 adds the SERVER to the
    message, not just the database name that docs/TASKS.md#t-f11-02 already named.
    """
    missing_db = re.sub(
        r"/[^/?]+(\?.*)?$", r"/agent_core_credential_mapping_test_missing\1", _ADMIN_CONNINFO
    )
    settings = composition.Settings(app_conninfo=missing_db)

    with pytest.raises(Exception) as refused:  # noqa: B017 - the TYPE is not the claim
        asyncio.run(composition.start_container(settings))

    message = str(refused.value)
    parsed = conninfo_to_dict(missing_db)
    host = str(parsed.get("host") or "localhost")
    port = str(parsed.get("port") or "5432")
    assert host in message, (
        f"a missing database at {host}:{port} failed without naming the host it tried - "
        "an operator cannot tell a missing database from a wrong server "
        "(docs/TASKS.md#t-f11-24)."
    )
    assert port in message, (
        f"a missing database at {host}:{port} failed without naming the port it tried "
        "(docs/TASKS.md#t-f11-24)."
    )


@_needs_postgres
def test_an_unreachable_server_is_not_told_to_create_a_database_there() -> None:
    """A server that is genuinely down must get its own error back, unwrapped.

    Telling an operator to `CREATE DATABASE` on a host that is not listening sends them to
    fix the wrong thing entirely.
    """
    unreachable = (
        "postgresql://postgres:postgres@localhost:1/agent_core_unreachable_test"
        "?connect_timeout=2"
    )
    settings = composition.Settings(app_conninfo=unreachable)

    with pytest.raises(Exception) as refused:  # noqa: B017 - the TYPE is not the claim
        asyncio.run(composition.start_container(settings))

    assert not isinstance(refused.value, composition.MissingDatabaseError), (
        "an unreachable server was turned into a MissingDatabaseError telling the "
        "operator to CREATE DATABASE on a host that is not even listening."
    )
