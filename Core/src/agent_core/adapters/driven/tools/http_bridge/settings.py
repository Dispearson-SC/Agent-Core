"""Settings of the HTTP tool bridge: what a deployment may change (Phase 3, G1, D83).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_settings.py
Implements: no port - the typed view of the `http_tool_bridge` addon's validated settings.

The schema an operator edits is `addons/http_tool_bridge/addon.yaml`; a test keeps the two in step.
A toolset module is built by a zero-argument `build_toolset()` and receives no settings from the
addon loader, so `settings_from_environment` reads the same sources the loader does (the addon
file defaults, `AGENT_CORE_ADDONS_CONFIG`, `AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__*`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Final

__all__ = ["BRIDGE_ADDON", "BridgeSettings", "settings_from_environment"]

BRIDGE_ADDON: Final = "http_tool_bridge"


@dataclass(frozen=True)
class BridgeSettings:
    base_url: str = "http://localhost:8080"
    timeout_seconds: float = 10.0
    connect_timeout_seconds: float = 3.0
    default_row_limit: int = 20
    # Header names for the turn's identities. An empty name turns that header off.
    session_header: str = "X-Agent-Session"
    agent_header: str = "X-Agent-Id"
    tenant_header: str = "X-Agent-Tenant"
    subject_header: str = ""
    idempotency_header: str = "Idempotency-Key"
    # The response header whose value is surfaced to the model (a tool opts in with `warning`).
    warning_header: str = "X-Warning"
    # A peer session id is `<prefix><asker id><separator><suffix>`; with a prefix set, the session
    # header carries the ASKING session's id. Empty = ids are sent as they are.
    peer_session_prefix: str = ""
    peer_session_separator: str = "~"
    # How error messages call the service ("The <name> timed out...").
    service_name: str = "backend"

    def __post_init__(self) -> None:
        if self.peer_session_prefix and not self.peer_session_separator:
            raise ValueError("peer_session_separator must not be empty when a prefix is set")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> BridgeSettings:
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown http_tool_bridge setting {unknown[0]!r}")
        data = dict(values)
        if "base_url" in data:
            data["base_url"] = str(data["base_url"]).rstrip("/")
        return cls(**data)


def settings_from_environment(env: Mapping[str, str] | None = None) -> BridgeSettings:
    """The enabled `http_tool_bridge` addon's settings; `ValueError` when it is not enabled."""
    from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment

    loaded = load_addons_from_environment(os.environ if env is None else env)
    for addon in loaded:
        if addon.manifest.name == BRIDGE_ADDON:
            return BridgeSettings.from_mapping(dict(addon.settings))
    raise ValueError(
        f"the {BRIDGE_ADDON} addon is not enabled: add it to AGENT_CORE_ADDONS "
        "(a consumer addon should list it under `depends`)"
    )
