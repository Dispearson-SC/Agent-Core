"""A7 (D80): install state - the `addons` table, versioned migrations and the post_init hook.

Phase:   Phase 2 (addon system)
Covers:  adapters/driven/addons_fs/install.py, persistence_pg/addons_migration.py

THE RULES UNDER TEST. A newly enabled addon runs its `migrations/*.sql` in name order, each once
and each in its own transaction, then `hooks.post_init` once; its version is recorded only after
all of that succeeded. A restart changes nothing. A version bump runs only the new files and the
hook again (with the previous version). A recorded version newer than the manifest's refuses
startup before anything runs. A failing file or hook stops startup naming addon and file, and
leaves the failed step undone and retryable. Two processes starting together do each step once.
Removing an addon from the list leaves its rows. No addons: nothing but the empty table.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path

import psycopg
import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonInstallError
from agent_core.adapters.driven.addons_fs.install import install_addons, list_installed
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.persistence_pg.migrations import apply_all_migrations

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_APP_DATABASE = "agent_core_addon_install_test"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")


@pytest.fixture
def conninfo() -> str:
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{_APP_DATABASE}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{_APP_DATABASE}"')
    url = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_APP_DATABASE}\1", _ADMIN_CONNINFO)
    asyncio.run(apply_all_migrations(url))
    return url


HOOK = """
from pathlib import Path

def post_init(context):
    log = Path(__file__).parent / "calls.log"
    with log.open("a") as handle:
        handle.write(f"{context.name} {context.previous_version} {context.version}\\n")
"""


def _addon(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    depends: str = "",
    sql: dict[str, str] | None = None,
    hook: str | None = None,
) -> None:
    """(Re)write an addon directory. Rewriting is how a test 'upgrades' it."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = f"name: {name}\nversion: {version}\ncore: '>=0'\n{depends}"
    if sql is not None:
        manifest += "migrations: migrations/\n"
        (directory / "migrations").mkdir(exist_ok=True)
        for file, text in sql.items():
            (directory / "migrations" / file).write_text(text, encoding="utf-8")
    if hook is not None:
        manifest += "hooks:\n  post_init: hooks:post_init\n"
        (directory / "hooks.py").write_text(hook, encoding="utf-8")
    (directory / "addon.yaml").write_text(manifest, encoding="utf-8")


def _install(root: Path, conninfo: str, enabled: list[str]):  # type: ignore[no-untyped-def]
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    return asyncio.run(install_addons(conninfo, addons))


def _calls(root: Path, name: str) -> list[str]:
    log = root / name / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _one(conninfo: str, sql: str) -> object:
    with psycopg.connect(conninfo) as connection:
        row = connection.execute(sql).fetchone()
    return None if row is None else row[0]


def _exists(conninfo: str, table: str) -> bool:
    return _one(conninfo, f"SELECT to_regclass('public.{table}')") is not None


def test_with_no_addons_nothing_is_installed_and_the_table_is_empty(conninfo: str) -> None:
    results = asyncio.run(install_addons(conninfo, ()))

    assert results == ()
    assert _one(conninfo, "SELECT count(*) FROM addons") == 0
    assert asyncio.run(list_installed(conninfo)) == ()


def test_a_new_addon_runs_its_migrations_in_order_then_its_hook_and_is_recorded(
    tmp_path: Path, conninfo: str
) -> None:
    _addon(
        tmp_path,
        "shop",
        sql={
            "0002_rows.sql": "INSERT INTO shop_items (name) VALUES ('from-0002');",
            "0001_table.sql": "CREATE TABLE shop_items (name TEXT NOT NULL);",
        },
        hook=HOOK,
    )

    (result,) = _install(tmp_path, conninfo, ["shop"])

    assert (result.name, result.action, result.previous_version) == ("shop", "installed", None)
    assert result.applied == ("0001_table.sql", "0002_rows.sql")
    assert _one(conninfo, "SELECT name FROM shop_items") == "from-0002"
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0"]
    (installed,) = asyncio.run(list_installed(conninfo))
    assert (installed.name, installed.version) == ("shop", "1.0.0")
    assert installed.installed_at is not None and installed.upgraded_at is None
    assert _one(conninfo, "SELECT count(*) FROM addon_migrations WHERE addon = 'shop'") == 2


def test_a_restart_changes_nothing(tmp_path: Path, conninfo: str) -> None:
    _addon(tmp_path, "shop", sql={"0001_t.sql": "CREATE TABLE shop_items (n INT);"}, hook=HOOK)
    _install(tmp_path, conninfo, ["shop"])
    before = asyncio.run(list_installed(conninfo))

    (again,) = _install(tmp_path, conninfo, ["shop"])

    assert (again.action, again.applied) == ("unchanged", ())
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0"]
    assert asyncio.run(list_installed(conninfo)) == before


def test_an_upgrade_runs_only_the_new_files_and_the_hook_with_the_previous_version(
    tmp_path: Path, conninfo: str
) -> None:
    _addon(tmp_path, "shop", sql={"0001_t.sql": "CREATE TABLE shop_items (n INT);"}, hook=HOOK)
    _install(tmp_path, conninfo, ["shop"])

    _addon(
        tmp_path,
        "shop",
        version="1.1.0",
        sql={
            "0001_t.sql": "CREATE TABLE shop_items (n INT);",
            "0002_col.sql": "ALTER TABLE shop_items ADD COLUMN label TEXT;",
        },
        hook=HOOK,
    )
    (result,) = _install(tmp_path, conninfo, ["shop"])

    assert (result.action, result.previous_version) == ("upgraded", "1.0.0")
    assert result.applied == ("0002_col.sql",)
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0", "shop 1.0.0 1.1.0"]
    (installed,) = asyncio.run(list_installed(conninfo))
    assert installed.version == "1.1.0" and installed.upgraded_at is not None


def test_a_recorded_version_newer_than_the_manifest_refuses_startup_before_anything_runs(
    tmp_path: Path, conninfo: str
) -> None:
    _addon(tmp_path, "shop", version="2.0.0")
    _install(tmp_path, conninfo, ["shop"])
    _addon(tmp_path, "shop", version="1.0.0")
    _addon(tmp_path, "other", sql={"0001_t.sql": "CREATE TABLE other_t (n INT);"})

    with pytest.raises(AddonInstallError, match="2.0.0") as caught:
        _install(tmp_path, conninfo, ["other", "shop"])

    assert caught.value.addon == "shop" and "1.0.0" in str(caught.value)
    assert not _exists(conninfo, "other_t")  # nothing ran, not even for an addon that was fine
    assert [a.name for a in asyncio.run(list_installed(conninfo))] == ["shop"]


def test_a_failing_migration_stops_startup_naming_addon_and_file_and_is_retryable(
    tmp_path: Path, conninfo: str
) -> None:
    _addon(
        tmp_path,
        "shop",
        sql={
            "0001_ok.sql": "CREATE TABLE shop_a (n INT);",
            "0002_bad.sql": "CREATE TABLE shop_b (n INT); SELECT * FROM no_such_table;",
        },
        hook=HOOK,
    )

    with pytest.raises(AddonInstallError, match="0002_bad.sql") as caught:
        _install(tmp_path, conninfo, ["shop"])

    assert caught.value.addon == "shop"
    assert _exists(conninfo, "shop_a") and not _exists(
        conninfo, "shop_b"
    )  # the bad file rolled back
    assert _one(conninfo, "SELECT count(*) FROM addon_migrations WHERE addon = 'shop'") == 1
    assert asyncio.run(list_installed(conninfo)) == ()  # not recorded as installed
    assert _calls(tmp_path, "shop") == []  # the hook waits for every file

    _addon(tmp_path, "shop", sql={"0002_bad.sql": "CREATE TABLE shop_b (n INT);"}, hook=HOOK)
    (result,) = _install(tmp_path, conninfo, ["shop"])

    assert result.applied == ("0002_bad.sql",)  # 0001 is not run a second time
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0"]


def test_a_failing_hook_leaves_the_version_unrecorded_and_runs_again_next_start(
    tmp_path: Path, conninfo: str
) -> None:
    broken = """
def post_init(context):
    context.connection.execute("INSERT INTO shop_items VALUES ('half')")
    raise RuntimeError("seed failed")
"""
    _addon(tmp_path, "shop", sql={"0001_t.sql": "CREATE TABLE shop_items (n TEXT);"}, hook=broken)

    with pytest.raises(AddonInstallError, match="post_init") as caught:
        _install(tmp_path, conninfo, ["shop"])

    assert caught.value.addon == "shop" and "seed failed" in str(caught.value)
    assert asyncio.run(list_installed(conninfo)) == ()
    assert _one(conninfo, "SELECT count(*) FROM shop_items") == 0  # the hook's writes rolled back
    assert _exists(conninfo, "shop_items")  # the migration that came first stays

    _addon(tmp_path, "shop", sql={"0001_t.sql": "CREATE TABLE shop_items (n TEXT);"}, hook=HOOK)
    (result,) = _install(tmp_path, conninfo, ["shop"])

    assert result.action == "installed" and result.applied == ()
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0"]


def test_dependencies_install_first_so_a_dependent_migration_can_use_their_tables(
    tmp_path: Path, conninfo: str
) -> None:
    _addon(tmp_path, "base", sql={"0001_t.sql": "CREATE TABLE base_t (n INT PRIMARY KEY);"})
    _addon(
        tmp_path,
        "top",
        depends="depends: [base]\n",
        sql={"0001_t.sql": "CREATE TABLE top_t (n INT REFERENCES base_t (n));"},
    )

    results = _install(tmp_path, conninfo, ["top", "base"])

    assert [r.name for r in results] == ["base", "top"]
    assert _exists(conninfo, "top_t")


def test_an_addon_removed_from_the_list_keeps_its_rows(tmp_path: Path, conninfo: str) -> None:
    _addon(tmp_path, "shop", sql={"0001_t.sql": "CREATE TABLE shop_items (n INT);"})
    _install(tmp_path, conninfo, ["shop"])

    assert _install(tmp_path, conninfo, []) == ()

    assert [a.name for a in asyncio.run(list_installed(conninfo))] == ["shop"]
    assert _exists(conninfo, "shop_items")
    assert _one(conninfo, "SELECT count(*) FROM addon_migrations WHERE addon = 'shop'") == 1


def test_processes_starting_at_once_run_each_step_exactly_once(
    tmp_path: Path, conninfo: str
) -> None:
    slow = HOOK.replace(
        "def post_init(context):", "def post_init(context):\n    import time\n    time.sleep(0.4)"
    )
    _addon(
        tmp_path,
        "shop",
        sql={"0001_t.sql": "CREATE TABLE shop_items (n INT);"},  # not idempotent on purpose
        hook=slow,
    )
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def start() -> None:
        try:
            addons = load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")
            barrier.wait()
            asyncio.run(install_addons(conninfo, addons))
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0"]
    assert _one(conninfo, "SELECT count(*) FROM addons") == 1
    assert _one(conninfo, "SELECT count(*) FROM addon_migrations") == 1


def test_startup_installs_the_enabled_addons_and_a_second_start_is_a_no_op(
    tmp_path: Path, conninfo: str
) -> None:
    from agent_core import composition

    _addon(tmp_path, "shop", sql={"0001_t.sql": "CREATE TABLE shop_items (n INT);"}, hook=HOOK)
    addons = load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")
    settings = composition.Settings(app_conninfo=conninfo, addons=addons)

    asyncio.run(composition.start_container(settings))
    asyncio.run(composition.start_container(settings))

    assert _exists(conninfo, "shop_items")
    assert _calls(tmp_path, "shop") == ["shop None 1.0.0"]
    assert [a.name for a in asyncio.run(list_installed(conninfo))] == ["shop"]


def test_startup_with_no_addons_leaves_the_install_tables_empty(conninfo: str) -> None:
    from agent_core import composition

    asyncio.run(composition.start_container(composition.Settings(app_conninfo=conninfo)))

    assert _one(conninfo, "SELECT count(*) FROM addons") == 0
    assert _one(conninfo, "SELECT count(*) FROM addon_migrations") == 0


def test_startup_stops_on_a_failing_addon_migration(tmp_path: Path, conninfo: str) -> None:
    from agent_core import composition

    _addon(tmp_path, "shop", sql={"0001_bad.sql": "SELECT * FROM nowhere;"})
    addons = load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")

    with pytest.raises(AddonInstallError, match="0001_bad.sql"):
        asyncio.run(
            composition.start_container(composition.Settings(app_conninfo=conninfo, addons=addons))
        )


def test_addons_list_reads_the_table_and_the_enabled_manifests(
    tmp_path: Path, conninfo: str
) -> None:
    import argparse

    from agent_core import composition
    from agent_core.adapters.driving.cli.addons import run_addons

    _addon(tmp_path, "old")
    _addon(tmp_path, "shop", version="1.1.0")
    _install(tmp_path, conninfo, ["old"])
    _addon(tmp_path, "shop", version="1.0.0")
    _install(tmp_path, conninfo, ["shop"])
    _addon(tmp_path, "shop", version="1.1.0")
    enabled = load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")
    lines: list[str] = []

    code = run_addons(
        argparse.Namespace(action="list"),
        composition.Settings(app_conninfo=conninfo, addons=enabled),
        out=lines.append,
    )

    assert code == 0
    assert any(line.startswith("old") and "kept" in line for line in lines)
    assert any(line.startswith("shop") and "upgrades at the next start" in line for line in lines)
