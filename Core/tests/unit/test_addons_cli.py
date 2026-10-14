"""A7 (D80): `python -m agent_core addons list` - what is installed and what a start would do.

Read-only: it reads the `addons` table and the enabled manifests and changes nothing. Uninstalling
is deliberately not a subcommand.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import pytest

from agent_core import main as main_module
from agent_core.adapters.driven.addons_fs.install import InstalledAddon
from agent_core.adapters.driving.cli.addons import render_addons, run_addons
from agent_core.composition import Settings

WHEN = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _row(name: str, version: str) -> InstalledAddon:
    return InstalledAddon(name, version, WHEN, None)


def test_the_subcommand_is_addons_list() -> None:
    arguments = main_module._argument_parser().parse_args(["addons", "list"])

    assert (arguments.command, arguments.action) == ("addons", "list")


def test_there_is_no_uninstall_subcommand() -> None:
    with pytest.raises(SystemExit):
        main_module._argument_parser().parse_args(["addons", "uninstall", "shop"])


@pytest.mark.parametrize(
    ("installed", "enabled", "state"),
    [
        ((_row("shop", "1.0.0"),), {"shop": "1.0.0"}, "current"),
        ((), {"shop": "1.0.0"}, "installs at the next start"),
        ((_row("shop", "1.0.0"),), {"shop": "1.1.0"}, "upgrades at the next start"),
        ((_row("shop", "2.0.0"),), {"shop": "1.0.0"}, "refuses to start"),
        ((_row("shop", "1.0.0"),), {}, "not enabled"),
    ],
    ids=["current", "new", "upgrade", "downgrade", "removed"],
)
def test_each_addon_says_what_the_next_start_does(
    installed: tuple[InstalledAddon, ...], enabled: dict[str, str], state: str
) -> None:
    text = "\n".join(render_addons(installed, enabled))

    assert "shop" in text and state in text


def test_nothing_enabled_and_nothing_installed_says_so() -> None:
    assert any("no addons" in line.lower() for line in render_addons((), {}))


def test_a_removed_addon_is_reported_as_kept_not_deleted() -> None:
    text = "\n".join(render_addons((_row("old", "1.0.0"),), {}))

    assert "kept" in text


def test_the_command_prints_the_report_and_exits_zero() -> None:
    lines: list[str] = []
    settings = Settings()

    code = run_addons(
        argparse.Namespace(action="list"),
        settings,
        read=lambda conninfo: (_row("old", "1.0.0"),),
        out=lines.append,
    )

    assert code == 0 and any("old" in line for line in lines)


def test_a_database_that_cannot_be_read_exits_two_not_clean() -> None:
    lines: list[str] = []

    def unreachable(conninfo: str) -> tuple[InstalledAddon, ...]:
        raise OSError("down")

    code = run_addons(
        argparse.Namespace(action="list"), Settings(), read=unreachable, out=lines.append
    )

    assert code == 2 and any("cannot read" in line for line in lines)
