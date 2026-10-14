"""Settings of the tool bridge: defaults, mapping, and the addon manifest that declares them."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.tools.http_bridge import BridgeSettings

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


def test_from_consumer_takes_the_bridge_keys_and_ignores_the_consumers_own() -> None:
    s = BridgeSettings.from_consumer(
        {"base_url": "http://x/", "service_name": "shop", "currency": "MXN", "page_size": 5}
    )

    assert s.base_url == "http://x" and s.service_name == "shop"
    assert s == BridgeSettings.from_mapping({"base_url": "http://x", "service_name": "shop"})


def test_the_bridge_addon_is_a_marker_that_declares_no_settings_of_its_own() -> None:
    (loaded,) = load_addons_from_environment({"AGENT_CORE_ADDONS": "http_tool_bridge"})

    assert loaded.manifest.name == "http_tool_bridge" and dict(loaded.settings) == {}
