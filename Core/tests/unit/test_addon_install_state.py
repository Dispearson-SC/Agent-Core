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
from agent_core.adapters.driven.addons_fs.install import AddonInstallPlan
from agent_core.adapters.driven.addons_fs.loader import load_addons


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Connection:
    """Records statements; every SELECT answers with no rows."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.statements: list[str] = []
        self._fail_on = fail_on

    def execute(self, sql: str, params: Any = None) -> _Rows:
        self.statements.append(sql)
        if self._fail_on is not None and self._fail_on in sql:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return _Rows([])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _plan(version: str, hook: Any = None) -> AddonInstallPlan:
    return AddonInstallPlan(name="shop", version=version, migrations=(), post_init=hook)


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
