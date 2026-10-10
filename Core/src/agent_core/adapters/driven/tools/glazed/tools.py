"""Vertical: Glazed tools, explicitly listed, grouped by agent role.

NO AUTO-DISCOVERY: each role builder names its functions in a literal list (CLAUDE.md #8).
Every function takes `ctx: RunContext[Any]` first - the model never sees it - and none
exposes a store, case or as_of parameter. Dates the model passes (`date_from`, `date_to`,
`date`) are ranges inside the case's horizon; the backend clamps them to the case's as_of.

The numbers come from the backend (code computes); these functions only fetch and return.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.glazed.client import call

_API = "/internal/v1"
_MAX_LIMIT = 20
_NOT_AVAILABLE = {
    "error": "Inter-store network actions are not available yet. Tell the manager so; "
    "do not promise a transfer."
}

Ctx = RunContext[Any]


def _bounded(limit: int) -> int:
    try:
        return max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):  # a model-supplied junk limit must not raise
        return _MAX_LIMIT


async def get_kpis(ctx: Ctx, date_from: str, date_to: str) -> Any:
    """KPIs (sales, waste, stockout losses...) for the store between two ISO dates."""
    return await call(
        ctx, "GET", f"{_API}/kpis", params={"date_from": date_from, "date_to": date_to}
    )


async def get_day_summary(ctx: Ctx, date: str) -> Any:
    """Summary of one day (ISO date) for the store: sales, traffic, waste, incidents."""
    return await call(ctx, "GET", f"{_API}/day-summary", params={"date": date})


async def get_issues(ctx: Ctx, limit: int = 10) -> Any:
    """Active issues for the store that are relevant to your role, most urgent first."""
    return await call(ctx, "GET", f"{_API}/issues", params={"limit": _bounded(limit)})


async def explain_metric(ctx: Ctx, metric: str) -> Any:
    """How a metric is defined and computed (not its value). Use before explaining one."""
    return await call(ctx, "GET", f"{_API}/metrics/explain", params={"metric": metric})


async def get_order_plan(ctx: Ctx) -> Any:
    """The computed order plan (draft) for the store: SKUs, quantities, deadlines."""
    return await call(ctx, "GET", f"{_API}/order-plan")


async def project_inventory(ctx: Ctx, sku: str, horizon: int = 7) -> Any:
    """Projected stock of one SKU for the next `horizon` days, with the stockout date."""
    return await call(
        ctx,
        "GET",
        f"{_API}/inventory/projection",
        params={"sku": sku, "horizon": max(1, min(int(horizon), 30))},
    )


async def get_supplier_performance(ctx: Ctx) -> Any:
    """Supplier lead times and delays for the store."""
    return await call(ctx, "GET", f"{_API}/suppliers/performance")


async def get_history(ctx: Ctx) -> Any:
    """Past decisions, their results and recorded events for the store."""
    return await call(ctx, "GET", f"{_API}/history")


async def evaluate_promo(ctx: Ctx, promo_id: str) -> Any:
    """Computed evaluation (uplift, cost, verdict) of a promotion, past or proposed."""
    return await call(
        ctx, "GET", f"{_API}/promos/{quote(promo_id, safe='')}/evaluation"
    )


async def recall_experiences(ctx: Ctx, query: str, limit: int = 5) -> Any:
    """Recall past experiences of this store from long-term memory (Backboard).

    Returns entries with text, kind, created_at and optionally decision_id and score. They
    are leads, not facts: re-verify any number with get_history or evaluate_promo using the
    decision_id before citing it. A `warning` means memory is unavailable: use history only.
    """
    outcome = await call(
        ctx,
        "GET",
        f"{_API}/experiences",
        params={"query": query, "limit": _bounded(limit)},
        warning_header=True,
    )
    if isinstance(outcome, dict):  # an error result from the client
        return outcome
    data, warning = outcome
    result: dict[str, Any] = {"experiences": data}
    if warning:
        result["warning"] = warning
    return result


async def record_event(
    ctx: Ctx, type: str, date_from: str, date_to: str, note: str = ""
) -> Any:
    """Record an operational event (holiday, outage, promo...) over a date range."""
    return await call(
        ctx,
        "POST",
        f"{_API}/events",
        body={"type": type, "date_from": date_from, "date_to": date_to, "note": note},
    )


async def propose_action(
    ctx: Ctx,
    issue_id: str,
    option_id: str,
    action_type: str,
    params: dict[str, Any],
    rationale: str,
) -> Any:
    """Propose an action for an issue option. NOTHING is executed: the manager decides."""
    return await call(
        ctx,
        "POST",
        f"{_API}/proposals",
        body={
            "issue_id": issue_id,
            "option_id": option_id,
            "action_type": action_type,
            "params": params,
            "rationale": rationale,
        },
    )


async def offer_surplus(ctx: Ctx, sku: str) -> Any:
    """Offer surplus of a SKU to other stores. NOT AVAILABLE YET."""
    return dict(_NOT_AVAILABLE)


async def request_stock(ctx: Ctx, sku: str) -> Any:
    """Request stock of a SKU from other stores. NOT AVAILABLE YET."""
    return dict(_NOT_AVAILABLE)


def _toolset(*functions: Callable[..., Any]) -> FunctionToolset[Any]:
    return FunctionToolset(list(functions))


# One literal builder per role. The packages `glazed_<role>/` re-export these.
TOOLSETS: dict[str, Callable[[], FunctionToolset[Any]]] = {
    "orchestrator": lambda: _toolset(propose_action),
    "present": lambda: _toolset(get_kpis, get_day_summary, get_issues, explain_metric),
    "past": lambda: _toolset(get_history, evaluate_promo, recall_experiences),
    "supply": lambda: _toolset(
        get_order_plan, project_inventory, get_supplier_performance, get_issues
    ),
    "strategist": lambda: _toolset(get_kpis, get_day_summary, evaluate_promo),
    "sentinel": lambda: _toolset(get_history, record_event),
    "auditor": lambda: _toolset(get_issues),
    "liaison": lambda: _toolset(offer_surplus, request_stock),
}
