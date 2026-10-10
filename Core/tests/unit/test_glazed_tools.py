"""Glazed vertical tools: bound to the turn, never to model-chosen store/case/as_of.

Subject: adapters/driven/tools/glazed/ and the eight glazed_<role> toolset packages.
No network: `httpx.MockTransport` stands in for the backend.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.glazed import client as glazed_client
from agent_core.adapters.driven.tools.glazed import tools as gt
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId

ROLES = {
    "orchestrator": {"propose_action"},
    "present": {"get_kpis", "get_day_summary", "get_issues", "explain_metric"},
    "past": {"get_history", "evaluate_promo", "recall_experiences"},
    "supply": {"get_order_plan", "project_inventory", "get_supplier_performance", "get_issues"},
    "strategist": {"get_kpis", "get_day_summary", "evaluate_promo"},
    "sentinel": {"get_history", "record_event"},
    "auditor": {"get_issues"},
    "liaison": {"offer_surplus", "request_stock"},
}
FORBIDDEN_PARAMS = {"store_id", "store", "as_of", "case", "case_id", "tenant", "tenant_id"}


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


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Backend:
    b = Backend()
    monkeypatch.setattr(glazed_client, "TRANSPORT", httpx.MockTransport(b))
    monkeypatch.setenv("GLAZED_BACKEND_URL", "http://backend.test:9")
    return b


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.mark.parametrize("role", sorted(ROLES))
def test_each_role_gets_exactly_its_tools_and_no_forbidden_parameter(role: str) -> None:
    toolset = gt.TOOLSETS[role]()
    assert set(toolset.tools) == ROLES[role]
    for tool in toolset.tools.values():
        props = set(tool.function_schema.json_schema.get("properties", {}))
        assert not props & FORBIDDEN_PARAMS, (tool.name, props)


def test_headers_carry_case_agent_and_store_from_the_turn(backend: Backend) -> None:
    _run(gt.get_kpis(_ctx(), "2026-03-01", "2026-03-07"))

    (req,) = backend.requests
    assert req.url.path == "/internal/v1/kpis"
    assert dict(req.url.params) == {"date_from": "2026-03-01", "date_to": "2026-03-07"}
    assert req.headers["X-Glazed-Case"] == "case-77"
    assert req.headers["X-Glazed-Agent"] == "glazed_supply"
    assert req.headers["X-Glazed-Store"] == "S030"
    assert str(req.url).startswith("http://backend.test:9/")


def test_a_peer_turn_resolves_the_asking_case(backend: Backend) -> None:
    _run(gt.get_order_plan(_ctx("peer~case-77~0f1e")))

    assert backend.requests[0].headers["X-Glazed-Case"] == "case-77"


def test_default_backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLAZED_BACKEND_URL", raising=False)
    assert glazed_client.backend_url() == "http://backend:8080"


@pytest.mark.parametrize(
    ("call", "path", "params"),
    [
        (
            lambda c: gt.get_day_summary(c, "2026-03-02"),
            "/internal/v1/day-summary",
            {"date": "2026-03-02"},
        ),
        (lambda c: gt.get_issues(c, 5), "/internal/v1/issues", {"limit": "5"}),
        (
            lambda c: gt.project_inventory(c, "SKU-1", 3),
            "/internal/v1/inventory/projection",
            {"sku": "SKU-1", "horizon": "3"},
        ),
        (lambda c: gt.get_order_plan(c), "/internal/v1/order-plan", {}),
        (lambda c: gt.get_supplier_performance(c), "/internal/v1/suppliers/performance", {}),
        (lambda c: gt.evaluate_promo(c, "P9"), "/internal/v1/promos/P9/evaluation", {}),
        (lambda c: gt.get_history(c), "/internal/v1/history", {}),
        (
            lambda c: gt.explain_metric(c, "waste_pct"),
            "/internal/v1/metrics/explain",
            {"metric": "waste_pct"},
        ),
    ],
)
def test_read_tools_hit_the_contract_endpoints(
    backend: Backend, call: Any, path: str, params: dict[str, str]
) -> None:
    _run(call(_ctx()))

    req = backend.requests[0]
    assert req.method == "GET"
    assert req.url.path == path
    assert dict(req.url.params) == params


def test_promo_id_cannot_escape_its_path_segment(backend: Backend) -> None:
    _run(gt.evaluate_promo(_ctx(), "../../x"))

    assert backend.requests[0].url.raw_path == b"/internal/v1/promos/..%2F..%2Fx/evaluation"


def test_recall_experiences_queries_memory_and_keeps_the_warning(backend: Backend) -> None:
    backend.body = []
    backend.headers = {"X-Glazed-Warning": "memory unavailable"}

    result = _run(gt.recall_experiences(_ctx(agent="glazed_past"), "combo PR004", 3))

    req = backend.requests[0]
    assert req.url.path == "/internal/v1/experiences"
    assert dict(req.url.params) == {"query": "combo PR004", "limit": "3"}
    assert result == {"experiences": [], "warning": "memory unavailable"}


def test_recall_experiences_without_warning_returns_the_entries(backend: Backend) -> None:
    entry = {"text": "combo failed", "decision_id": "D1", "kind": "promo", "created_at": "x"}
    backend.body = [entry]

    assert _run(gt.recall_experiences(_ctx(), "combo")) == {"experiences": [entry]}


def test_recall_experiences_limit_is_bounded(backend: Backend) -> None:
    _run(gt.recall_experiences(_ctx(), "x", 10_000))

    assert dict(backend.requests[0].url.params)["limit"] == "20"


def test_record_event_posts_json(backend: Backend) -> None:
    _run(gt.record_event(_ctx(), "holiday", "2026-03-10", "2026-03-12", "puente"))

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("POST", "/internal/v1/events")
    assert json.loads(req.content) == {
        "type": "holiday",
        "date_from": "2026-03-10",
        "date_to": "2026-03-12",
        "note": "puente",
    }


def test_propose_action_posts_a_proposal_and_never_executes(backend: Backend) -> None:
    _run(
        gt.propose_action(
            _ctx(agent="glazed_orchestrator"),
            issue_id="I1",
            option_id="O2",
            action_type="place_order",
            params={"sku": "SKU-1", "qty": 4},
            rationale="stockout in 2 days",
        )
    )

    req = backend.requests[0]
    assert (req.method, req.url.path) == ("POST", "/internal/v1/proposals")
    assert json.loads(req.content)["option_id"] == "O2"
    assert json.loads(req.content)["params"] == {"sku": "SKU-1", "qty": 4}
    assert len(backend.requests) == 1


def test_http_error_becomes_an_explainable_result(backend: Backend) -> None:
    backend.status, backend.body = 404, {"detail": "unknown case"}

    result = _run(gt.get_kpis(_ctx(), "2026-03-01", "2026-03-07"))

    assert result["error"] and result["status"] == 404
    assert "unknown case" in result["error"]


def test_connection_failure_becomes_an_error_result(backend: Backend) -> None:
    backend.error = httpx.ConnectError("refused")

    result = _run(gt.get_order_plan(_ctx()))

    assert "unreachable" in result["error"]


def test_timeout_becomes_an_error_result(backend: Backend) -> None:
    backend.error = httpx.ReadTimeout("slow")

    assert "timed out" in _run(gt.get_order_plan(_ctx()))["error"]


def test_no_turn_context_is_refused_without_a_request(backend: Backend) -> None:
    result = _run(gt.get_order_plan(SimpleNamespace(deps=None)))  # type: ignore[arg-type]

    assert "turn context" in result["error"]
    assert backend.requests == []


def test_liaison_placeholders_say_not_available(backend: Backend) -> None:
    assert "not available" in _run(gt.offer_surplus(_ctx(), "SKU-1"))["error"]
    assert "not available" in _run(gt.request_stock(_ctx(), "SKU-1"))["error"]
    assert backend.requests == []


def test_registered_in_the_composition_root() -> None:
    from agent_core.composition import TOOL_PACKAGES

    for role in ROLES:
        assert f"glazed_{role}" in TOOL_PACKAGES
        assert set(TOOL_PACKAGES[f"glazed_{role}"]().tools) == ROLES[role]
