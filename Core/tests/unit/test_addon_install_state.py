"""A7b (D81): the install pass's version handling and its database failures, without a database.

Subject: adapters/driven/addons_fs/install.py `_install_one`, `_refuse_downgrades` and
`install_addons`, driven with a small fake connection. The real-database behavior is
tests/integration/test_addon_install.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.addons_fs import install
from agent_core.adapters.driven.addons_fs.errors import AddonInstallError
from agent_core.adapters.driven.addons_fs.install import AddonInstallPlan, MigrationFile
from agent_core.adapters.driven.addons_fs.loader import load_addons


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Connection:
    """Records statements; every SELECT answers with no rows."""

    def __init__(self, fail_on: str | None = None, error: Exception | None = None) -> None:
        self.statements: list[str] = []
        self._fail_on = fail_on
        self._error = error or psycopg.OperationalError("server closed the connection unexpectedly")

    def execute(self, sql: str, params: Any = None) -> _Rows:
        self.statements.append(sql)
        if self._fail_on is not None and self._fail_on in sql:
            raise self._error
        return _Rows([])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _plan(
    version: str, hook: Any = None, migrations: tuple[MigrationFile, ...] = ()
) -> AddonInstallPlan:
    return AddonInstallPlan(name="shop", version=version, migrations=migrations, post_init=hook)


def _run(connection: _Connection, plan: AddonInstallPlan) -> AddonInstallError:
    """Install `plan` over `connection` (a fresh install) and return the error it stops with."""
    with pytest.raises(AddonInstallError) as caught:
        install._install_one(connection, plan, None)  # type: ignore[arg-type]
    return caught.value


_SQL_ERROR = psycopg.errors.UndefinedTable('relation "nope" does not exist')
_ONE_FILE = (MigrationFile("0001_t.sql", "CREATE TABLE t (n INT);"),)


@pytest.mark.parametrize(
    ("recorded", "manifest"), [("1.0", "1.0.0"), ("1.0.0", "1.0"), ("2.1.0", "2.1")]
)
def test_equal_versions_written_differently_are_unchanged_and_run_no_hook(
    recorded: str, manifest: str
) -> None:
    ran: list[object] = []

    result = install._install_one(_Connection(), _plan(manifest, ran.append), recorded)  # type: ignore[arg-type]

    assert result.action == "unchanged" and ran == []


def test_versions_are_compared_by_number_not_by_text() -> None:
    install._refuse_downgrades([_plan("1.10")], {"shop": "1.9"})  # an upgrade: no error

    with pytest.raises(AddonInstallError, match="downgrade"):
        install._refuse_downgrades([_plan("1.9")], {"shop": "1.10"})


def test_a_recorded_version_that_is_not_a_version_stops_startup_clearly() -> None:
    with pytest.raises(AddonInstallError, match="is not a version"):
        install._refuse_downgrades([_plan("1.0.0")], {"shop": "banana"})


def test_a_dropped_connection_while_installing_is_an_install_error_naming_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install.psycopg, "connect", lambda *a, **k: _Connection(fail_on="SELECT file")
    )

    with pytest.raises(AddonInstallError) as caught:
        install._install_sync("postgresql://x/y", [_plan("1.0.0")])

    assert caught.value.addon == "shop" and caught.value.field == "database"
    assert "OperationalError" in str(caught.value)


def test_a_database_that_cannot_be_reached_is_an_install_error_naming_the_step(
    tmp_path: Path,
) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "addon.yaml").write_text("name: shop\nversion: 1.0.0\ncore: '>=0'\n")
    addons = load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")

    with pytest.raises(AddonInstallError, match="connect") as caught:
        asyncio.run(
            install.install_addons("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1", addons)
        )

    assert caught.value.field == "database"


# --- A8b: advisories of the slice 24 review ------------------------------------------------------


@pytest.mark.parametrize(
    ("fail_on", "step"),
    [("pg_advisory_lock", "take the install lock"), ("SELECT name", "read the recorded")],
)
def test_a_dropped_connection_names_the_pass_step_that_failed(
    monkeypatch: pytest.MonkeyPatch, fail_on: str, step: str
) -> None:
    monkeypatch.setattr(install.psycopg, "connect", lambda *a, **k: _Connection(fail_on=fail_on))

    with pytest.raises(AddonInstallError) as caught:
        install._install_sync("postgresql://x/y", [_plan("1.0.0")])

    assert caught.value.field == "database" and step in caught.value.reason
    assert caught.value.addon is None  # the pass, not one addon


def test_a_dropped_connection_while_reading_applied_migrations_names_that_step() -> None:
    error = _run(_Connection(fail_on="SELECT file"), _plan("1.0.0"))

    assert error.field == "database" and "read the applied migrations" in error.reason


def test_a_dropped_connection_in_a_migration_is_a_database_failure_not_a_sql_defect() -> None:
    error = _run(_Connection(fail_on="CREATE TABLE"), _plan("1.0.0", migrations=_ONE_FILE))

    assert error.field == "database" and "0001_t.sql" in error.reason
    assert "rolled back" not in error.reason  # the connection died: what happened is unknown


def test_a_sql_error_in_a_migration_is_a_migration_defect_not_a_database_failure() -> None:
    connection = _Connection(fail_on="CREATE TABLE", error=_SQL_ERROR)

    error = _run(connection, _plan("1.0.0", migrations=_ONE_FILE))

    assert error.field == "migrations" and "0001_t.sql" in error.reason
    assert "rolled back" in error.reason and "UndefinedTable" in error.reason


def test_a_server_side_operational_error_is_not_mistaken_for_a_lost_connection() -> None:
    disk_full = psycopg.errors.DiskFull("could not extend file")  # an OperationalError, class 53

    error = _run(
        _Connection(fail_on="CREATE TABLE", error=disk_full), _plan("1.0.0", migrations=_ONE_FILE)
    )

    assert error.field == "migrations" and "DiskFull" in error.reason


def test_a_dropped_connection_inside_the_hook_is_a_database_failure_naming_post_init() -> None:
    def hook(context: Any) -> None:
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    error = _run(_Connection(), _plan("1.0.0", hook))

    assert error.field == "database" and "run post_init" in error.reason


def test_a_hook_that_raises_is_still_a_hook_failure() -> None:
    def hook(context: Any) -> None:
        raise RuntimeError("seed failed")

    error = _run(_Connection(), _plan("1.0.0", hook))

    assert error.field == "hooks.post_init" and "seed failed" in error.reason


def test_a_dropped_connection_while_recording_the_version_is_not_blamed_on_the_hook() -> None:
    ran: list[object] = []

    error = _run(_Connection(fail_on="INSERT INTO addons "), _plan("1.0.0", ran.append))

    assert ran  # the hook did run and succeeded
    assert error.field == "database" and "record the installed version" in error.reason
    assert "post_init for version" not in error.reason


def test_a_sql_error_while_recording_the_version_is_not_blamed_on_the_hook_or_the_database() -> (
    None
):
    connection = _Connection(fail_on="INSERT INTO addons ", error=_SQL_ERROR)

    error = _run(connection, _plan("1.0.0", lambda context: None))

    assert error.field not in {"database", "hooks.post_init"}
    assert "record the installed version" in error.reason and "UndefinedTable" in error.reason


def test_a_sql_error_taking_the_lock_is_not_reported_as_a_database_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install.psycopg,
        "connect",
        lambda *a, **k: _Connection(fail_on="pg_advisory_lock", error=_SQL_ERROR),
    )

    with pytest.raises(AddonInstallError) as caught:
        install._install_sync("postgresql://x/y", [_plan("1.0.0")])

    assert caught.value.field != "database" and "take the install lock" in caught.value.reason


def test_a_hook_that_returns_a_coroutine_is_a_failure_not_a_recorded_install() -> None:
    async def work() -> None: ...

    def hook(context: Any) -> Any:
        return work()  # a decorator that hides the coroutine function from inspection

    error = _run(_Connection(), _plan("1.0.0", hook))

    assert error.field == "hooks.post_init" and "coroutine" in error.reason
