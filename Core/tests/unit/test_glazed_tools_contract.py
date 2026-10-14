"""The glazed tools keep the contract of the hand-written tools they replaced, and the startup
refuses a backend that no longer honours it (Phase 3, G2).

Oracle: `fixtures/glazed_tools/old_tool_contract.json`, captured from the tools of vertical/glazed
(descriptions and argument schemas the model saw). Subject: addons/glazed_tools, built from the
committed snapshot of the backend's internal OpenAPI document.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError
from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import AddonContributions, register_addons
from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.http_bridge import OpenApiContractError, clear_openapi_cache
from agent_core.composition import TOOL_PACKAGES
from agent_core.domain.tool_replay import ReplayPolicy, replay_policy
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId

HERE = Path(__file__).resolve()
ADDONS_DIR = HERE.parents[3] / "addons"
FIXTURES = HERE.parents[1] / "fixtures" / "glazed_tools"
SNAPSHOT = FIXTURES / "internal_openapi.json"
OLD = json.loads((FIXTURES / "old_tool_contract.json").read_text(encoding="utf-8"))
HELPER = "agent_core_addons.glazed_tools.toolsets._glazed"
ENVIRONMENT = {
    "AGENT_CORE_ADDONS": "http_tool_bridge,glazed_tools",
    "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR),
    "AGENT_CORE_ADDON__GLAZED_TOOLS__BASE_URL": "http://backend.test:9",
    "AGENT_CORE_ADDON__GLAZED_TOOLS__OPENAPI_URL": str(SNAPSHOT),
}
# Everything that is not a plain read of the backend keeps the old `interrupted` replay.
INTERRUPTED = {
    "propose_action",
    "classify_text",
    "save_manager_note",
    "audit_promo",
    "record_event",
    "propose_transfer",
}


@pytest.fixture(autouse=True)
def _fresh_document_cache() -> None:
    clear_openapi_cache()


@pytest.fixture(scope="module")
def registered() -> tuple[AddonContributions, ModuleType]:
    loaded = load_addons_from_environment(dict(ENVIRONMENT))
    contributions = register_addons(
        loaded, builtin_toolsets=TOOL_PACKAGES, builtin_fragments_dir=None, reserved_channels=()
    )
    return contributions, sys.modules[HELPER]


def _all_tools(contributions: AddonContributions) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for role in OLD["roles"]:
        tools.update(contributions.toolsets[f"glazed_{role}"]().tools)
    return tools


def test_every_role_offers_exactly_the_tools_it_had(
    registered: tuple[AddonContributions, ModuleType],
) -> None:
    contributions, _ = registered
    for role, names in OLD["roles"].items():
        assert sorted(contributions.toolsets[f"glazed_{role}"]().tools) == names, role
    assert set(contributions.tools["glazed_tools"]) == set(OLD["tools"])


def test_every_description_is_the_text_the_model_read_before(
    registered: tuple[AddonContributions, ModuleType],
) -> None:
    tools = _all_tools(registered[0])
    for name, old in OLD["tools"].items():
        assert tools[name].description == old["description"], name


def test_every_argument_keeps_its_name_requiredness_and_type(
    registered: tuple[AddonContributions, ModuleType],
) -> None:
    tools = _all_tools(registered[0])
    for name, old in OLD["tools"].items():
        new = tools[name].function_schema.json_schema
        assert set(new["properties"]) == set(old["parameters"]["properties"]), name
        assert set(new.get("required", [])) == set(old["parameters"].get("required", [])), name
        assert new["additionalProperties"] is False
        for arg, spec in old["parameters"]["properties"].items():
            old_type = spec.get("type") or next(
                o["type"] for o in spec["anyOf"] if o["type"] != "null"
            )
            assert new["properties"][arg]["type"] == old_type, (name, arg)
            if "default" in spec and spec["default"] is not None:
                assert new["properties"][arg]["default"] == spec["default"], (name, arg)


# (tool, argument) -> (default, minimum, maximum): the clamps of the old hand-written tools
BOUNDS = {
    ("get_issues", "limit"): (10, 1, 20),
    ("recall_experiences", "limit"): (5, 1, 20),
    ("get_transfer_options", "limit"): (5, 1, 20),
    ("project_inventory", "horizon"): (7, 1, 30),
    ("get_inventory_status", "limit"): (15, 1, 50),
    ("get_expiring", "days"): (7, 1, 30),
    ("get_deliveries", "days"): (7, 1, 14),
    ("get_staffing", "days"): (7, 1, 14),
    ("get_promos", "days"): (14, 1, 365),
    ("get_outlook", "days"): (2, 1, 7),
}
ENUMS = {
    ("get_issues", "category"): ["operational", "integrity", "all"],
    ("classify_text", "kind"): ["rejection_reason", "deviation_cause"],
    ("get_inventory_status", "sort"): ["days_of_cover", "turnover", "slow"],
    ("get_promos", "status"): ["active", "upcoming", "past"],
}


def test_every_clamp_and_enum_of_the_old_tools_is_in_the_schema_the_model_sees(
    registered: tuple[AddonContributions, ModuleType],
) -> None:
    tools = _all_tools(registered[0])
    for (name, arg), (default, low, high) in BOUNDS.items():
        spec = tools[name].function_schema.json_schema["properties"][arg]
        assert (spec.get("default"), spec.get("minimum"), spec.get("maximum")) == (
            default,
            low,
            high,
        ), (name, arg)
    for (name, arg), allowed in ENUMS.items():
        assert tools[name].function_schema.json_schema["properties"][arg]["enum"] == allowed


def _normalised(spec: dict[str, Any]) -> dict[str, Any]:
    """An argument schema without titles, an optional X (`anyOf [X, null]`) flattened to X."""
    flat = {k: v for k, v in spec.items() if k != "title"}
    if "anyOf" in flat:
        real = next(o for o in flat.pop("anyOf") if o["type"] != "null")
        flat.update(real)
    return flat


# The only places the generated schema may differ from the oracle (G2b); all else must be equal.
#   * an optional argument whose old default was None: the bridge states no default
#   * the clamps and enums the old tools applied in code, now in the schema the model sees
#     (BOUNDS / ENUMS above pin their values)
#   * `format: date`: the bridge's argument model has no `format`; the date is validated and
#     answered by the forecast guard instead (`_target`), with the old wording
def test_the_generated_argument_schemas_differ_from_the_oracle_only_where_documented(
    registered: tuple[AddonContributions, ModuleType],
) -> None:
    tools = _all_tools(registered[0])
    unexplained: list[str] = []
    for name, old in OLD["tools"].items():
        generated = tools[name].function_schema.json_schema["properties"]
        for arg, spec in old["parameters"]["properties"].items():
            expected = _normalised(spec)
            if expected.get("default", 0) is None:
                del expected["default"]
            if (name, arg) in BOUNDS:
                default, low, high = BOUNDS[(name, arg)]
                expected.update(minimum=low, maximum=high)
            if (name, arg) in ENUMS:
                expected["enum"] = ENUMS[(name, arg)]
            expected.pop("format", None)
            if _normalised(generated[arg]) != expected:
                unexplained.append(f"{name}.{arg}: {_normalised(generated[arg])} != {expected}")
    assert unexplained == []


def test_the_oracle_has_one_tool_more_than_the_backend_marks_the_second_tool_over_issues() -> None:
    """`get_integrity_issues` is not an OpenAPI operation of its own: the overlay defines it as a
    second tool over `GET /internal/v1/issues` (fixed filter), pinned to that operation. The
    snapshot marks the other 29; the oracle's description of it is the addon's (tested above)."""
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    marked = {
        op.get("x-agent-name") or op["operationId"]
        for methods in document["paths"].values()
        for op in methods.values()
        if isinstance(op, dict) and op.get("x-agent-tool") is True
    }

    assert set(OLD["tools"]) - marked == {"get_integrity_issues"}
    assert marked - set(OLD["tools"]) == set()
    assert OLD["roles"]["auditor"] == ["get_integrity_issues"]


def test_reads_are_replay_safe_and_the_rest_keep_the_old_interrupted(
    registered: tuple[AddonContributions, ModuleType],
) -> None:
    """The old tools were all `interrupted`. A plain GET of the backend is idempotent (its only side
    effect is a trace row of the read), so replaying it after a crash is safe; every POST is not."""
    for name, tool in _all_tools(registered[0]).items():
        expected = ReplayPolicy.INTERRUPTED if name in INTERRUPTED else ReplayPolicy.SAFE
        assert replay_policy(tool.metadata) is expected, name


# --- the startup refuses a backend that broke the contract ---------------------------------------


def _build(document: dict[str, Any], tmp_path: Path, role: str = "supply") -> Any:
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    helper = sys.modules[HELPER]
    settings = {"base_url": "http://backend.test:9", "openapi_url": str(path)}
    return helper.build_role(settings, role)


def _snapshot() -> dict[str, Any]:
    return copy.deepcopy(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


def test_the_snapshot_itself_builds(
    registered: tuple[AddonContributions, ModuleType], tmp_path: Path
) -> None:
    assert set(_build(_snapshot(), tmp_path).tools) == set(OLD["roles"]["supply"])


def test_a_dropped_mark_stops_the_startup_naming_the_tool(tmp_path: Path) -> None:
    document = _snapshot()
    del document["paths"]["/internal/v1/kpis"]["get"]["x-agent-tool"]

    with pytest.raises(OpenApiContractError, match="get_kpis"):
        _build(document, tmp_path)


def test_a_renamed_tool_stops_the_startup(tmp_path: Path) -> None:
    document = _snapshot()
    document["paths"]["/internal/v1/kpis"]["get"]["x-agent-name"] = "get_kpi_window"

    with pytest.raises(OpenApiContractError, match="get_kpis"):
        _build(document, tmp_path)


def test_a_moved_operation_stops_the_startup_through_its_pin(tmp_path: Path) -> None:
    document = _snapshot()
    document["paths"]["/internal/v1/kpi"] = document["paths"].pop("/internal/v1/kpis")

    with pytest.raises(OpenApiContractError, match="get_kpis.*GET /internal/v1/kpis"):
        _build(document, tmp_path)


def test_the_second_tool_over_issues_follows_its_operation(tmp_path: Path) -> None:
    document = _snapshot()
    document["paths"]["/internal/v1/issues"] = {}  # /issues gone: get_issues AND the integrity tool
    document["paths"]["/internal/v1/issue-list"] = {"get": {"x-agent-tool": False}}

    with pytest.raises(OpenApiContractError, match="get_issues|get_integrity_issues"):
        _build(document, tmp_path)


def test_an_argument_the_overlay_hides_but_the_backend_dropped_stops_the_startup(
    tmp_path: Path,
) -> None:
    document = _snapshot()
    parameters = document["paths"]["/internal/v1/forecast/demand"]["get"]["parameters"]
    document["paths"]["/internal/v1/forecast/demand"]["get"]["parameters"] = [
        p for p in parameters if p["name"] != "store_id"
    ]

    with pytest.raises(OpenApiContractError, match="forecast_demand.*store_id"):
        _build(document, tmp_path)


def test_a_backend_that_is_down_at_startup_stops_it_naming_the_url(
    registered: tuple[AddonContributions, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, helper = registered

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(helper, "TRANSPORT", httpx.MockTransport(down))
    with pytest.raises(OpenApiContractError, match="backend.test.*startup"):
        helper.build_role({"base_url": "http://backend.test:9", "openapi_url": ""}, "past")


def test_the_openapi_document_is_asked_for_once_by_all_eight_toolsets(
    registered: tuple[AddonContributions, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    contributions, helper = registered
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json=_snapshot())

    # the registered builders read the snapshot FILE; a URL source is the production one
    monkeypatch.setattr(helper, "TRANSPORT", httpx.MockTransport(handler))
    for role in OLD["roles"]:
        helper.build_role({"base_url": "http://backend.test:9", "openapi_url": ""}, role)

    assert asked == ["http://backend.test:9/openapi.json"]


# --- settings -------------------------------------------------------------------------------------


def test_the_settings_default_to_the_glazed_conventions() -> None:
    (addon,) = (
        a
        for a in load_addons_from_environment(
            {
                "AGENT_CORE_ADDONS": "http_tool_bridge,glazed_tools",
                "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR),
            }
        )
        if a.manifest.name == "glazed_tools"
    )

    assert dict(addon.settings) == {
        "base_url": "http://backend:8080",
        "openapi_url": "",
        "timeout_seconds": 10.0,
        "connect_timeout_seconds": 3.0,
        "default_row_limit": 20,
        "session_header": "X-Glazed-Case",
        "agent_header": "X-Glazed-Agent",
        "tenant_header": "X-Glazed-Store",
        "subject_header": "",
        "idempotency_header": "Idempotency-Key",
        "warning_header": "X-Glazed-Warning",
        "peer_session_prefix": "peer~",
        "peer_session_separator": "~",
        "service_name": "Glazed backend",
    }


def test_the_addon_needs_the_bridge_enabled() -> None:
    with pytest.raises(AddonResolutionError, match="http_tool_bridge"):
        load_addons_from_environment(
            {"AGENT_CORE_ADDONS": "glazed_tools", "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR)}
        )


def test_a_setting_changes_the_headers_the_tools_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {**ENVIRONMENT, "AGENT_CORE_ADDON__GLAZED_TOOLS__SESSION_HEADER": "X-Other-Case"}
    contributions = register_addons(
        load_addons_from_environment(env),
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=(),
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(sys.modules[HELPER], "TRANSPORT", httpx.MockTransport(handler))
    deps = TurnContext(
        caller=CallerIdentity("m-1", "glazed", TenantId("S030"), frozenset()),
        session=SessionRef(session_id="case-1", tenant_id=TenantId("S030")),  # type: ignore[arg-type]
        agent_id="glazed_supply",
    )

    tool = contributions.toolsets["glazed_supply"]().tools["get_order_plan"]
    asyncio.run(tool.function(SimpleNamespace(deps=deps)))

    assert seen[0].headers["X-Other-Case"] == "case-1" and "X-Glazed-Case" not in seen[0].headers
