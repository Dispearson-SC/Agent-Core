"""Glazed tools through the HTTP bridge: bound to the turn, never to model-chosen store/case/as_of.

Subject: addons/glazed_tools (eight per-role toolsets generated from the backend's OpenAPI document
plus overlay.yaml). A port of `test_glazed_tools.py` of vertical/glazed: the same behaviors, the
same assertions, now through the addon's toolsets. No network: `httpx.MockTransport` stands in
for the backend and the OpenAPI document is the committed snapshot of its internal contract.

Where an assertion had to change, the test says so in a `CHANGED:` note (see docs/DECISIONS.md D87).
One old test is not ported: `test_second_ask_peer_of_a_step_is_a_deterministic_result_not_a_retry`
exercised `peers._guarded_ask_peer`, a patch of the vertical branch's Core that this Core replaced
with real fan-out (`test_peer_fanout.py`); it never touched a glazed tool.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import AddonContributions, register_addons
from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.http_bridge import case_id_for, clear_openapi_cache
from agent_core.composition import TOOL_PACKAGES, served_tool_packages
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId

HERE = Path(__file__).resolve()
ADDONS_DIR = HERE.parents[3] / "addons"
SNAPSHOT = HERE.parents[1] / "fixtures" / "glazed_tools" / "internal_openapi.json"

ROLES = {
    "orchestrator": {"propose_action", "classify_text", "get_briefing", "save_manager_note"},
    "present": {
        "get_snapshot",
        "get_kpis",
        "get_day_summary",
        "get_issues",
        "explain_metric",
        "get_expiring",
        "get_outlook",
    },
    "past": {"get_snapshot", "get_history", "evaluate_promo", "recall_experiences"},
    "supply": {
        "get_snapshot",
        "get_order_plan",
        "project_inventory",
        "get_supplier_performance",
        "get_issues",
        "forecast_demand",
        "get_inventory_status",
        "get_expiring",
        "get_deliveries",
        "get_issue_analysis",
    },
    "strategist": {
        "get_snapshot",
        "get_kpis",
        "get_day_summary",
        "evaluate_promo",
        "audit_promo",
        "forecast_daily_flow",
        "get_staffing",
        "get_promos",
        "get_outlook",
    },
    "sentinel": {"get_history", "record_event", "check_compliance", "classify_text"},
    "auditor": {"get_integrity_issues"},
    "liaison": {"get_transfer_options", "propose_transfer"},
}
FORBIDDEN_PARAMS = {"store_id", "store", "as_of", "case", "case_id", "tenant", "tenant_id"}
HELPER = "agent_core_addons.glazed_tools.toolsets._glazed"


def _ctx(session_id: str = "case-77", agent: str = "glazed_supply") -> Any:
    deps = TurnContext(
        caller=CallerIdentity("m-1", "glazed", TenantId("S030"), frozenset({"peer"})),
        session=SessionRef(session_id=session_id, tenant_id=TenantId("S030")),  # type: ignore[arg-type]
        agent_id=agent,
    )
    return SimpleNamespace(deps=deps)


class Backend:
    def __init__(self, status: int = 200, body: Any = None, error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.status, self.body, self.error = status, body if body is not None else {}, error
        self.headers: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.body, headers=self.headers)


class Glazed:
    """The addon as the root registers it: `tool(name)` is a callable of the generated tool."""

    def __init__(self, contributions: AddonContributions, helper: ModuleType) -> None:
        self.contributions, self.helper = contributions, helper

    def toolset(self, role: str) -> Any:
        # built fresh, like every toolset builder, so a test can swap the transport between calls
        return self.contributions.toolsets[f"glazed_{role}"]()

    def tool(self, name: str) -> Any:
        for role, names in ROLES.items():
            if name in names:
                return self.toolset(role).tools[name]
        raise KeyError(name)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        tool = self.tool(name)

        def call(ctx: Any, **arguments: Any) -> Any:
            return tool.function(ctx, **arguments)

        return call


@pytest.fixture(autouse=True)
def _fresh_document_cache() -> None:
    clear_openapi_cache()


ENVIRONMENT = {
    "AGENT_CORE_ADDONS": "http_tool_bridge,glazed_tools",
    "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR),
    "AGENT_CORE_ADDON__GLAZED_TOOLS__BASE_URL": "http://backend.test:9",
    "AGENT_CORE_ADDON__GLAZED_TOOLS__OPENAPI_URL": str(SNAPSHOT),
}


@pytest.fixture(scope="module")
def registered() -> tuple[AddonContributions, ModuleType]:
    """The addon as the root registers it, once: real loader, real registration, real Core tools."""
    loaded = load_addons_from_environment(dict(ENVIRONMENT))
    contributions = register_addons(
        loaded, builtin_toolsets=TOOL_PACKAGES, builtin_fragments_dir=None, reserved_channels=()
    )
    return contributions, sys.modules[HELPER]


@pytest.fixture
def gt(
    registered: tuple[AddonContributions, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> Glazed:
    contributions, helper = registered
    monkeypatch.setattr(helper, "TRANSPORT", None)
    return Glazed(contributions, helper)


@pytest.fixture
def backend(gt: Glazed, monkeypatch: pytest.MonkeyPatch) -> Backend:
    b = Backend()
    monkeypatch.setattr(gt.helper, "TRANSPORT", httpx.MockTransport(b))
    return b


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.mark.parametrize("role", sorted(ROLES))
def test_each_role_gets_exactly_its_tools_and_no_forbidden_parameter(gt: Glazed, role: str) -> None:
    toolset = gt.toolset(role)
    assert set(toolset.tools) == ROLES[role]
    for tool in toolset.tools.values():
        props = set(tool.function_schema.json_schema.get("properties", {}))
        assert not props & FORBIDDEN_PARAMS, (tool.name, props)


def test_headers_carry_case_agent_and_store_from_the_turn(gt: Glazed, backend: Backend) -> None:
    _run(gt.get_kpis(_ctx(), date_from="2026-03-01", date_to="2026-03-07"))

    (req,) = backend.requests
    assert req.url.path == "/internal/v1/kpis"
    assert dict(req.url.params) == {"date_from": "2026-03-01", "date_to": "2026-03-07"}
    assert req.headers["X-Glazed-Case"] == "case-77"
    assert req.headers["X-Glazed-Agent"] == "glazed_supply"
    assert req.headers["X-Glazed-Store"] == "S030"
    assert str(req.url).startswith("http://backend.test:9/")


def test_a_peer_turn_resolves_the_asking_case(gt: Glazed, backend: Backend) -> None:
    _run(gt.get_order_plan(_ctx("peer~case-77~0f1e")))

    assert backend.requests[0].headers["X-Glazed-Case"] == "case-77"


def test_default_backend_url() -> None:
    """CHANGED: the default is the manifest's `base_url` setting, not a function of the environment.

    The tool code reads no environment; the operator sets AGENT_CORE_ADDON__GLAZED_TOOLS__BASE_URL.
    """
    loaded = load_addons_from_environment(
        {
            "AGENT_CORE_ADDONS": "http_tool_bridge,glazed_tools",
            "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR),
        }
    )
    (addon,) = (a for a in loaded if a.manifest.name == "glazed_tools")

    assert addon.settings["base_url"] == "http://backend:8080"


@pytest.mark.parametrize(
    ("call", "path", "params"),
    [
        (
            lambda g, c: g.get_day_summary(c, date="2026-03-02"),
            "/internal/v1/day-summary",
            {"date": "2026-03-02"},
        ),
        (lambda g, c: g.get_issues(c, limit=5), "/internal/v1/issues", {"limit": "5"}),
        (
            lambda g, c: g.project_inventory(c, sku="SKU-1", horizon=3),
            "/internal/v1/inventory/projection",
            {"sku": "SKU-1", "horizon": "3"},
        ),
        (lambda g, c: g.get_order_plan(c), "/internal/v1/order-plan", {}),
        (lambda g, c: g.get_supplier_performance(c), "/internal/v1/suppliers/performance", {}),
        (
            lambda g, c: g.evaluate_promo(c, promo_id="P9"),
            "/internal/v1/promos/P9/evaluation",
            {},
        ),
        (lambda g, c: g.get_history(c), "/internal/v1/history", {}),
        (
            lambda g, c: g.explain_metric(c, metric="waste_pct"),
            "/internal/v1/metrics/explain",
            {"metric": "waste_pct"},
        ),
    ],
)
def test_read_tools_hit_the_contract_endpoints(
    gt: Glazed, backend: Backend, call: Any, path: str, params: dict[str, str]
) -> None:
    _run(call(gt, _ctx()))

    req = backend.requests[0]
    assert req.method == "GET"
    assert req.url.path == path
    assert dict(req.url.params) == params


def test_promo_id_cannot_escape_its_path_segment(gt: Glazed, backend: Backend) -> None:
    _run(gt.evaluate_promo(_ctx(), promo_id="../../x"))

    assert backend.requests[0].url.raw_path == b"/internal/v1/promos/..%2F..%2Fx/evaluation"


def test_recall_experiences_queries_memory_and_keeps_the_warning(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = []
    backend.headers = {"X-Glazed-Warning": "memory unavailable"}

    result = _run(gt.recall_experiences(_ctx(agent="glazed_past"), query="combo PR004", limit=3))

    req = backend.requests[0]
    assert req.url.path == "/internal/v1/experiences"
    assert dict(req.url.params) == {"query": "combo PR004", "limit": "3"}
    assert result == {"experiences": [], "warning": "memory unavailable"}


def test_recall_experiences_without_warning_returns_the_entries(
    gt: Glazed, backend: Backend
) -> None:
    entry = {"text": "combo failed", "decision_id": "D1", "kind": "promo", "created_at": "x"}
    backend.body = [entry]

    assert _run(gt.recall_experiences(_ctx(), query="combo")) == {"experiences": [entry]}


def test_recall_experiences_limit_is_bounded(gt: Glazed, backend: Backend) -> None:
    _run(gt.recall_experiences(_ctx(), query="x", limit=10_000))

    assert dict(backend.requests[0].url.params)["limit"] == "20"


def test_record_event_posts_json(gt: Glazed, backend: Backend) -> None:
    _run(
        gt.record_event(
            _ctx(), type="holiday", date_from="2026-03-10", date_to="2026-03-12", note="puente"
        )
    )

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("POST", "/internal/v1/events")
    assert json.loads(req.content) == {
        "type": "holiday",
        "date_from": "2026-03-10",
        "date_to": "2026-03-12",
        "note": "puente",
    }


def test_propose_action_posts_a_proposal_and_never_executes(gt: Glazed, backend: Backend) -> None:
    _run(
        gt.propose_action(
            _ctx(agent="glazed_orchestrator"),
            issue_id="I1",
            option_id="O2",
            rationale="stockout in 2 days",
        )
    )

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("POST", "/internal/v1/proposals")
    assert json.loads(req.content) == {
        "issue_id": "I1",
        "option_id": "O2",
        "rationale": "stockout in 2 days",
    }
    assert len(backend.requests) == 1


def test_http_error_becomes_an_explainable_result(gt: Glazed, backend: Backend) -> None:
    backend.status, backend.body = 404, {"detail": "unknown case"}

    result = _run(gt.get_kpis(_ctx(), date_from="2026-03-01", date_to="2026-03-07"))

    assert result["error"] and result["status"] == 404
    assert "unknown case" in result["error"]


def test_connection_failure_becomes_an_error_result(gt: Glazed, backend: Backend) -> None:
    backend.error = httpx.ConnectError("refused")

    result = _run(gt.get_order_plan(_ctx()))

    assert "unreachable" in result["error"]


def test_timeout_becomes_an_error_result(gt: Glazed, backend: Backend) -> None:
    backend.error = httpx.ReadTimeout("slow")

    assert "timed out" in _run(gt.get_order_plan(_ctx()))["error"]


def test_a_non_json_response_becomes_an_error_result(gt: Glazed, backend: Backend) -> None:
    def text_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    gt.helper.TRANSPORT = httpx.MockTransport(text_response)  # type: ignore[attr-defined]

    assert "non-JSON" in _run(gt.get_order_plan(_ctx()))["error"]


def test_an_unexpected_exception_never_escapes_a_tool(gt: Glazed, backend: Backend) -> None:
    """A tool that raises strands the whole turn as 'running'; every class must come back
    as an error result: transport bugs, invalid URLs, anything not an httpx error."""
    for error in (
        RuntimeError("boom"),
        ValueError("odd"),
        httpx.InvalidURL("bad url"),
        UnicodeError("bytes"),
    ):
        backend.error = error
        result = _run(gt.get_order_plan(_ctx()))
        assert "error" in result, type(error).__name__


def test_a_body_that_cannot_be_serialised_becomes_an_error_result(
    gt: Glazed, backend: Backend
) -> None:
    """CHANGED: the bridge types every body value from the schema, and a `string` argument takes
    `str(value)`, so an `object()` rationale is sent as its text instead of failing before the
    request (the old tool let httpx raise and turned it into an error value). What the old test
    protected still holds: the tool never raises, whatever the model puts in an argument.
    """
    result = _run(
        gt.propose_action(_ctx(), issue_id="I1", option_id="O1", rationale=object())  # type: ignore[arg-type]
    )

    assert "error" not in result
    (req,) = backend.requests
    assert json.loads(req.content)["rationale"].startswith("<object object at")


@pytest.mark.parametrize("limit", ["many", None, [3]])
def test_a_non_numeric_limit_is_bounded_not_raised(
    gt: Glazed, backend: Backend, limit: Any
) -> None:
    assert "error" not in _run(gt.get_issues(_ctx(), limit=limit))
    assert "error" not in _run(gt.recall_experiences(_ctx(), query="q", limit=limit))


def test_no_turn_context_is_refused_without_a_request(gt: Glazed, backend: Backend) -> None:
    result = _run(gt.get_order_plan(SimpleNamespace(deps=None)))

    assert "turn context" in result["error"]
    assert backend.requests == []


def test_save_manager_note_posts_the_note_without_store_or_date(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {"saved": True, "memory_id": "m1", "event_id": "e1", "category": "eventos"}
    result = _run(
        gt.save_manager_note(
            _ctx(agent="glazed_orchestrator"),
            text="Cerramos el 19",
            date_from="2026-07-19",
            date_to="2026-07-19",
        )
    )

    (req,) = backend.requests
    assert (req.method, req.url.path) == ("POST", "/internal/v1/notes")
    assert json.loads(req.content) == {
        "text": "Cerramos el 19",
        "category": "eventos",
        "date_from": "2026-07-19",
        "date_to": "2026-07-19",
    }
    assert req.headers["X-Glazed-Store"] == "S030"
    assert result["saved"] is True


def test_save_manager_note_omits_unset_optional_fields(gt: Glazed, backend: Backend) -> None:
    _run(gt.save_manager_note(_ctx(agent="glazed_orchestrator"), text="hola"))

    assert json.loads(backend.requests[0].content) == {"text": "hola", "category": "eventos"}


def test_get_transfer_options_gets_options_with_sku_and_limit(gt: Glazed, backend: Backend) -> None:
    backend.body = {"options": [], "do_nothing": {}, "assumptions": {}}
    _run(gt.get_transfer_options(_ctx(agent="glazed_liaison"), sku="SKU-1", limit=3))
    _run(gt.get_transfer_options(_ctx(agent="glazed_liaison")))

    first, second = backend.requests
    assert (first.method, first.url.path) == ("GET", "/internal/v1/transfers/options")
    assert dict(first.url.params) == {"sku": "SKU-1", "limit": "3"}
    assert dict(second.url.params) == {"limit": "5"}


def test_propose_transfer_posts_ids_with_a_stable_idempotency_key(
    gt: Glazed, backend: Backend
) -> None:
    for _ in range(2):
        _run(
            gt.propose_transfer(
                _ctx(agent="glazed_liaison"), option_id="T1", rationale="sobra aqui"
            )
        )
    _run(gt.propose_transfer(_ctx(agent="glazed_liaison"), option_id="T2", rationale="sobra aqui"))

    first, again, other = backend.requests
    assert (first.method, first.url.path) == ("POST", "/internal/v1/transfers/proposals")
    assert json.loads(first.content) == {"option_id": "T1", "rationale": "sobra aqui"}
    assert first.headers["Idempotency-Key"] == again.headers["Idempotency-Key"]
    assert first.headers["Idempotency-Key"] != other.headers["Idempotency-Key"]


def test_placeholder_liaison_tools_are_gone(gt: Glazed) -> None:
    """CHANGED: no module attribute to look at; the tools must be in no toolset."""
    every_tool = {name for role in ROLES for name in gt.toolset(role).tools}

    assert "offer_surplus" not in every_tool
    assert "request_stock" not in every_tool


def test_registered_in_the_composition_root(gt: Glazed) -> None:
    """CHANGED: the toolsets reach the root through the addon contributions, not TOOL_PACKAGES."""
    packages = served_tool_packages(gt.contributions)

    for role in ROLES:
        assert f"glazed_{role}" in packages
        assert f"glazed_{role}" not in TOOL_PACKAGES
        assert set(packages[f"glazed_{role}"]().tools) == ROLES[role]


@pytest.mark.parametrize(
    ("session_id", "case"),
    [
        ("", ""),
        ("case-77", "case-77"),
        ("peer~case-77~u1", "case-77"),
        ("peer~", ""),
        ("peer~case-77", "case-77"),
        ("peer~peer~case-77~u1~u2", "case-77"),
        ("peer~a~b~u1", "a~b"),
    ],
)
def test_case_id_for_edge_cases(session_id: str, case: str) -> None:
    """CHANGED: the bridge's generic `case_id_for(session_id, prefix, separator)`, with glazed's."""
    assert case_id_for(session_id, "peer~", "~") == case


def test_check_compliance_reads_all_decisions_or_one(gt: Glazed, backend: Backend) -> None:
    _run(gt.check_compliance(_ctx(agent="glazed_sentinel")))
    _run(gt.check_compliance(_ctx(agent="glazed_sentinel"), decision_id="D-1"))

    first, second = backend.requests
    assert (first.method, first.url.path) == ("GET", "/internal/v1/compliance")
    assert dict(first.url.params) == {}
    assert dict(second.url.params) == {"decision_id": "D-1"}


def test_classify_text_posts_kind_and_text(gt: Glazed, backend: Backend) -> None:
    _run(gt.classify_text(_ctx(agent="glazed_sentinel"), kind="rejection_reason", text="muy caro"))

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("POST", "/internal/v1/classify")
    assert json.loads(req.content) == {"kind": "rejection_reason", "text": "muy caro"}


def test_classify_text_rejects_an_unknown_kind_without_calling_the_backend(
    gt: Glazed, backend: Backend
) -> None:
    result = _run(gt.classify_text(_ctx(), kind="mood", text="x"))

    assert "rejection_reason" in result["error"] and "deviation_cause" in result["error"]
    assert backend.requests == []


def test_classify_text_refuses_an_unknown_kind_in_the_old_tools_words(
    gt: Glazed, backend: Backend
) -> None:
    """G6 golden turn: the bridge's generic enum message dropped the old tool's final period."""
    result = _run(gt.classify_text(_ctx(), kind="mood", text="x"))

    assert result == {"error": "kind must be one of rejection_reason, deviation_cause."}


def test_audit_promo_posts_department_and_discount(gt: Glazed, backend: Backend) -> None:
    _run(gt.audit_promo(_ctx(agent="glazed_strategist"), department="bakery", discount_pct=15))

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("POST", "/internal/v1/promos/audit")
    assert json.loads(req.content) == {"department": "bakery", "discount_pct": 15.0}


@pytest.mark.parametrize("bad", ["abc", None, float("nan"), -5, 101])
def test_audit_promo_refuses_a_junk_discount_without_raising(
    gt: Glazed, backend: Backend, bad: Any
) -> None:
    result = _run(gt.audit_promo(_ctx(), department="bakery", discount_pct=bad))

    assert "error" in result
    assert backend.requests == []


def test_get_integrity_issues_filters_by_category(gt: Glazed, backend: Backend) -> None:
    _run(gt.get_integrity_issues(_ctx(agent="glazed_auditor")))

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("GET", "/internal/v1/issues")
    assert dict(req.url.params) == {"category": "integrity"}


@pytest.mark.parametrize("horizon", ["abc", None, 0, -4, 10_000, float("nan")])
def test_project_inventory_clamps_a_junk_horizon_instead_of_raising(
    gt: Glazed, backend: Backend, horizon: Any
) -> None:
    _run(gt.project_inventory(_ctx(), sku="SKU-1", horizon=horizon))

    assert 1 <= int(dict(backend.requests[0].url.params)["horizon"]) <= 30


def test_propose_action_sends_a_stable_idempotency_key(gt: Glazed, backend: Backend) -> None:
    ctx = _ctx(agent="glazed_orchestrator")
    for _ in range(2):
        _run(gt.propose_action(ctx, issue_id="I1", option_id="O2", rationale="why"))
    _run(gt.propose_action(ctx, issue_id="I1", option_id="O3", rationale="why"))
    _run(
        gt.propose_action(
            _ctx("case-78", "glazed_orchestrator"), issue_id="I1", option_id="O2", rationale="why"
        )
    )

    keys = [r.headers["Idempotency-Key"] for r in backend.requests]
    assert keys[0] == keys[1]
    assert len(set(keys)) == 3  # a different option or case is a different proposal
    assert "case-77" not in keys[0]


def test_read_calls_send_no_idempotency_key(gt: Glazed, backend: Backend) -> None:
    _run(gt.get_history(_ctx()))

    assert "Idempotency-Key" not in backend.requests[0].headers


def test_get_issues_defaults_to_operational_without_extra_params(
    gt: Glazed, backend: Backend
) -> None:
    _run(gt.get_issues(_ctx()))

    params = dict(backend.requests[0].url.params)
    assert "category" not in params and "kind" not in params


@pytest.mark.parametrize("category", ["operational", "integrity", "all"])
def test_get_issues_maps_category_and_kind_to_the_backend(
    gt: Glazed, backend: Backend, category: str
) -> None:
    _run(gt.get_issues(_ctx(), limit=5, category=category, kind="sku_key_mismatch"))

    params = dict(backend.requests[0].url.params)
    assert params["category"] == category
    assert params["kind"] == "sku_key_mismatch"
    assert params["limit"] == "5"


def test_get_issues_rejects_an_unknown_category_without_calling_the_backend(
    gt: Glazed, backend: Backend
) -> None:
    result = _run(gt.get_issues(_ctx(), limit=5, category="everything"))

    assert "error" in result and "category" in result["error"]
    assert backend.requests == []


def test_get_kpis_description_says_it_takes_a_date_window(gt: Glazed) -> None:
    """CHANGED: the model reads the tool description (the old test read the function docstring)."""
    doc = gt.tool("get_kpis").description or ""
    assert "date_from" in doc and "date_to" in doc and "window" in doc


def test_get_snapshot_and_get_briefing_take_no_model_parameter(
    gt: Glazed, backend: Backend
) -> None:
    _run(gt.get_snapshot(_ctx()))
    _run(gt.get_briefing(_ctx(agent="glazed_orchestrator")))

    snapshot, briefing = backend.requests
    assert snapshot.url.path == "/internal/v1/snapshot"
    assert briefing.url.path == "/internal/v1/briefing"
    assert dict(snapshot.url.params) == {} and dict(briefing.url.params) == {}
    # CHANGED: the schema the model sees has no property (the old test read the signature)
    assert not gt.tool("get_snapshot").function_schema.json_schema["properties"]
    assert not gt.tool("get_briefing").function_schema.json_schema["properties"]


# ---- forecast tools ------------------------------------------------------------------

_DEMAND_BODY = {
    "sku": "SKU-1",
    "target_date": "2026-03-10",
    "qty_range": {"p10": 8, "p50": 12, "p90": 19},
    "quantiles": {"p05": 6, "p95": 22},
    "censored_share": 0.18,
    "weather_kind": "observed",
    "model": "lgbm-quantile",
    "model_version": "2026.03.1",
    "forecast_origin": "2026-03-07",
    "internal_debug": "must not leak",
}
_FLOW_BODY = {
    "target_date": "2026-03-10",
    "transactions": {"p10": 90, "p50": 120, "p90": 150},
    "avg_ticket": {"p10": 8.0, "p50": 9.5, "p90": 11.0},
    "granularity": "daily",
    "model": "lgbm-quantile",
    "forecast_origin": "2026-03-07",
}
# CHANGED: the model sends an ISO string (the generated schema types a date as a string); the old
# tools took a `datetime.date`, which the framework parsed from the same string.
_TARGET = dt.date(2026, 3, 10).isoformat()


def test_forecast_demand_calls_the_contract_endpoint_without_store_or_as_of(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = _DEMAND_BODY
    _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))

    (req,) = backend.requests
    assert req.method == "GET"
    assert req.url.path == "/internal/v1/forecast/demand"
    assert dict(req.url.params) == {"sku": "SKU-1", "target_date": "2026-03-10"}
    assert req.headers["X-Glazed-Store"] == "S030"
    assert req.headers["X-Glazed-Case"] == "case-77"


def test_forecast_demand_returns_the_documented_shape(gt: Glazed, backend: Backend) -> None:
    backend.body = _DEMAND_BODY
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))

    assert out["qty"] == 12
    assert out["qty_range"] == {"p10": 8, "p50": 12, "p90": 19}
    assert out["quantiles"] == {"p05": 6, "p95": 22}
    assert out["censored_share"] == 0.18
    assert out["weather_kind"] == "observed"
    assert (out["model"], out["model_version"]) == ("lgbm-quantile", "2026.03.1")
    assert (out["sku"], out["target_date"], out["forecast_origin"]) == (
        "SKU-1",
        "2026-03-10",
        "2026-03-07",
    )
    assert "internal_debug" not in out


def test_forecast_demand_omits_optional_quantiles_when_absent(gt: Glazed, backend: Backend) -> None:
    backend.body = {k: v for k, v in _DEMAND_BODY.items() if k != "quantiles"}
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert "quantiles" not in out


@pytest.mark.parametrize("status", [400, 422])
def test_forecast_demand_backend_rejecting_a_non_future_date_is_a_clear_error(
    gt: Glazed, backend: Backend, status: int
) -> None:
    backend.status = status
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert "future" in out["error"].lower()


def test_forecast_demand_refuses_a_date_not_after_the_forecast_origin(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {**_DEMAND_BODY, "forecast_origin": "2026-03-10"}
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert "future" in out["error"].lower()
    assert "qty" not in out


@pytest.mark.parametrize("status", [404, 503])
def test_forecast_demand_unavailable_on_404_503(gt: Glazed, backend: Backend, status: int) -> None:
    backend.status = status
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert out["forecast_available"] is False
    assert out["sku"] == "SKU-1"
    assert "qty" not in out and "qty_range" not in out


def test_forecast_demand_unavailable_when_backend_unreachable(gt: Glazed, backend: Backend) -> None:
    backend.error = httpx.ConnectError("boom")
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert out["forecast_available"] is False


def test_forecast_demand_without_a_range_is_unavailable_never_invented(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {"sku": "SKU-1", "model": "m"}
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert out["forecast_available"] is False


def test_forecast_demand_other_backend_errors_stay_errors(gt: Glazed, backend: Backend) -> None:
    backend.status = 500
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    assert "error" in out and "forecast_available" not in out


def test_forecast_demand_an_unreadable_date_is_an_error_without_a_request(
    gt: Glazed, backend: Backend
) -> None:
    """NEW: the old tool let the framework reject a bad `datetime.date`; here the tool says so."""
    out = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date="next tuesday"))
    assert "target_date" in out["error"]
    assert backend.requests == []


def test_forecast_daily_flow_calls_the_contract_endpoint(gt: Glazed, backend: Backend) -> None:
    backend.body = _FLOW_BODY
    _run(gt.forecast_daily_flow(_ctx(agent="glazed_strategist"), target_date=_TARGET))

    (req,) = backend.requests
    assert req.url.path == "/internal/v1/forecast/flow"
    assert dict(req.url.params) == {"target_date": "2026-03-10"}
    assert req.headers["X-Glazed-Agent"] == "glazed_strategist"


def test_forecast_daily_flow_returns_the_documented_shape_with_null_footfall(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = _FLOW_BODY
    out = _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))

    assert out["transactions"] == {"p10": 90, "p50": 120, "p90": 150}
    assert out["footfall"] is None
    assert out["avg_ticket"]["p50"] == 9.5
    assert out["granularity"] == "daily"
    assert (out["model"], out["forecast_origin"]) == ("lgbm-quantile", "2026-03-07")
    assert out["target_date"] == "2026-03-10"


def test_forecast_daily_flow_keeps_footfall_when_present(gt: Glazed, backend: Backend) -> None:
    backend.body = {**_FLOW_BODY, "footfall": {"p10": 100, "p50": 150, "p90": 200}}
    out = _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))
    assert out["footfall"] == {"p10": 100, "p50": 150, "p90": 200}


def test_forecast_daily_flow_future_only_and_unavailable(gt: Glazed, backend: Backend) -> None:
    backend.status = 422
    assert "future" in _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))["error"].lower()
    backend.status = 503
    out = _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))
    assert out["forecast_available"] is False and "transactions" not in out
    backend.status, backend.error = 200, httpx.ConnectError("x")
    assert _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))["forecast_available"] is False


def test_forecast_daily_flow_refuses_a_date_not_after_the_forecast_origin(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {**_FLOW_BODY, "forecast_origin": "2026-03-11"}
    assert "future" in _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))["error"].lower()


# ---- manager views: inventory, expiring, deliveries, staffing, promos, outlook, analysis ----

_V = "/internal/v1"
_ENVELOPE = {
    "as_of": "2026-03-07",
    "store_id": "S030",
    "store_display_name": "Tienda Centro",
    "source": "internal-debug",
}


@pytest.mark.parametrize(
    ("call", "path", "params"),
    [
        (lambda g, c: g.get_inventory_status(c), f"{_V}/inventory", {"limit": "15"}),
        (
            lambda g, c: g.get_inventory_status(c, sort="slow", limit=5),
            f"{_V}/inventory",
            {"sort": "slow", "limit": "5"},
        ),
        (lambda g, c: g.get_expiring(c), f"{_V}/expiring", {"days": "7", "limit": "10"}),
        (lambda g, c: g.get_expiring(c, days=3), f"{_V}/expiring", {"days": "3", "limit": "10"}),
        (lambda g, c: g.get_deliveries(c, days=5), f"{_V}/deliveries", {"days": "5"}),
        (lambda g, c: g.get_staffing(c, days=4), f"{_V}/staffing", {"days": "4"}),
        (lambda g, c: g.get_outlook(c, days=2), f"{_V}/outlook", {"days": "2"}),
        (
            lambda g, c: g.get_promos(c, status="active", days=30),
            f"{_V}/promos",
            {"status": "active", "days": "30", "limit": "10"},
        ),
        (lambda g, c: g.get_promos(c), f"{_V}/promos", {"days": "14", "limit": "10"}),
        (
            lambda g, c: g.get_issue_analysis(c, issue_id="ISS-1"),
            f"{_V}/issues/ISS-1/analysis",
            {},
        ),
    ],
)
def test_manager_view_tools_hit_the_internal_twins_without_store_or_as_of(
    gt: Glazed, backend: Backend, call: Any, path: str, params: dict[str, str]
) -> None:
    backend.body = {**_ENVELOPE}
    _run(call(gt, _ctx()))

    (req,) = backend.requests
    assert req.method == "GET"
    assert req.url.path == path
    assert dict(req.url.params) == params
    assert req.headers["X-Glazed-Store"] == "S030"
    assert req.headers["X-Glazed-Case"] == "case-77"
    assert req.headers["X-Glazed-Agent"] == "glazed_supply"
    assert "store_id" not in req.url.params and "as_of" not in req.url.params


def test_issue_id_cannot_escape_its_path_segment(gt: Glazed, backend: Backend) -> None:
    backend.body = {**_ENVELOPE}
    _run(gt.get_issue_analysis(_ctx(), issue_id="../x"))

    assert backend.requests[0].url.raw_path == b"/internal/v1/issues/..%2Fx/analysis"


@pytest.mark.parametrize(
    ("call", "params"),
    [
        (lambda g, c: g.get_expiring(c, days=999), {"days": "30", "limit": "10"}),
        (lambda g, c: g.get_expiring(c, days="junk"), {"days": "7", "limit": "10"}),
        (lambda g, c: g.get_deliveries(c, days=0), {"days": "1"}),
        (lambda g, c: g.get_deliveries(c, days=99), {"days": "14"}),
        (lambda g, c: g.get_staffing(c, days=99), {"days": "14"}),
        (lambda g, c: g.get_outlook(c, days=99), {"days": "7"}),
        (lambda g, c: g.get_outlook(c, days=None), {"days": "2"}),
        (lambda g, c: g.get_inventory_status(c, sort=None, limit=9999), {"limit": "50"}),
        (lambda g, c: g.get_inventory_status(c, sort=None, limit="x"), {"limit": "15"}),
        (lambda g, c: g.get_promos(c, status=None, days=9999), {"days": "365", "limit": "10"}),
    ],
)
def test_junk_or_oversized_window_arguments_are_clamped_not_raised(
    gt: Glazed, backend: Backend, call: Any, params: dict[str, str]
) -> None:
    backend.body = {**_ENVELOPE}
    _run(call(gt, _ctx()))

    assert dict(backend.requests[0].url.params) == params


def test_unknown_inventory_sort_and_promo_status_are_refused_without_a_request(
    gt: Glazed, backend: Backend
) -> None:
    assert "sort" in _run(gt.get_inventory_status(_ctx(), sort="random"))["error"]
    assert "status" in _run(gt.get_promos(_ctx(), status="someday"))["error"]
    assert backend.requests == []


def test_inventory_status_is_compact_and_keeps_cover_status_and_next_delivery(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {
        **_ENVELOPE,
        "coverage": {"skus_with_inventory": 40, "skus_selling": 38},
        "status_rule": "long text",
        "items": [
            {
                "sku": "SKU-1",
                "product_name": "Dona",
                "department": "Bakery",
                "on_hand": 12,
                "avg_daily_demand": 6.0,
                "days_of_cover": 2.0,
                "status": "critical",
                "status_reason": "runs out before delivery",
                "next_delivery_date": "2026-03-09",
                "next_delivery_source": "po",
                "scheduled_in_units": 24,
                "forecast_outflow_units": 18,
                "projected_stockout_date": "2026-03-09",
                "turnover_28d": 4.1,
                "weeks_of_supply": 0.3,
                "inventory_value_usd": 10.0,
                "last_sale_date": "2026-03-07",
                "slow_mover": False,
                "internal_debug": "leak",
            }
        ],
    }
    out = _run(gt.get_inventory_status(_ctx()))

    assert out["available"] is True
    assert out["store_display_name"] == "Tienda Centro"
    assert out["as_of"] == "2026-03-07"
    assert out["coverage"] == {"skus_with_inventory": 40, "skus_selling": 38}
    assert "source" not in out and "store_id" not in out and "status_rule" not in out
    (item,) = out["items"]
    assert item["days_of_cover"] == 2.0
    assert item["status"] == "critical"
    assert item["next_delivery_date"] == "2026-03-09"
    assert item["on_hand"] == 12
    assert "internal_debug" not in item


def test_expiring_keeps_waste_ranges_by_day_and_the_total(gt: Glazed, backend: Backend) -> None:
    backend.body = {
        **_ENVELOPE,
        "days": 7,
        "total_items": 12,
        "total_waste_usd": {"low": 1.0, "mid": 5.0, "high": 9.0},
        "by_day": [
            {
                "date": "2026-03-08",
                "waste_usd": {"low": 1.0, "mid": 2.0, "high": 3.0},
                "projected": True,
            }
        ],
        "items": [
            {
                "sku": "SKU-1",
                "product_name": "Dona",
                "expiring_units": 4,
                "expiry_date": "2026-03-08",
                "waste_units": {"low": 0, "mid": 1, "high": 2},
                "waste_usd": {"low": 0.0, "mid": 1.5, "high": 3.0},
                "lot_age_inferred": True,
                "projected": True,
                "shelf_life_days": 3,
            }
        ],
    }
    out = _run(gt.get_expiring(_ctx()))

    assert out["available"] is True
    assert out["total_items"] == 12
    assert out["total_waste_usd"] == {"low": 1.0, "mid": 5.0, "high": 9.0}
    assert out["by_day"][0]["waste_usd"]["mid"] == 2.0
    assert out["items"][0]["waste_usd"] == {"low": 0.0, "mid": 1.5, "high": 3.0}
    assert out["items"][0]["expiry_date"] == "2026-03-08"
    assert "store_id" not in out and "source" not in out


def test_deliveries_keep_the_calendar_per_day(gt: Glazed, backend: Backend) -> None:
    backend.body = {
        **_ENVELOPE,
        "window_days": 7,
        "late_lookback_days": 3,
        "days": [
            {
                "date": "2026-03-09",
                "projected": True,
                "deliveries": [
                    {
                        "supplier_id": "SUP-1",
                        "supplier_name": "Panificadora",
                        "po_id": "PO-1",
                        "kind": "open_po",
                        "status": "expected",
                        "value_usd": 120.0,
                        "expected_date": "2026-03-09",
                        "lines": [{"sku": "SKU-1", "product_name": "Dona", "qty": 10}],
                    }
                ],
            }
        ],
    }
    out = _run(gt.get_deliveries(_ctx(), days=7))

    assert out["available"] is True
    (day,) = out["days"]
    assert day["date"] == "2026-03-09"
    assert day["deliveries"][0]["supplier_name"] == "Panificadora"
    assert day["deliveries"][0]["lines"][0]["qty"] == 10
    assert "store_id" not in out


def test_staffing_keeps_recommended_hours_by_day_and_says_daily_only(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {
        **_ENVELOPE,
        "benchmark": {"format": "mall", "tx_per_staff_hour_p50": 11.2, "n_store_days": 40},
        "note": "sin datos por hora",
        "planned_hours_note": "no roster",
        "days": [
            {
                "date": "2026-03-08",
                "forecast_transactions": {"low": 90, "mid": 120, "high": 150},
                "recommended_staff_hours": {"low": 70.0, "mid": 90.0, "high": 110.0},
                "planned_staff_hours": None,
                "gap_hours": None,
                "basis": "daily",
                "projected": True,
            }
        ],
    }
    out = _run(gt.get_staffing(_ctx(agent="glazed_strategist"), days=7))

    assert out["available"] is True
    assert out["days"][0]["recommended_staff_hours"]["mid"] == 90.0
    assert out["days"][0]["basis"] == "daily"
    assert out["benchmark"]["tx_per_staff_hour_p50"] == 11.2
    assert out["note"] == "sin datos por hora"


def test_promos_keep_the_audit_verdict_and_evaluation(gt: Glazed, backend: Backend) -> None:
    backend.body = {
        **_ENVELOPE,
        "window_days": 14,
        "items": [
            {
                "promo_id": "PR1",
                "name": "2x1 donas",
                "department": "Bakery",
                "category": "bundle",
                "discount_pct": 20,
                "start_date": "2026-02-20",
                "end_date": "2026-03-01",
                "status": "past",
                "audit_verdict": "block",
                "evaluation": {"uplift_pct": 2.0, "expected_uplift_pct": 8.0, "confidence": 0.4},
                "projected": False,
            }
        ],
    }
    out = _run(gt.get_promos(_ctx(agent="glazed_strategist"), status="past"))

    assert out["available"] is True
    (promo,) = out["items"]
    assert promo["audit_verdict"] == "block"
    assert promo["evaluation"]["uplift_pct"] == 2.0
    assert promo["status"] == "past"


def test_outlook_keeps_sales_waste_lost_sales_and_the_weather_note(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {
        **_ENVELOPE,
        "days": [
            {
                "date": "2026-03-08",
                "projected": True,
                "net_sales": {"low": 800.0, "mid": 1000.0, "high": 1200.0},
                "transactions": {"low": 90, "mid": 120, "high": 150},
                "avg_ticket": {"low": 8.0, "mid": 9.5, "high": 11.0},
                "expected_waste_usd": {"low": 0.0, "mid": 3.0, "high": 6.0},
                "expected_lost_sales_usd": {"low": 0.0, "mid": 10.0, "high": 30.0},
                "weather": None,
                "weather_note": "Sin pronóstico",
            }
        ],
    }
    out = _run(gt.get_outlook(_ctx(agent="glazed_present")))

    assert out["available"] is True
    day = out["days"][0]
    assert day["net_sales"]["mid"] == 1000.0
    assert day["expected_lost_sales_usd"]["high"] == 30.0
    assert day["expected_waste_usd"]["mid"] == 3.0
    assert day["weather_note"] == "Sin pronóstico"
    assert day["avg_ticket"]["mid"] == 9.5


def test_issue_analysis_keeps_recommendation_pros_cons_do_nothing_and_confidence(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {
        **_ENVELOPE,
        "issue_id": "ISS-1",
        "title": "Reorder donas",
        "tier": "N1",
        "deadline": "2026-03-08",
        "impact_usd": {"low": 10, "mid": 20, "high": 30},
        "confidence": {"score": 0.8, "calibrated": True},
        "problem": "stockout risk",
        "recommendation": {"option_id": "O1", "label": "Order 24", "action_type": "order"},
        "pros": ["avoids stockout"],
        "cons": ["min order of 20"],
        "requires_approval": False,
        "dialog": {
            "why": "best net",
            "if_accept": "order",
            "if_not_act": "lose sales",
            "alternatives": [{"option_id": "do_nothing", "label": "Do nothing", "net_usd_mid": 0}],
        },
    }
    out = _run(gt.get_issue_analysis(_ctx(), issue_id="ISS-1"))

    assert out["available"] is True
    assert out["recommendation"]["option_id"] == "O1"
    assert out["confidence"]["score"] == 0.8
    assert out["pros"] == ["avoids stockout"] and out["cons"] == ["min order of 20"]
    assert out["dialog"]["alternatives"][0]["option_id"] == "do_nothing"
    assert out["dialog"]["if_not_act"] == "lose sales"
    assert out["store_display_name"] == "Tienda Centro"
    assert "store_id" not in out and "source" not in out


_VIEWS: list[Callable[[Glazed, Any], Any]] = [
    lambda g, c: g.get_inventory_status(c),
    lambda g, c: g.get_expiring(c),
    lambda g, c: g.get_deliveries(c),
    lambda g, c: g.get_staffing(c),
    lambda g, c: g.get_promos(c),
    lambda g, c: g.get_outlook(c),
    lambda g, c: g.get_issue_analysis(c, issue_id="ISS-1"),
]


@pytest.mark.parametrize("view", _VIEWS)
@pytest.mark.parametrize("status", [404, 503])
def test_views_degrade_to_unavailable_on_404_and_503(
    gt: Glazed, backend: Backend, view: Any, status: int
) -> None:
    backend.status, backend.body = status, {"detail": "nope"}
    out = _run(view(gt, _ctx()))

    assert out["available"] is False
    assert "reason" in out and "never estimate" in out["reason"].lower()
    assert "error" not in out


@pytest.mark.parametrize("view", _VIEWS)
def test_views_degrade_to_unavailable_when_the_backend_is_unreachable(
    gt: Glazed, backend: Backend, view: Any
) -> None:
    backend.error = httpx.ConnectError("down")
    out = _run(view(gt, _ctx()))

    assert out["available"] is False
    assert "reason" in out


@pytest.mark.parametrize("view", _VIEWS)
def test_views_keep_other_backend_errors_as_errors(gt: Glazed, backend: Backend, view: Any) -> None:
    backend.status, backend.body = 400, {"detail": "days must be between 1 and 7"}
    out = _run(view(gt, _ctx()))

    assert out["status"] == 400
    assert "error" in out


@pytest.mark.parametrize("view", _VIEWS)
def test_views_with_an_unexpected_body_are_unavailable(
    gt: Glazed, backend: Backend, view: Any
) -> None:
    backend.body = ["not", "an", "object"]
    assert _run(view(gt, _ctx()))["available"] is False


def test_view_descriptions_tell_the_model_what_comes_back(gt: Glazed) -> None:
    """CHANGED: the description the model reads (the old test read the function docstring)."""
    for tool, terms in {
        "get_inventory_status": ["days_of_cover", "status", "next_delivery_date"],
        "get_expiring": ["waste_usd", "by_day", "low", "high"],
        "get_deliveries": ["supplier", "calendar"],
        "get_staffing": ["recommended_staff_hours", "daily"],
        "get_promos": ["audit_verdict", "evaluation"],
        "get_outlook": [
            "net_sales",
            "expected_waste_usd",
            "expected_lost_sales_usd",
            "weather_note",
        ],
        "get_issue_analysis": ["recommendation", "pros", "cons", "do_nothing", "confidence"],
    }.items():
        doc = gt.tool(tool).description or ""
        for term in terms:
            assert term in doc, f"{tool} description lacks {term}"


# --- G2b: error values instead of exceptions or masked refusals ---------------------------------


def test_a_forecast_called_without_its_sku_is_an_error_value_and_no_request(
    gt: Glazed, backend: Backend
) -> None:
    """NEW: `arguments["sku"]` raised KeyError into the turn when the model left the sku out."""
    out = _run(gt.forecast_demand(_ctx(), target_date=_TARGET))

    assert out == {"error": "sku is required"}
    assert backend.requests == []


def test_a_view_called_without_a_required_argument_says_so_instead_of_unavailable(
    gt: Glazed, backend: Backend
) -> None:
    """The bridge's refusal has no HTTP status, which the view outcome read as "backend down"."""
    out = _run(gt.get_issue_analysis(_ctx()))

    assert out == {"error": "issue_id is required"}
    assert backend.requests == []


@pytest.mark.parametrize("days", [5, "soon", {"a": 1}, True])
def test_a_delivery_calendar_whose_days_is_not_a_list_is_unavailable_not_a_crash(
    gt: Glazed, backend: Backend, days: Any
) -> None:
    backend.body = {"window_days": 7, "days": days}

    out = _run(gt.get_deliveries(_ctx(), days=7))

    assert out["available"] is False and "unexpected" in out["reason"]


def test_a_delivery_calendar_without_days_is_an_empty_calendar(
    gt: Glazed, backend: Backend
) -> None:
    backend.body = {"window_days": 7}

    assert _run(gt.get_deliveries(_ctx(), days=7))["days"] == []


# --- the model sees why a view or a forecast is unavailable (user decision 2026-10-14, D91) -----


@pytest.mark.parametrize("view", _VIEWS)
@pytest.mark.parametrize("status", [404, 503])
def test_an_unavailable_view_carries_the_backends_reason(
    gt: Glazed, backend: Backend, view: Any, status: int
) -> None:
    backend.status, backend.body = status, {"detail": "no closing data for 2026-03-09 yet"}
    out = _run(view(gt, _ctx()))

    assert out["available"] is False
    assert "no closing data for 2026-03-09 yet" in out["reason"]
    assert "never estimate" in out["reason"].lower()


@pytest.mark.parametrize("status", [404, 503])
def test_an_unavailable_forecast_carries_the_backends_reason(
    gt: Glazed, backend: Backend, status: int
) -> None:
    backend.status = status
    backend.headers = {"content-type": "application/problem+json"}
    backend.body = {"title": "Unavailable", "detail": "the demand model is retraining"}
    demand = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))
    flow = _run(gt.forecast_daily_flow(_ctx(), target_date=_TARGET))

    for out in (demand, flow):
        assert out["forecast_available"] is False
        assert "the demand model is retraining" in out["reason"]
        assert "never estimate" in out["reason"].lower()


def test_without_a_reason_from_the_backend_the_unavailable_text_is_unchanged(
    gt: Glazed, backend: Backend
) -> None:
    backend.status, backend.body = 503, {}
    view = _run(gt.get_staffing(_ctx()))
    demand = _run(gt.forecast_demand(_ctx(), sku="SKU-1", target_date=_TARGET))

    assert view["reason"] == (
        "The staffing recommendation is not available right now. "
        "Say the data is unavailable; never estimate it yourself."
    )
    assert demand["reason"] == (
        "The forecast service is not available right now. "
        "Say the forecast is unavailable; never estimate one yourself."
    )
