"""A consumer addon uses the bridge exactly as docs/ADDONS.md says (Phase 3, G1, D83).

Real loader, real registration: the consumer lists `http_tool_bridge` under `depends`, its toolset
module calls `build_bridge_toolset(<its own manifest>)`, and the tools, audit and the consumer's
settings all arrive through the addon system.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError, AddonSettingsError
from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId

HERE = Path(__file__).resolve()
BRIDGE_DIR = HERE.parents[3] / "addons" / "http_tool_bridge"
EXAMPLE = HERE.parents[1] / "fixtures" / "http_bridge" / "example_tools.yaml"

CONSUMER_YAML = """\
name: lending
version: 1.0.0
core: ">=0.0,<1"
depends: [http_tool_bridge]
settings:
  base_url: {type: str, default: "http://localhost:1"}
  service_name: {type: str, default: lending}
  currency: {type: str, default: MXN}
provides:
  toolsets: [books]
  audit:
    allow: {get_book: [book_id]}
"""
CONSUMER_TOOLSET = """\
from pathlib import Path

from agent_core.adapters.driven.tools.http_bridge import build_bridge_toolset


def build_toolset(settings):
    return build_bridge_toolset(
        settings, manifest=Path(__file__).resolve().parent.parent / "tools.yaml"
    )
"""


def _ctx() -> SimpleNamespace:
    deps = TurnContext(
        caller=CallerIdentity("m-1", "web", TenantId("T9"), frozenset()),
        session=SessionRef(session_id="case-1", tenant_id=TenantId("T9")),  # type: ignore[arg-type]
        agent_id="clerk",
    )
    return SimpleNamespace(deps=deps)


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
    monkeypatch.setenv("AGENT_CORE_ADDON__LENDING__BASE_URL", "http://lend.test:1")
    monkeypatch.setenv("AGENT_CORE_ADDON__LENDING__SERVICE_NAME", "library")
    return tmp_path


def test_a_consumer_addon_gets_tools_audit_and_its_own_settings(root: Path) -> None:
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


def test_a_setting_aimed_at_the_bridge_addon_is_refused_not_silently_ignored(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__BASE_URL", "http://ignored.test:9")

    with pytest.raises(AddonSettingsError, match="not a setting of this addon"):
        load_addons_from_environment(dict(os.environ))


def test_the_consumers_settings_configure_the_calls(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = load_addons_from_environment(dict(os.environ))
    contributions = register_addons(
        loaded, builtin_toolsets={}, builtin_fragments_dir=None, reserved_channels=()
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "agent_core.adapters.driven.tools.http_bridge.client.httpx.AsyncClient",
        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}),
    )
    tool = contributions.toolsets["books"]().tools["get_book"]

    result = asyncio.run(tool.function(_ctx(), book_id="b1"))

    assert seen[0].url.host == "lend.test"
    assert result["error"].startswith("The library is unreachable")


def test_a_consumer_without_the_bridge_enabled_is_refused_at_resolution(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CORE_ADDONS", "lending")

    with pytest.raises(AddonResolutionError, match="http_tool_bridge"):
        load_addons_from_environment(dict(os.environ))
