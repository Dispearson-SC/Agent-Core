"""Driving adapter: `python -m agent_core addons list` - what is installed, what a start would do.

Phase:   Phase 2 (addon system), A7
Tests:   Core/tests/unit/test_addons_cli.py
Used by: agent_core/main.py - the `addons` subcommand
Runbook: docs/OPERATIONS.md, "Install state"

READ-ONLY, AND THERE IS NO `uninstall`
    It compares the `addons` table with the manifests of the addons enabled right now and says,
    for each, what the next start does: nothing, install, upgrade, or refuse (a downgrade). An
    addon that is installed but no longer enabled is listed as kept: its rows and the tables its
    migrations made stay until an operator removes them by hand (D80). A command that dropped an
    addon's tables would be a destructive path nobody asked for; the safe default is that
    removing an addon from the list loses nothing.

EXIT CODE: 0 the report was printed, 2 the database could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable

from packaging.version import InvalidVersion, Version

from agent_core.adapters.driven.addons_fs.install import InstalledAddon, list_installed
from agent_core.composition import Settings

__all__ = ["add_addons_parser", "render_addons", "run_addons"]


def add_addons_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser(
        "addons", help="the install state of addons (see OPERATIONS.md, 'Install state')"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "list", help="installed and enabled addons, and what the next start would do (read-only)"
    )


def _newer(left: str, right: str) -> bool:
    try:
        return Version(left) > Version(right)
    except InvalidVersion:
        return False


def _state(installed: str | None, enabled: str | None) -> str:
    if enabled is None:
        return "not enabled: kept, with its rows and tables (remove them by hand to uninstall)"
    if installed is None:
        return "installs at the next start"
    if installed == enabled:
        return "current"
    if _newer(installed, enabled):
        return "refuses to start: the database holds a newer version than the manifest"
    return "upgrades at the next start"


def render_addons(installed: tuple[InstalledAddon, ...], enabled: dict[str, str]) -> list[str]:
    """One line per addon, installed or enabled, by name; a note when there is neither."""
    recorded = {row.name: row.version for row in installed}
    names = sorted({*recorded, *enabled})
    if not names:
        return ["no addons are enabled and none is installed."]
    return [
        f"{name}  installed={recorded.get(name, '-')}  manifest={enabled.get(name, '-')}  "
        f"{_state(recorded.get(name), enabled.get(name))}"
        for name in names
    ]


def _read(conninfo: str) -> tuple[InstalledAddon, ...]:
    return asyncio.run(list_installed(conninfo))


def run_addons(
    arguments: argparse.Namespace,
    settings: Settings,
    *,
    read: Callable[[str], tuple[InstalledAddon, ...]] = _read,
    out: Callable[[str], None] = print,
) -> int:
    """Run the parsed `addons` command (only `list` exists). `read` is injectable for tests."""
    try:
        installed = read(settings.app_conninfo)
    except Exception as error:
        out(f"[addons] cannot read the install state: {type(error).__name__}")
        return 2
    enabled = {addon.manifest.name: addon.manifest.version for addon in settings.addons}
    for line in render_addons(installed, enabled):
        out(line)
    return 0
