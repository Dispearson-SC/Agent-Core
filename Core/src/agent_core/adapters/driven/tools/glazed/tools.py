"""Vertical: Glazed tools, explicitly listed, grouped by agent role.

NO AUTO-DISCOVERY: each role builder names its functions in a literal list (CLAUDE.md #8).
Every function takes `ctx: RunContext[Any]` first - the model never sees it - and none
exposes a store, case or as_of parameter. Dates the model passes (`date_from`, `date_to`,
`date`) are ranges inside the case's horizon; the backend clamps them to the case's as_of.

The numbers come from the backend (code computes); these functions only fetch and return.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.glazed.client import call

_API = "/internal/v1"
_MAX_LIMIT = 20

Ctx = RunContext[Any]


_ISSUE_CATEGORIES = ("operational", "integrity", "all")


def _bounded(limit: int) -> int:
    try:
        return max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):  # a model-supplied junk limit must not raise
        return _MAX_LIMIT


def _clamped(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError, OverflowError):  # junk or NaN from the model must not raise
        return default


async def get_kpis(ctx: Ctx, date_from: str, date_to: str) -> Any:
    """KPIs for the store over a date window (`date_from` to `date_to`, ISO dates).

    One call covers the whole window: use it once instead of one call per day.

    Returns sales and traffic figures plus the two losses in USD: `waste_usd` and
    `lost_sales_usd`, the latter as {low, mid, high} (a range, never a point value; quote
    all three). `lost_sales_usd` may be absent; never estimate it yourself.
    Results carry `store_display_name`: use it, never the store code.
    """
    return await call(
        ctx, "GET", f"{_API}/kpis", params={"date_from": date_from, "date_to": date_to}
    )


async def get_snapshot(ctx: Ctx) -> Any:
    """Everything a specialist usually needs, in one call. Call it FIRST.

    Returns store_display_name, as_of, window {from, to}, `kpis` (net_sales, transactions,
    waste_usd, lost_sales_usd {low, mid, high}, tx_per_staff_hour), `top_issues` (up to 5
    Issues with options), `integrity` (up to 3 {kind, exposure_usd, summary}), `order_plan`
    {due_today, due_tomorrow, top_lines}, `suppliers` (on_time_pct, fill_pct,
    next_delivery), pending_proposals, recent_decisions and cache_age_s.
    A field may be absent; never estimate it yourself. Do not re-fetch this data.
    """
    return await call(ctx, "GET", f"{_API}/snapshot")


async def get_briefing(ctx: Ctx) -> Any:
    """The precomputed briefing of the case. Call it FIRST on every question.

    Returns `snapshot` (kpis, top_issues with options, integrity, order_plan, suppliers,
    pending_proposals, recent_decisions), `headline` (up to 5 facts computed by code, each
    with value, unit and source) and `suggested_focus` (issue_ids worth the manager's
    attention). Use the issue_id and option_id of `top_issues` to propose.
    A field may be absent; never estimate it yourself.
    """
    return await call(ctx, "GET", f"{_API}/briefing")


async def get_day_summary(ctx: Ctx, date: str) -> Any:
    """Summary of one day (ISO date) for the store: sales, traffic, waste, incidents."""
    return await call(ctx, "GET", f"{_API}/day-summary", params={"date": date})


async def get_issues(
    ctx: Ctx, limit: int = 10, category: str | None = None, kind: str | None = None
) -> Any:
    """Active issues for the store relevant to your role, most urgent first.

    `category` is "operational" (the backend default when omitted), "integrity"
    (data-quality issues, with `exposure_usd`) or "all"; `kind` narrows to one issue kind.

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
    if category is not None and category not in _ISSUE_CATEGORIES:
        return {"error": f"category must be one of {', '.join(_ISSUE_CATEGORIES)}"}
    params: dict[str, Any] = {"limit": _bounded(limit)}
    if category is not None:
        params["category"] = category
    if kind:
        params["kind"] = kind
    return await call(ctx, "GET", f"{_API}/issues", params=params)


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
        params={"sku": sku, "horizon": _clamped(horizon, 1, 30, 7)},
    )


_FUTURE_ONLY = {
    "error": "Forecasts are only available for future dates: the target date must be after "
    "the case's current date. Tell the manager so; do not estimate one."
}
_DEMAND_KEYS = (
    "quantiles",
    "censored_share",
    "weather_kind",
    "model",
    "model_version",
    "forecast_origin",
)
_FLOW_KEYS = ("avg_ticket", "granularity", "model", "forecast_origin")


def _unavailable(subject: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **subject,
        "forecast_available": False,
        "reason": f"{reason} Say the forecast is unavailable; never estimate one yourself.",
    }


def _after_origin(target: dt.date, origin: Any) -> bool:
    """True unless the backend's forecast origin proves the target is not in the future."""
    try:
        return target > dt.date.fromisoformat(str(origin)[:10])
    except ValueError:
        return True  # no usable origin: the backend already enforced the rule


async def _forecast(
    ctx: Ctx, path: str, target: dt.date, params: dict[str, Any], subject: dict[str, Any]
) -> Any:
    """Shared forecast call: future-only rule, unavailable handling. Returns the raw body."""
    data = await call(
        ctx, "GET", f"{_API}/forecast/{path}", params={**params, "target_date": target.isoformat()}
    )
    if not isinstance(data, dict):
        return _unavailable(subject, "The backend returned an unexpected forecast.")
    if "error" in data:
        status = data.get("status")
        if status in (400, 422):
            return dict(_FUTURE_ONLY)
        if status is None or status in (404, 503):
            return _unavailable(subject, "The forecast service is not available right now.")
        return data
    if not _after_origin(target, data.get("forecast_origin")):
        return dict(_FUTURE_ONLY)
    return data


async def forecast_demand(ctx: Ctx, sku: str, target_date: dt.date) -> Any:
    """Forecast demand of one SKU on a FUTURE date, from the backend's model.

    Use it for any question about future demand; never estimate a forecast yourself.
    `target_date` must be after the case's current date. Returns `qty` (the p50),
    `qty_range` {p10, p50, p90} (quote the range, never only the point), optional
    `quantiles`, `censored_share` (share of history with stockouts), `weather_kind`
    ("observed"), `model`, `model_version` and `forecast_origin`. If `forecast_available`
    is false the forecast is unavailable: say so.
    """
    subject = {"sku": sku, "target_date": target_date.isoformat()}
    data = await _forecast(ctx, "demand", target_date, {"sku": sku}, subject)
    if "error" in data or data.get("forecast_available") is False:
        return data
    qty_range = data.get("qty_range")
    if not isinstance(qty_range, dict) or qty_range.get("p50") is None:
        return _unavailable(subject, "The backend returned no demand range.")
    result: dict[str, Any] = {**subject, "qty": qty_range["p50"], "qty_range": qty_range}
    result.update({k: data[k] for k in _DEMAND_KEYS if k in data})
    return result


async def forecast_daily_flow(ctx: Ctx, target_date: dt.date) -> Any:
    """Forecast the store's transactions and average ticket on a FUTURE date (daily).

    Use it for any question about future traffic; never estimate a forecast yourself.
    `target_date` must be after the case's current date. Returns `transactions`
    {p10, p50, p90}, `footfall` (null when there is no data), `avg_ticket`
    {p10, p50, p90}, `granularity` ("daily"), `model` and `forecast_origin`. Quote the
    range and the model. If `forecast_available` is false the forecast is unavailable.
    """
    subject = {"target_date": target_date.isoformat()}
    data = await _forecast(ctx, "flow", target_date, {}, subject)
    if "error" in data or data.get("forecast_available") is False:
        return data
    if not isinstance(data.get("transactions"), dict):
        return _unavailable(subject, "The backend returned no transactions range.")
    result: dict[str, Any] = {
        **subject,
        "transactions": data["transactions"],
        "footfall": data.get("footfall"),
    }
    result.update({k: data[k] for k in _FLOW_KEYS if k in data})
    return result


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
    option; you cannot set or change them. Returns the pending proposal (or an error). The
    call carries an `Idempotency-Key` (case + issue + option), so repeating it after a
    timeout does not create a second proposal.
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
        idempotency_parts=(issue_id, option_id),
    )


_CLASSIFY_KINDS = ("rejection_reason", "deviation_cause")


async def check_compliance(ctx: Ctx, decision_id: str | None = None) -> Any:
    """Whether approved decisions were carried out, as measured by the backend.

    Without `decision_id` it covers all approved decisions of the case, otherwise only
    that one. Returns the checked decisions; each has `decision_id`, `execution_status`
    (executed, not_executed or partial) and, once the horizon passed, a `diagnosis`:
    `not_executed`, `within_range`, `external_event` (a registered event overlaps the
    horizon) or `forecast_error`, with the observed vs simulated figures (label
    `backtest`). Only order-quantity actions are measurable; a field may be absent; never
    estimate it yourself. An empty list means there is nothing to check.
    """
    return await call(
        ctx, "GET", f"{_API}/compliance", params={"decision_id": decision_id or None}
    )


async def classify_text(ctx: Ctx, kind: str, text: str) -> Any:
    """Classify free text with Jev into a fixed label set.

    `kind` is `rejection_reason` (why the manager rejected a proposal) or
    `deviation_cause` (why a decision was not carried out as planned). Returns
    {label, confidence, model, calibrated, adapter} plus `warning` when an earlier
    classifier failed. The label is `needs_review` (rejection_reason) or `no_match`
    (deviation_cause) when nothing fits or confidence is under 0.4. `confidence` only
    measures how well the text fits the label; it is not the confidence of a
    recommendation, and `calibrated` false means a fallback model answered. Quote the
    label as returned; never invent a category.
    """
    if kind not in _CLASSIFY_KINDS:
        return {"error": f"kind must be one of {', '.join(_CLASSIFY_KINDS)}."}
    return await call(ctx, "POST", f"{_API}/classify", body={"kind": kind, "text": text})


async def audit_promo(ctx: Ctx, department: str, discount_pct: float) -> Any:
    """Audit a promotion idea against past evidence BEFORE recommending it.

    Returns {verdict, recommendation, similar, alternative, reasons, noise_floor_pct}.
    `verdict` is `block` (do not recommend it), `allow` or `insufficient_evidence`.
    `similar` lists comparable past promos (each may carry this store's
    `store_evaluation`), `alternative` a better idea when there is one and `reasons` the
    evidence. On `block`, recommend `do_nothing`. A field may be absent; never estimate
    it yourself.
    """
    try:
        pct = float(discount_pct)
    except (TypeError, ValueError):
        pct = float("nan")
    if not 0.0 <= pct <= 100.0:  # NaN fails the comparison too
        return {"error": "discount_pct must be a number between 0 and 100."}
    return await call(
        ctx,
        "POST",
        f"{_API}/promos/audit",
        body={"department": department, "discount_pct": pct},
    )


async def get_integrity_issues(ctx: Ctx) -> Any:
    """Data-integrity issues of the store (SKU key mismatches, orphan SKUs...).

    Same Issue shape as get_issues (issue_id, kind, evidence, data_caveats, options)
    marked `category` integrity, `integrity` true, with `exposure_usd`: the sales amount
    at stake (their `severity_usd` is 0 on purpose). Options are a `data_fix_task`
    (never automatic) and `do_nothing`. A field may be absent; never estimate it
    yourself. An empty list means no integrity issue was found.
    """
    return await call(ctx, "GET", f"{_API}/issues", params={"category": "integrity"})


async def save_manager_note(
    ctx: Ctx,
    text: str,
    category: str = "eventos",
    date_from: str | None = None,
    date_to: str | None = None,
    impact: str | None = None,
) -> Any:
    """Save a note the manager asked to record (a closure, an event, supplier news...).

    Call it when the manager asks to note, remember or record something, or states a fact
    about future operations. `text` is the manager's own words; `category` defaults to
    `eventos`; `date_from` / `date_to` (ISO dates) bound the period it applies to;
    `impact` is a short free label when the manager gave one. Returns {saved, memory_id,
    event_id, category, date_from, date_to}. Only say it was saved when `saved` is true.
    A write of the manager's own words, done by backend code; it changes no store data.
    """
    body: dict[str, Any] = {"text": text, "category": category}
    for key, value in (("date_from", date_from), ("date_to", date_to), ("impact", impact)):
        if value is not None:
            body[key] = value
    return await call(ctx, "POST", f"{_API}/notes", body=body)


async def get_transfer_options(ctx: Ctx, sku: str | None = None, limit: int = 5) -> Any:
    """Transfer options between stores, computed by the backend (optionally for one `sku`).

    Returns `options`: {option_id, sku, product_name, from_store, to_store, to_store_name,
    qty, surplus_units, deficit_units, transfer_cost_usd, waste_avoided_usd,
    lost_sales_avoided_usd, net_usd, confidence, expires_on, rationale_es}, plus
    `do_nothing` and `assumptions` (the cost assumptions). Quote the confidence, the cost
    assumptions and the do-nothing comparison. A field may be absent; never estimate it.
    """
    return await call(
        ctx,
        "GET",
        f"{_API}/transfers/options",
        params={"sku": sku or None, "limit": _bounded(limit)},
    )


async def propose_transfer(ctx: Ctx, option_id: str, rationale: str) -> Any:
    """Propose one transfer option. NOTHING is executed: the manager approves or rejects.

    Send only an `option_id` returned by get_transfer_options and a short `rationale`. The
    backend copies the transfer from the stored option and creates a pending proposal. The
    call carries an `Idempotency-Key` (case + option), so a retry after a timeout does not
    create a second proposal.
    """
    return await call(
        ctx,
        "POST",
        f"{_API}/transfers/proposals",
        body={"option_id": option_id, "rationale": rationale},
        idempotency_parts=(option_id,),
    )


# ---- manager views (inventory, expiring, deliveries, staffing, promos, outlook, analysis) ----
#
# Each tool calls the backend's internal twin of a public manager screen, so the chat and the
# web quote the same numbers. The model never supplies the store or the date: the case in the
# headers fixes both. Replies are compacted with a whitelist of keys (no store codes, no
# internal sources) and every failure the backend can have becomes a structured
# `{"available": false, "reason": ...}` the persona can say out loud.

_INVENTORY_SORTS = ("days_of_cover", "turnover", "slow")
_PROMO_STATUSES = ("active", "upcoming", "past")
_ENVELOPE_KEYS = ("store_display_name", "as_of")
_INVENTORY_ITEM_KEYS = (
    "sku",
    "product_name",
    "department",
    "on_hand",
    "avg_daily_demand",
    "days_of_cover",
    "status",
    "status_reason",
    "closing_reason",
    "display_status_es",
    "next_delivery_date",
    "next_delivery_source",
    "scheduled_in_units",
    "forecast_outflow_units",
    "projected_stockout_date",
    "turnover_28d",
    "inventory_value_usd",
    "slow_mover",
)
_EXPIRING_ITEM_KEYS = (
    "sku",
    "product_name",
    "expiring_units",
    "expiry_date",
    "waste_units",
    "waste_usd",
    "shelf_life_days",
    "lot_age_inferred",
)
_DELIVERY_KEYS = (
    "supplier_id",
    "supplier_name",
    "po_id",
    "kind",
    "status",
    "value_usd",
    "expected_date",
    "received_date",
    "lines",
)
_STAFFING_DAY_KEYS = (
    "date",
    "forecast_transactions",
    "recommended_staff_hours",
    "planned_staff_hours",
    "gap_hours",
    "basis",
)
_PROMO_KEYS = (
    "promo_id",
    "name",
    "department",
    "category",
    "discount_pct",
    "start_date",
    "end_date",
    "status",
    "audit_verdict",
    "evaluation",
)
_OUTLOOK_DAY_KEYS = (
    "date",
    "net_sales",
    "transactions",
    "avg_ticket",
    "expected_waste_usd",
    "expected_lost_sales_usd",
    "weather",
    "weather_note",
)
_ANALYSIS_KEYS = (
    "issue_id",
    "title",
    "tier",
    "deadline",
    "impact_usd",
    "confidence",
    "problem",
    "recommendation",
    "pros",
    "cons",
    "requires_approval",
    "dialog",
)
_VIEW_LIST_LIMIT = 10


def _pick(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: row[k] for k in keys if k in row} if isinstance(row, dict) else {}


def _pick_all(rows: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_pick(r, keys) for r in rows] if isinstance(rows, list) else []


def _view_unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": f"{reason} Say the data is unavailable; never estimate it yourself.",
    }


async def _view(
    ctx: Ctx,
    path: str,
    params: dict[str, Any],
    shape: Callable[[dict[str, Any]], dict[str, Any]],
    what: str,
) -> Any:
    """One manager-view call: compacted data, or `available: false` when it cannot be had."""
    data = await call(ctx, "GET", f"{_API}/{path}", params=params)
    if isinstance(data, dict) and "error" in data:
        status = data.get("status")
        if status is None or status in (404, 503):
            return _view_unavailable(f"The {what} is not available right now.")
        return data
    if not isinstance(data, dict):
        return _view_unavailable(f"The backend returned an unexpected {what}.")
    return {"available": True, **_pick(data, _ENVELOPE_KEYS), **shape(data)}


async def get_inventory_status(ctx: Ctx, sort: str | None = None, limit: int = 15) -> Any:
    """Stock status per SKU: on hand, days_of_cover, status and the next delivery.

    `sort` is "days_of_cover" (emptiest shelf first, the default), "turnover" (fastest
    first) or "slow" (slow movers with the most money tied up first); `limit` is 1-50.
    Each item has `days_of_cover`, `status` (critical when the cover ends before the next
    delivery, watch or ok) with `status_reason`, `next_delivery_date`,
    `projected_stockout_date` and `forecast_outflow_units`. A field may be absent; never
    estimate it yourself. Use it for any question about what is running out or what is
    covered until the next delivery.
    """
    if sort is not None and sort not in _INVENTORY_SORTS:
        return {"error": f"sort must be one of {', '.join(_INVENTORY_SORTS)}."}
    params: dict[str, Any] = {"limit": _clamped(limit, 1, 50, 15)}
    if sort is not None:
        params["sort"] = sort
    return await _view(
        ctx,
        "inventory",
        params,
        lambda d: {
            "coverage": d.get("coverage"),
            "sort": d.get("sort"),
            "items": _pick_all(d.get("items"), _INVENTORY_ITEM_KEYS),
        },
        "inventory status",
    )


async def get_expiring(ctx: Ctx, days: int = 7) -> Any:
    """Items about to expire in the next `days` (1-30, default 7) and the waste they imply.

    Returns `items` (the largest lots first: sku, product_name, expiring_units,
    expiry_date, `waste_usd` as {low, mid, high} - a range, quote all three), `by_day`
    (the waste_usd range per expiry date), `total_items` and `total_waste_usd`. Lot ages
    are inferred, not measured. Use it for any question about expiry or waste to come.
    """
    return await _view(
        ctx,
        "expiring",
        {"days": _clamped(days, 1, 30, 7), "limit": _VIEW_LIST_LIMIT},
        lambda d: {
            "days": d.get("days"),
            "total_items": d.get("total_items"),
            "total_waste_usd": d.get("total_waste_usd"),
            "by_day": _pick_all(d.get("by_day"), ("date", "waste_usd")),
            "items": _pick_all(d.get("items"), _EXPIRING_ITEM_KEYS),
        },
        "expiring list",
    )


async def get_deliveries(ctx: Ctx, days: int = 7) -> Any:
    """The delivery calendar of the next `days` (1-14, default 7): which supplier comes when.

    Returns `days`, one entry per date with its `deliveries`: open purchase orders (kind
    open_po, with po_id, expected_date, lines and value_usd) and scheduled supplier slots
    (kind scheduled, no lines). Use it to answer when a supplier arrives; never guess a
    delivery date.
    """
    return await _view(
        ctx,
        "deliveries",
        {"days": _clamped(days, 1, 14, 7)},
        lambda d: {
            "window_days": d.get("window_days"),
            "days": [
                {
                    "date": day.get("date"),
                    "deliveries": _pick_all(day.get("deliveries"), _DELIVERY_KEYS),
                }
                for day in (d.get("days") or [])
                if isinstance(day, dict)
            ],
        },
        "delivery calendar",
    )


async def get_staffing(ctx: Ctx, days: int = 7) -> Any:
    """Recommended staff hours per future day (daily only; there is no hourly staffing).

    `days` is 1-14, default 7. Each day has `forecast_transactions` and
    `recommended_staff_hours` as {low, mid, high}; `planned_staff_hours` and `gap_hours`
    are null because there is no roster in the data. `benchmark` gives the format's
    transactions per staff hour. Say plainly that the data is daily when asked about hours.
    """
    return await _view(
        ctx,
        "staffing",
        {"days": _clamped(days, 1, 14, 7)},
        lambda d: {
            "benchmark": d.get("benchmark"),
            "note": d.get("note"),
            "planned_hours_note": d.get("planned_hours_note"),
            "days": _pick_all(d.get("days"), _STAFFING_DAY_KEYS),
        },
        "staffing recommendation",
    )


async def get_promos(ctx: Ctx, status: str | None = None, days: int = 14) -> Any:
    """Promotions with their audit verdict and evaluation.

    `status` is "active", "upcoming" or "past" (all when omitted); `days` (1-365, default
    14) is the window around the case date. Each item has `audit_verdict` (block, allow or
    insufficient_evidence) and, only for promos that already ended, an `evaluation`
    (uplift_pct, expected_uplift_pct, confidence). A promo without evaluation has no result
    yet: never claim an uplift.
    """
    if status is not None and status not in _PROMO_STATUSES:
        return {"error": f"status must be one of {', '.join(_PROMO_STATUSES)}."}
    params: dict[str, Any] = {"days": _clamped(days, 1, 365, 14), "limit": _VIEW_LIST_LIMIT}
    if status is not None:
        params["status"] = status
    return await _view(
        ctx,
        "promos",
        params,
        lambda d: {
            "window_days": d.get("window_days"),
            "items": _pick_all(d.get("items"), _PROMO_KEYS),
        },
        "promotions list",
    )


async def get_outlook(ctx: Ctx, days: int = 2) -> Any:
    """Outlook for the next `days` (1-7, default 2: tomorrow and the day after).

    Each day has `net_sales`, `transactions` and `avg_ticket` as {low, mid, high},
    `expected_waste_usd` and `expected_lost_sales_usd` as {low, mid, high} ranges (quote
    all three), `weather` (null without a feed) and `weather_note`. Every day is a
    projection. Use it for any question about today, tomorrow or the next days.
    """
    return await _view(
        ctx,
        "outlook",
        {"days": _clamped(days, 1, 7, 2)},
        lambda d: {"days": _pick_all(d.get("days"), _OUTLOOK_DAY_KEYS)},
        "outlook",
    )


async def get_issue_analysis(ctx: Ctx, issue_id: str) -> Any:
    """The deterministic analysis of one issue (use an issue_id from get_issues).

    Returns the `recommendation` (option_id, label), its `pros` and `cons`, `confidence`
    {score, calibrated}, `tier`, `deadline`, `impact_usd` {low, mid, high}, `problem` and
    `dialog`: `why`, `if_accept`, `if_not_act` (the do_nothing case) and `alternatives`
    with their net_usd_mid. Always give the confidence and the do_nothing comparison with
    a recommendation.
    """
    return await _view(
        ctx,
        f"issues/{quote(str(issue_id), safe='')}/analysis",
        {},
        lambda d: _pick(d, _ANALYSIS_KEYS),
        "issue analysis",
    )


def _toolset(*functions: Callable[..., Any]) -> FunctionToolset[Any]:
    return FunctionToolset(list(functions))


# One literal builder per role. The packages `glazed_<role>/` re-export these.
TOOLSETS: dict[str, Callable[[], FunctionToolset[Any]]] = {
    # save_manager_note is a deliberate, narrow exception to "the manager turn holds no data
    # tool": it WRITES the manager's own words (backend code stores them) and reads nothing.
    "orchestrator": lambda: _toolset(
        propose_action, classify_text, get_briefing, save_manager_note
    ),
    "present": lambda: _toolset(
        get_snapshot,
        get_kpis,
        get_day_summary,
        get_issues,
        explain_metric,
        get_expiring,
        get_outlook,
    ),
    "past": lambda: _toolset(get_snapshot, get_history, evaluate_promo, recall_experiences),
    "supply": lambda: _toolset(
        get_snapshot,
        get_order_plan,
        project_inventory,
        get_supplier_performance,
        get_issues,
        forecast_demand,
        get_inventory_status,
        get_expiring,
        get_deliveries,
        get_issue_analysis,
    ),
    "strategist": lambda: _toolset(
        get_snapshot,
        get_kpis,
        get_day_summary,
        evaluate_promo,
        audit_promo,
        forecast_daily_flow,
        get_staffing,
        get_promos,
        get_outlook,
    ),
    "sentinel": lambda: _toolset(get_history, record_event, check_compliance, classify_text),
    "auditor": lambda: _toolset(get_integrity_issues),
    "liaison": lambda: _toolset(get_transfer_options, propose_transfer),
}
