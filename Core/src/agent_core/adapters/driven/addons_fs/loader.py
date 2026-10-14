"""Load the enabled addons from disk (Phase 2, A2; roadmap 5.4 steps 1-3).

Phase:   Phase 2 (addon system)
Tests:   Core/tests/unit/test_addon_loader.py
Implements: no port - startup wiring (D70, D71).

THE ONE FUNCTION THE COMPOSITION ROOT CALLS
    `load_addons_from_environment(env)` returns the active addons, validated and in activation
    order, each with its directory and (A3) its validated settings. Registering what they provide
    into the root is A4; nothing here touches the toolset registry, the policy or the database.

WHAT IS READ
    The listed addons' `addon.yaml`, and - to find bridges - the `addon.yaml` of every OTHER
    directory in the root, but only far enough to see `auto_install: true`. Only such a candidate is
    then validated in full. An unlisted addon that is not a bridge is never parsed, never imported,
    never checked; it cannot enable itself or break startup. No addon CODE is imported here (hooks
    are A7).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Final, Literal

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError, ManifestError
from agent_core.adapters.driven.addons_fs.manifest import (
    MANIFEST_FILENAME,
    AddonManifest,
    load_manifest,
)
from agent_core.adapters.driven.addons_fs.resolver import parse_enabled, resolve_addons
from agent_core.adapters.driven.addons_fs.settings import read_addons_config, resolve_settings

__all__ = [
    "ResolvedAddon",
    "core_version",
    "default_addons_root",
    "load_addons",
    "load_addons_from_environment",
]

ENV_ADDONS: Final = "AGENT_CORE_ADDONS"
ENV_ADDONS_DIR: Final = "AGENT_CORE_ADDONS_DIR"
ENV_ADDONS_CONFIG: Final = "AGENT_CORE_ADDONS_CONFIG"

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[6]


@dataclass(frozen=True)
class ResolvedAddon:
    """An active addon: its manifest, where it lives, how it became active and its settings."""

    manifest: AddonManifest
    root: Path
    source: Literal["explicit", "auto_install"]
    settings: Mapping[str, Any] = field(default_factory=dict)


def core_version() -> str:
    """The installed Core version, the one every manifest's `core` range is checked against."""
    return metadata.version("agent-core")


def default_addons_root() -> Path:
    """`<repository>/addons`: the monorepo location (user decision 2026-10-13)."""
    return _REPOSITORY_ROOT / "addons"


def _is_bridge_candidate(directory: Path) -> bool:
    """Whether `directory/addon.yaml` says `auto_install: true`. Anything unreadable is not."""
    try:
        raw = yaml.safe_load((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    return isinstance(raw, dict) and raw.get("auto_install") is True


def _check_paths(manifest: AddonManifest, directory: Path) -> None:
    """Each path a manifest points at exists, is the right kind and stays inside the addon."""
    base = directory.resolve()
    declared: tuple[tuple[str, str | None, bool], ...] = (
        ("provides.profiles", manifest.provides.profiles, True),
        ("provides.fragments", manifest.provides.fragments, True),
        ("provides.policy", manifest.provides.policy, False),
        ("provides.intents", manifest.provides.intents, False),
        ("migrations", manifest.migrations, True),
    )
    for name, relative, is_directory in declared:
        if relative is None:
            continue
        target = (directory / relative).resolve()
        if not target.is_relative_to(base):
            raise ManifestError(
                manifest.name, name, f"{relative!r} resolves outside the addon directory"
            )
        exists = target.is_dir() if is_directory else target.is_file()
        if not exists:
            kind = "directory" if is_directory else "file"
            raise ManifestError(manifest.name, name, f"{relative!r} is not a {kind} of the addon")


def load_addons(
    *,
    enabled: Sequence[str],
    addons_root: Path,
    core_version: str,
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[ResolvedAddon, ...]:
    """The active addons under `addons_root`, in activation order. Raises `AddonError` subclasses.

    `config` is the parsed deployment file and `env` the environment the settings are read from
    (`settings.py`). No manifest is read when nothing is enabled.
    """
    if not enabled:
        resolve_settings([], config=config or {}, env=env or {})  # settings for nothing: an error
        return ()
    if not addons_root.is_dir():
        raise AddonResolutionError(enabled[0], None, f"is enabled but {addons_root} does not exist")

    available: dict[str, AddonManifest] = {}
    for name in enabled:
        directory = addons_root / name
        if not (directory / MANIFEST_FILENAME).is_file():
            raise AddonResolutionError(
                name, None, f"is enabled but {directory / MANIFEST_FILENAME} does not exist"
            )
        available[name] = load_manifest(directory)
    for directory in sorted(addons_root.iterdir()):
        name = directory.name
        if name in available or name.startswith((".", "_")) or not directory.is_dir():
            continue
        if _is_bridge_candidate(directory):
            available[name] = load_manifest(directory)

    resolved = resolve_addons(enabled=enabled, available=available, core_version=core_version)
    for item in resolved:
        _check_paths(item.manifest, addons_root / item.manifest.name)
    settings = resolve_settings(
        [item.manifest for item in resolved], config=config or {}, env=env or {}
    )
    return tuple(
        ResolvedAddon(
            item.manifest,
            addons_root / item.manifest.name,
            item.source,
            settings[item.manifest.name],
        )
        for item in resolved
    )


def load_addons_from_environment(env: Mapping[str, str]) -> tuple[ResolvedAddon, ...]:
    """`AGENT_CORE_ADDONS` (comma list, default none), `AGENT_CORE_ADDONS_DIR` (default the
    repository's `addons/`), `AGENT_CORE_ADDONS_CONFIG` (optional settings file) and the
    `AGENT_CORE_ADDON__<ADDON>__<KEY>` variables -> the active addons for this Core."""
    root, config_path = env.get(ENV_ADDONS_DIR), env.get(ENV_ADDONS_CONFIG)
    return load_addons(
        enabled=parse_enabled(env.get(ENV_ADDONS, "")),
        addons_root=Path(root) if root else default_addons_root(),
        core_version=core_version(),
        config=read_addons_config(Path(config_path)) if config_path else None,
        env=env,
    )
