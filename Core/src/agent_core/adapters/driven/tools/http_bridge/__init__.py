"""Generic HTTP tool bridge: tools built from a declarative manifest (Phase 3, G1, D83).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_*.py
"""

from agent_core.adapters.driven.tools.http_bridge.builder import build_bridge_toolset
from agent_core.adapters.driven.tools.http_bridge.client import Reply, call_backend, case_id_for
from agent_core.adapters.driven.tools.http_bridge.manifest import (
    BridgeManifest,
    BridgeManifestError,
    load_bridge_manifest,
    parse_bridge_manifest,
)
from agent_core.adapters.driven.tools.http_bridge.openapi import (
    OpenApiContractError,
    load_openapi_manifest,
)
from agent_core.adapters.driven.tools.http_bridge.settings import BridgeSettings

__all__ = [
    "BridgeManifest",
    "BridgeManifestError",
    "BridgeSettings",
    "OpenApiContractError",
    "Reply",
    "build_bridge_toolset",
    "call_backend",
    "case_id_for",
    "load_bridge_manifest",
    "load_openapi_manifest",
    "parse_bridge_manifest",
]
