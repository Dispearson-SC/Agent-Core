"""Settings of the tool bridge: defaults, mapping, and the addon manifest that declares them."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.tools.http_bridge import BridgeSettings, settings_from_environment

ADDONS_ROOT = Path(__file__).resolve().parents[3] / "addons"


def test_the_defaults_are_neutral() -> None:
    s = BridgeSettings()

    assert s.session_header == "X-Agent-Session" and s.agent_header == "X-Agent-Id"
    assert s.tenant_header == "X-Agent-Tenant" and s.subject_header == ""
    assert s.idempotency_header == "Idempotency-Key" and s.warning_header == "X-Warning"
    assert s.peer_session_prefix == "" and s.default_row_limit == 20
    assert (s.timeout_seconds, s.connect_timeout_seconds) == (10.0, 3.0)


def test_from_mapping_takes_known_keys_and_refuses_unknown_ones() -> None:
    s = BridgeSettings.from_mapping({"base_url": "http://x/", "default_row_limit": 5})
    assert s.base_url == "http://x" and s.default_row_limit == 5  # trailing slash trimmed

    with pytest.raises(ValueError, match="nope"):
        BridgeSettings.from_mapping({"nope": 1})


def test_the_addon_declares_exactly_the_settings_the_class_reads() -> None:
    env = {"AGENT_CORE_ADDONS": "http_tool_bridge"}
    (loaded,) = load_addons_from_environment(env)

    assert loaded.manifest.name == "http_tool_bridge"
    assert set(loaded.settings) == set(BridgeSettings.__dataclass_fields__)
    assert BridgeSettings.from_mapping(dict(loaded.settings)) == BridgeSettings(
        base_url=loaded.settings["base_url"]
    )


def test_settings_come_from_the_enabled_addon_and_its_variables() -> None:
    env = {
        "AGENT_CORE_ADDONS": "http_tool_bridge",
        "AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__BASE_URL": "http://svc.internal:8080/",
        "AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__SESSION_HEADER": "X-Case",
    }

    s = settings_from_environment(env)

    assert s.base_url == "http://svc.internal:8080" and s.session_header == "X-Case"


def test_reading_settings_with_the_addon_not_enabled_says_how_to_enable_it() -> None:
    with pytest.raises(ValueError, match="http_tool_bridge"):
        settings_from_environment({})
