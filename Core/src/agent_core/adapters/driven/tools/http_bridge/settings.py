"""Settings of the HTTP tool bridge: what a deployment may change (Phase 3, G1, D83, D84).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_settings.py
Implements: no port - the typed view of the settings a CONSUMER addon declares.

A consumer addon declares the keys it wants an operator to edit in its own `addon.yaml` `settings:`
(any subset of the fields below; the rest keep these defaults) and its `build_toolset(settings)`
hands the loader-validated mapping to `build_bridge_toolset`. The bridge reads no environment: each
vertical is configured under its own name (`AGENT_CORE_ADDON__<CONSUMER>__BASE_URL`), so two
consumers can point at two backends.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

__all__ = ["BridgeSettings"]


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
    warning_header: str = "X-Agent-Warning"
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
    def from_consumer(cls, values: Mapping[str, Any]) -> BridgeSettings:
        """The bridge keys of a consumer addon's settings; its own other keys are ignored."""
        known = {f.name for f in fields(cls)}
        return cls.from_mapping({k: v for k, v in values.items() if k in known})

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
