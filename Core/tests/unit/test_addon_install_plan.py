"""A7 (D80): what an addon's install steps are, read from disk before any database is touched.

Subject: adapters/driven/addons_fs/install.py `plan_installs`, the manifest's `migrations/`
directory and `hooks.post_init`, and the `0044_addons` migration text. The database half
(applied once, recorded, locked, rolled back) is tests/integration/test_addon_install.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonInstallError
from agent_core.adapters.driven.addons_fs.install import plan_installs
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.addons_migration import ADDONS_MIGRATION


def _addon(
    root: Path,
    name: str,
    *,
    body: str = "",
    sql: dict[str, str] | None = None,
    hooks: str | None = None,
    version: str = "1.0.0",
) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    extra = body
    if sql is not None:
        extra += "migrations: migrations/\n"
        (directory / "migrations").mkdir()
        for file, text in sql.items():
            (directory / "migrations" / file).write_text(text, encoding="utf-8")
    if hooks is not None:
        extra += "hooks:\n  post_init: hooks:post_init\n"
        (directory / "hooks.py").write_text(hooks, encoding="utf-8")
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: {version}\ncore: '>=0'\n{extra}", encoding="utf-8"
    )


def _plans(root: Path, enabled: list[str]):  # type: ignore[no-untyped-def]
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    return plan_installs(addons)


def test_an_addon_with_no_install_steps_plans_nothing_to_run(tmp_path: Path) -> None:
    _addon(tmp_path, "plain")

    (plan,) = _plans(tmp_path, ["plain"])

    assert plan.name == "plain" and plan.version == "1.0.0"
    assert plan.migrations == () and plan.post_init is None


def test_migration_files_are_ordered_by_name_and_only_sql_files_count(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        sql={
            "0010_later.sql": "SELECT 10;",
            "0002_second.sql": "SELECT 2;",
            "0001_first.sql": "SELECT 1;",
            "README.md": "notes",
        },
    )

    (plan,) = _plans(tmp_path, ["shop"])

    assert [(m.name, m.sql) for m in plan.migrations] == [
        ("0001_first.sql", "SELECT 1;"),
        ("0002_second.sql", "SELECT 2;"),
        ("0010_later.sql", "SELECT 10;"),
    ]


def test_plans_come_in_activation_order(tmp_path: Path) -> None:
    _addon(tmp_path, "base")
    _addon(tmp_path, "top", body="depends: [base]\n")

    assert [p.name for p in _plans(tmp_path, ["top", "base"])] == ["base", "top"]


def test_the_post_init_hook_is_imported_from_the_addon_directory(tmp_path: Path) -> None:
    _addon(tmp_path, "shop", hooks="def post_init(context):\n    return 'ran'\n")

    (plan,) = _plans(tmp_path, ["shop"])

    assert plan.post_init is not None and plan.post_init(None) == "ran"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("hooks", "message"),
    [
        ("x = 1\n", "post_init"),
        ("def post_init(:\n", "failed to import"),
        ("post_init = 3\n", "post_init"),
    ],
    ids=["no-function", "does-not-import", "not-callable"],
)
def test_a_hook_that_is_missing_or_broken_stops_startup_naming_the_addon(
    tmp_path: Path, hooks: str, message: str
) -> None:
    _addon(tmp_path, "shop", hooks=hooks)

    with pytest.raises(AddonInstallError, match=message) as caught:
        _plans(tmp_path, ["shop"])

    assert caught.value.addon == "shop" and caught.value.field == "hooks.post_init"


def test_a_hook_file_that_does_not_exist_stops_startup(tmp_path: Path) -> None:
    _addon(tmp_path, "shop", hooks="def post_init(context): ...\n")
    (tmp_path / "shop" / "hooks.py").unlink()

    with pytest.raises(AddonInstallError, match="hooks.py"):
        _plans(tmp_path, ["shop"])


def test_an_unreadable_migration_file_stops_startup_naming_addon_and_file(
    tmp_path: Path,
) -> None:
    _addon(tmp_path, "shop", sql={"0001_a.sql": ""})
    (tmp_path / "shop" / "migrations" / "0001_a.sql").write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(AddonInstallError, match="0001_a.sql") as caught:
        _plans(tmp_path, ["shop"])

    assert caught.value.addon == "shop"


def test_the_addons_migration_creates_the_install_state_and_never_touches_audit() -> None:
    sql = ADDONS_MIGRATION.sql
    assert ADDONS_MIGRATION.id == "0044_addons"
    assert "CREATE TABLE IF NOT EXISTS addons" in sql
    assert "CREATE TABLE IF NOT EXISTS addon_migrations" in sql
    assert ADDONS_MIGRATION in migrations.discover_app_migrations()


# --- A7b: advisories of the slice 23 review ------------------------------------------------------


@pytest.mark.parametrize(
    "hooks",
    [
        "async def post_init(context):\n    pass\n",
        "import functools\nasync def _inner(context):\n    pass\n"
        "post_init = functools.partial(_inner)\n",
    ],
    ids=["async-def", "partial-of-async"],
)
def test_an_async_post_init_is_refused_at_load_instead_of_recorded_without_running(
    tmp_path: Path, hooks: str
) -> None:
    _addon(tmp_path, "shop", hooks=hooks)

    with pytest.raises(AddonInstallError, match="async") as caught:
        _plans(tmp_path, ["shop"])

    assert caught.value.addon == "shop" and caught.value.field == "hooks.post_init"
    assert "def post_init" in caught.value.reason  # says what to write instead
