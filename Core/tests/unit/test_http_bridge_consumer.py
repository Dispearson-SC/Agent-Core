"""A consumer addon uses the bridge exactly as docs/ADDONS.md says (Phase 3, G1, D83).

Real loader, real registration: the consumer lists `http_tool_bridge` under `depends`, its toolset
module calls `build_bridge_toolset(<its own manifest>)`, and the tools, audit and the bridge's
settings all arrive through the addon system.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError
from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import register_addons

HERE = Path(__file__).resolve()
BRIDGE_DIR = HERE.parents[3] / "addons" / "http_tool_bridge"
EXAMPLE = HERE.parents[1] / "fixtures" / "http_bridge" / "example_tools.yaml"

CONSUMER_YAML = """\
name: lending
version: 1.0.0
core: ">=0.0,<1"
depends: [http_tool_bridge]
provides:
  toolsets: [books]
  audit:
    allow: {get_book: [book_id]}
"""
CONSUMER_TOOLSET = """\
from pathlib import Path

from agent_core.adapters.driven.tools.http_bridge import build_bridge_toolset


def build_toolset():
    return build_bridge_toolset(Path(__file__).resolve().parent.parent / "tools.yaml")
"""


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(BRIDGE_DIR, tmp_path / "http_tool_bridge")
    consumer = tmp_path / "lending"
    (consumer / "toolsets").mkdir(parents=True)
    (consumer / "addon.yaml").write_text(CONSUMER_YAML, encoding="utf-8")
    (consumer / "toolsets" / "books.py").write_text(CONSUMER_TOOLSET, encoding="utf-8")
    shutil.copy(EXAMPLE, consumer / "tools.yaml")
    monkeypatch.setenv("AGENT_CORE_ADDONS", "http_tool_bridge,lending")
    monkeypatch.setenv("AGENT_CORE_ADDONS_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__BASE_URL", "http://lend.test:1")
    return tmp_path


def test_a_consumer_addon_gets_tools_audit_and_the_bridge_settings(root: Path) -> None:
    loaded = load_addons_from_environment(dict(os.environ))

    contributions = register_addons(
        loaded, builtin_toolsets={}, builtin_fragments_dir=None, reserved_channels=()
    )

    toolset = contributions.toolsets["books"]()
    assert set(toolset.tools) == {
        "list_books",
        "get_book",
        "recall_reviews",
        "reserve_book",
        "ping",
    }
    assert contributions.audit["lending"][0] == {"get_book": frozenset({"book_id"})}


def test_a_consumer_without_the_bridge_enabled_is_refused_at_resolution(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CORE_ADDONS", "lending")

    with pytest.raises(AddonResolutionError, match="http_tool_bridge"):
        load_addons_from_environment(dict(os.environ))
