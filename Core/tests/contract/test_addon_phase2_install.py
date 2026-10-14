"""Phase 2 exit criterion (A8, D82), part 2: a sample addon installs, against a real Postgres.

`start_container` is the real startup path: Core's schema, then the enabled addons' install state
(migration + `post_init`, recorded), then the policy reconciliation that includes the addons'
rules. The unit and integration suites cover each step; this proves the whole story with the
sample addons shipped in `addons/`, including the bridge that activates by itself.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.addons_fs.install import list_installed
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.persistence_pg.migrations import apply_all_migrations
from tests.fakes import peer_world

pytestmark = [
    pytest.mark.contract,
    pytest.mark.phase("2"),
    pytest.mark.skipif(
        not peer_world.postgres_reachable(), reason="no reachable Postgres instance"
    ),
]

SAMPLES = Path(__file__).resolve().parents[3] / "addons"
DATABASE = "agent_core_addon_phase2_contract"
ENABLED = ["sample_base", "sample_extra", "sample_shout", "sample_install"]


@pytest.fixture
def conninfo() -> str:
    with psycopg.connect(peer_world.ADMIN_CONNINFO, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{DATABASE}"')
    url = re.sub(r"/[^/?]+(\?.*)?$", rf"/{DATABASE}\1", peer_world.ADMIN_CONNINFO)
    asyncio.run(apply_all_migrations(url))
    return url


@pytest.fixture
def addons_root(tmp_path: Path) -> Path:
    """A copy of the shipped samples, so a test can 'upgrade' one without touching the repo."""
    root = tmp_path / "addons"
    shutil.copytree(SAMPLES, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _start(conninfo: str, root: Path, enabled: list[str]) -> None:
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    container = asyncio.run(
        composition.start_container(composition.Settings(app_conninfo=conninfo, addons=addons))
    )
    container.domain_pool.close()
    container.audit_pool.close()


def _column(conninfo: str, sql: str) -> list[str]:
    with psycopg.connect(conninfo) as connection:
        return [str(row[0]) for row in connection.execute(sql).fetchall()]


def test_a_sample_addon_installs_its_migration_and_post_init_and_is_recorded(
    conninfo: str, addons_root: Path
) -> None:
    _start(conninfo, addons_root, ENABLED)

    assert _column(
        conninfo, "SELECT file FROM addon_migrations WHERE addon = 'sample_install'"
    ) == ["0001_notes.sql"]
    assert _column(conninfo, "SELECT note FROM sample_install_notes") == [
        "post_init: first install -> 1.0.0"
    ]
    installed = {a.name: a.version for a in asyncio.run(list_installed(conninfo))}
    assert installed["sample_install"] == "1.0.0"


def test_the_bridge_installs_with_its_dependencies_and_is_recorded_like_any_addon(
    conninfo: str, addons_root: Path
) -> None:
    _start(conninfo, addons_root, ENABLED)  # sample_bridge is not in the list

    installed = {a.name for a in asyncio.run(list_installed(conninfo))}
    assert installed == {*ENABLED, "sample_bridge"}


def test_the_bridge_is_not_installed_when_a_dependency_is_missing(
    conninfo: str, addons_root: Path
) -> None:
    _start(conninfo, addons_root, ["sample_base"])

    assert {a.name for a in asyncio.run(list_installed(conninfo))} == {"sample_base"}


def test_the_addons_policy_rules_reach_the_database_with_the_startup(
    conninfo: str, addons_root: Path
) -> None:
    _start(conninfo, addons_root, ENABLED)

    assert "sample-base-allow-echo" in _column(conninfo, "SELECT rule_id FROM policy_rules")


def test_a_second_start_changes_nothing(conninfo: str, addons_root: Path) -> None:
    _start(conninfo, addons_root, ENABLED)
    before = _column(conninfo, "SELECT name || version || installed_at FROM addons ORDER BY name")
    notes = _column(conninfo, "SELECT note || created_at FROM sample_install_notes")

    _start(conninfo, addons_root, ENABLED)

    assert (
        _column(conninfo, "SELECT name || version || installed_at FROM addons ORDER BY name")
        == before
    )
    assert _column(conninfo, "SELECT note || created_at FROM sample_install_notes") == notes


def test_a_version_bump_runs_only_the_new_migration_and_post_init_again(
    conninfo: str, addons_root: Path
) -> None:
    _start(conninfo, addons_root, ENABLED)
    directory = addons_root / "sample_install"
    manifest = directory / "addon.yaml"
    manifest.write_text(manifest.read_text().replace("version: 1.0.0", "version: 1.1.0"))
    (directory / "migrations" / "0002_label.sql").write_text(
        "ALTER TABLE sample_install_notes ADD COLUMN label TEXT;"
    )

    _start(conninfo, addons_root, ENABLED)

    assert _column(
        conninfo, "SELECT file FROM addon_migrations WHERE addon = 'sample_install' ORDER BY file"
    ) == [
        "0001_notes.sql",
        "0002_label.sql",
    ]
    assert _column(conninfo, "SELECT note FROM sample_install_notes ORDER BY note") == [
        "post_init: 1.0.0 -> 1.1.0",
        "post_init: first install -> 1.0.0",
    ]
    assert _column(conninfo, "SELECT version FROM addons WHERE name = 'sample_install'") == [
        "1.1.0"
    ]
