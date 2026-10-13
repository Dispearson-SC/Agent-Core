"""A2: manifests are read from the addons root for exactly the listed addons (plus bridges).

Subject: adapters/driven/addons_fs/loader.py. The resolution rules are `test_addon_resolver.py`;
this file covers what reads the disk: the root, the directory per addon, the files a manifest
points at, and the environment that carries the list.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError, ManifestError
from agent_core.adapters.driven.addons_fs.loader import (
    core_version,
    default_addons_root,
    load_addons,
    load_addons_from_environment,
)


def _addon(root: Path, name: str, body: str = "", *, files: tuple[str, ...] = ()) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\n{body}", encoding="utf-8"
    )
    for relative in files:
        target = directory / relative
        if relative.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
    return directory


def _names(root: Path, enabled: list[str]) -> list[tuple[str, str]]:
    loaded = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    return [(a.manifest.name, a.source) for a in loaded]


def test_the_listed_addons_load_in_dependency_order_with_their_directory(tmp_path: Path) -> None:
    _addon(tmp_path, "base")
    _addon(tmp_path, "top", "depends: [base]\n")

    loaded = load_addons(enabled=["top", "base"], addons_root=tmp_path, core_version="0.0.0")

    assert [(a.manifest.name, a.root) for a in loaded] == [
        ("base", tmp_path / "base"),
        ("top", tmp_path / "top"),
    ]


def test_a_bridge_in_the_root_activates_without_being_listed(tmp_path: Path) -> None:
    _addon(tmp_path, "a")
    _addon(tmp_path, "b")
    _addon(tmp_path, "a_b", "depends: [a, b]\nauto_install: true\n")

    assert _names(tmp_path, ["a", "b"]) == [
        ("a", "explicit"),
        ("b", "explicit"),
        ("a_b", "auto_install"),
    ]
    assert _names(tmp_path, ["a"]) == [("a", "explicit")]


def test_an_unlisted_addon_that_is_not_a_bridge_is_never_read(tmp_path: Path) -> None:
    _addon(tmp_path, "a")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "addon.yaml").write_text("name: [unclosed", encoding="utf-8")
    _addon(tmp_path, "unrelated", "settings: not-a-mapping\n")

    assert _names(tmp_path, ["a"]) == [("a", "explicit")]


def test_a_broken_bridge_fails_startup_rather_than_staying_silently_off(tmp_path: Path) -> None:
    _addon(tmp_path, "a")
    _addon(tmp_path, "a_x", "depends: [a]\nauto_install: true\nprovides: nope\n")

    with pytest.raises(ManifestError) as caught:
        _names(tmp_path, ["a"])

    assert caught.value.addon == "a_x"


def test_files_and_hidden_directories_in_the_root_are_ignored(tmp_path: Path) -> None:
    _addon(tmp_path, "a")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".git").mkdir()

    assert _names(tmp_path, ["a"]) == [("a", "explicit")]


def test_an_enabled_addon_that_is_not_in_the_root_fails_naming_the_root(tmp_path: Path) -> None:
    with pytest.raises(AddonResolutionError) as caught:
        _names(tmp_path, ["ghost"])

    assert caught.value.addon == "ghost" and str(tmp_path) in str(caught.value)


def test_a_missing_root_fails_only_when_something_is_enabled(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"

    assert _names(missing, []) == []
    with pytest.raises(AddonResolutionError, match="nowhere"):
        _names(missing, ["a"])


def test_a_broken_enabled_manifest_names_the_addon_and_field(tmp_path: Path) -> None:
    _addon(tmp_path, "a", "depends: a\n")

    with pytest.raises(ManifestError) as caught:
        _names(tmp_path, ["a"])

    assert caught.value.addon == "a" and caught.value.field == "depends"


@pytest.mark.parametrize(
    ("body", "files", "field"),
    [
        ("provides:\n  profiles: profiles/\n", (), "provides.profiles"),
        ("provides:\n  fragments: fragments/\n", (), "provides.fragments"),
        ("provides:\n  policy: policy/rules.yaml\n", (), "provides.policy"),
        ("migrations: migrations/\n", (), "migrations"),
        ("provides:\n  profiles: profiles/\n", ("profiles",), "provides.profiles"),
    ],
)
def test_a_path_the_manifest_points_at_must_exist_with_the_right_kind(
    tmp_path: Path, body: str, files: tuple[str, ...], field: str
) -> None:
    _addon(tmp_path, "a", body, files=files)  # `profiles` as a FILE where a directory is declared

    with pytest.raises(ManifestError) as caught:
        _names(tmp_path, ["a"])

    assert caught.value.addon == "a" and caught.value.field == field


def test_paths_that_exist_are_accepted(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "a",
        "provides:\n  profiles: profiles/\n  policy: policy/rules.yaml\nmigrations: migrations/\n",
        files=("profiles/", "policy/rules.yaml", "migrations/"),
    )

    assert _names(tmp_path, ["a"]) == [("a", "explicit")]


def test_a_symlink_cannot_point_a_manifest_path_outside_the_addon(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = _addon(tmp_path / "root", "a", "provides:\n  profiles: profiles\n")
    (directory / "profiles").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManifestError, match="outside the addon directory"):
        _names(tmp_path / "root", ["a"])


# --- the environment ---------------------------------------------------------------------------


def test_the_core_version_is_the_installed_package_version() -> None:
    assert core_version() == metadata.version("agent-core")


def test_the_default_root_is_the_addons_directory_of_the_repository() -> None:
    root = default_addons_root()

    assert root.name == "addons" and root.parent == Path(__file__).resolve().parents[3]


def test_the_environment_carries_the_list_and_the_root(tmp_path: Path) -> None:
    _addon(tmp_path, "a")
    _addon(tmp_path, "b")
    env = {"AGENT_CORE_ADDONS": "b, a", "AGENT_CORE_ADDONS_DIR": str(tmp_path)}

    assert [a.manifest.name for a in load_addons_from_environment(env)] == ["b", "a"]


def test_nothing_is_enabled_when_the_environment_does_not_say_so() -> None:
    assert load_addons_from_environment({}) == ()
