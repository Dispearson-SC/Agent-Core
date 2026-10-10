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
    """KPIs for the store between two ISO dates, computed by the backend.

    Returns sales and traffic figures plus the two losses in USD: `waste_usd` and
    `lost_sales_usd`, the latter as {low, mid, high} (a range, never a point value; quote
    all three). `lost_sales_usd` may be absent; never estimate it yourself.
    Results carry `store_display_name`: use it, never the store code.
    """
    return await call(
        ctx, "GET", f"{_API}/kpis", params={"date_from": date_from, "date_to": date_to}
    )


async def get_day_summary(ctx: Ctx, date: str) -> Any:
    """Summary of one day (ISO date) for the store: sales, traffic, waste, incidents."""
    return await call(ctx, "GET", f"{_API}/day-summary", params={"date": date})


async def get_issues(ctx: Ctx, limit: int = 10) -> Any:
    """Active issues for the store relevant to your role, most urgent first.

    Each issue has: issue_id, kind, sku?, supplier_id?, store_display_name, as_of,
    severity_usd, `evidence` [{metric, value, unit, period, source}], `data_caveats` [str]
    and `options` (up to 4, always including `do_nothing` with its cost). Each option has:
    option_id, action_type, params, expected_benefit_usd {low, mid, high}, cost_usd,
    net_usd_mid, loss_impact {waste_usd_delta, lost_sales_usd_delta}, confidence {score
    0-1, n_backtest, method}, tier (N0-N3, computed by the backend), tier_reasons, urgency
    {deadline_date?, reason}, risk, if_act and if_not. Present only these options.
    Optional fields (sku, supplier_id, deadline_date, if_act, if_not) are absent when the
    backend has no value: a field may be absent; never estimate it yourself.
    An empty list means there are no issues.
    """
    return await call(ctx, "GET", f"{_API}/issues", params={"limit": _bounded(limit)})


async def explain_metric(ctx: Ctx, metric: str) -> Any:
    """How a metric is defined and computed (not its value). Use before explaining one."""
    return await call(ctx, "GET", f"{_API}/metrics/explain", params={"metric": metric})


async def get_order_plan(ctx: Ctx) -> Any:
    """The order plan (draft) computed by the backend, one line per SKU.

    Lines carry the quantity plus: service_level_target, order_deadline,
    projected_stockout_date, stockout_risk_without_order, vs_current_practice
    {received_to_sold, qty_delta} and min_order_warning.
    Any of these may be absent; never estimate it yourself.
    If one is absent, say it is not available. Results carry `store_display_name`.
    """
    return await call(ctx, "GET", f"{_API}/order-plan")


async def project_inventory(ctx: Ctx, sku: str, horizon: int = 7) -> Any:
    """Projected stock of one SKU for the next `horizon` days (1-30), from the backend.

    Includes the projected stockout date when there is one. Fields may be absent; never
    estimate it yourself.
    """
    return await call(
        ctx,
        "GET",
        f"{_API}/inventory/projection",
        params={"sku": sku, "horizon": max(1, min(int(horizon), 30))},
    )


async def get_supplier_performance(ctx: Ctx) -> Any:
    """Supplier lead times and delays for the store, as computed by the backend.

    Fields may be absent; never estimate it yourself.
    """
    return await call(ctx, "GET", f"{_API}/suppliers/performance")


async def get_history(ctx: Ctx) -> Any:
    """Past decisions and recorded events for the store, as the backend stores them.

    A decision's measured result is only present once the backend has measured it; it may
    be absent; never estimate it yourself, say the result is not available yet.
    """
    return await call(ctx, "GET", f"{_API}/history")


async def evaluate_promo(ctx: Ctx, promo_id: str) -> Any:
    """Backend-computed evaluation (uplift, cost, verdict) of a promotion.

    Fields may be absent; never estimate it yourself.
    """
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


async def propose_action(ctx: Ctx, issue_id: str, option_id: str, rationale: str) -> Any:
    """Propose one option of an issue. NOTHING is executed: the manager decides.

    Send only the `issue_id` and `option_id` returned by get_issues, and a short
    `rationale` in prose. The backend copies action_type, params and tier from the stored
    option; you cannot set or change them. Returns the pending proposal (or an error).
    """
    return await call(
        ctx,
        "POST",
        f"{_API}/proposals",
        body={
            "issue_id": issue_id,
            "option_id": option_id,
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
