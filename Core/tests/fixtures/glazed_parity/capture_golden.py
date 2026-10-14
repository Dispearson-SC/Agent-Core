"""Capture the golden glazed turns from vertical/glazed, the old patched Core (G6).

The scripted turns of `tests/support/glazed_parity.py` are run against the OLD build: the glazed
tools of `adapters/driven/tools/glazed/tools.py`, the `glazed_*.yaml` profiles and
`policy/rules.yaml` of that branch's Core, and its `PgAuditSink` with the glazed entries patched
into its constants.
The result is `golden.json` next to this file; `tests/unit/test_glazed_golden_turns.py` runs the
same turns on the addon build and compares.

HOW golden.json WAS PRODUCED (run it again only to re-capture; never edit golden.json by hand)
    OLD="$(mktemp -d)/old"                          # a throwaway checkout in the system temp dir
    git -C <repo> worktree add --detach "$OLD" vertical/glazed
    cd <this worktree>/Core
    PYTHONPATH="$OLD/Core/src" .venv/bin/python \
        tests/fixtures/glazed_parity/capture_golden.py "$OLD/Core"
    git -C <repo> worktree remove --force "$OLD"    # and delete it

`PYTHONPATH` puts the OLD `agent_core` first, so every import below (and the harness's) is the old
Core's; the harness itself is imported from this worktree by path. The same `.venv` serves both:
vertical/glazed needs nothing this one lacks. Captured from vertical/glazed cfecd44.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
sys.path.insert(1, str(HERE.parents[3]))  # this worktree's Core/, for `tests.support`

from tests.support.glazed_parity import BASE_URL, Build, run_scenarios  # noqa: E402


def main(old_core: Path) -> int:
    os.environ["GLAZED_BACKEND_URL"] = BASE_URL
    # vertical/glazed only: this worktree's Core has no such package
    from agent_core.adapters.driven.tools.glazed import (  # type: ignore[import-untyped, unused-ignore]
        client,
    )
    from agent_core.adapters.driven.tools.glazed import (  # type: ignore[import-untyped, unused-ignore]
        tools as gt,
    )

    import agent_core
    from agent_core.adapters.driven.policy_fs.loader import load_policy_rules
    from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync

    loaded_from = Path(agent_core.__file__).resolve()
    if not loaded_from.is_relative_to(old_core.resolve()):
        print(f"agent_core comes from {loaded_from}, not from {old_core}", file=sys.stderr)
        return 1

    def install(transport: Any) -> None:
        client.TRANSPORT = transport

    build = Build(
        toolset=lambda role: gt.TOOLSETS[role](),
        persona=lambda role: (
            load_profile_sync(old_core / "profiles" / f"glazed_{role}.yaml").persona
        ),
        rules=load_policy_rules(old_core / "policy"),
        audit_policy=None,  # the old sink's module constants, glazed entries included
        install_transport=install,
    )
    golden = run_scenarios(build)
    target = HERE.parent / "golden.json"
    target.write_text(
        json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {target} ({len(golden)} scenarios)")
    return 0


USAGE = "usage: capture_golden.py <old Core directory>  (a vertical/glazed checkout's Core/)"


def cli(argv: list[str]) -> int:
    """Check the command line before anything is imported or written: one argument, a Core
    directory (it holds `src/agent_core`). Anything else prints the usage and exits 2."""
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    old_core = Path(argv[0])
    if not (old_core / "src" / "agent_core").is_dir():
        print(f"{old_core} is not a Core directory (no src/agent_core)\n{USAGE}", file=sys.stderr)
        return 2
    return main(old_core)


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
