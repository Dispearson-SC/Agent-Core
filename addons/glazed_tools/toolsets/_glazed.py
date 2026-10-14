"""The glazed tools: generated from the backend's OpenAPI document, shaped where the old tools were.

Phase:   Phase 3 (white-label addons), G2
Tests:   Core/tests/unit/test_glazed_tools_addon.py, test_glazed_tools_contract.py

WHAT COMES FROM WHERE
    * Tool names, descriptions, argument names/types/defaults, idempotency, the warning header and
      replay come from the backend's own OpenAPI document (`x-agent-*` marks on its /internal/v1
      operations), patched by ../overlay.yaml (bounds the model gets, arguments it must not see, the
      second tool over /issues). The HTTP itself (identity headers, errors as values, row limit) is
      the bridge's.
    * This module holds only what OpenAPI cannot say: the forecast rules (future dates only, an
      unavailable forecast is said, never invented) and the manager views (a `{available, reason}`
      value and an allow-list of keys), which reproduce the hand-written tools they replace.

THE ROLES
    Each of the eight toolsets offers exactly the tools its agent role had. The model never sees a
    store, case or as_of argument: the overlay removes them and the turn's headers carry the case.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.http_bridge import build_bridge_toolset

__all__ = ["ROLES", "TRANSPORT", "build_role"]

# Replaced in tests with an `httpx.MockTransport`; None means the real network.
TRANSPORT: httpx.AsyncBaseTransport | None = None

OVERLAY = Path(__file__).resolve().parent.parent / "overlay.yaml"

# Every tool of the backend contract. `expect=` makes a backend that dropped or renamed one stop the
# startup instead of one conversation.
CONTRACT = (
    "propose_action",
    "classify_text",
    "get_briefing",
    "save_manager_note",
    "get_snapshot",
    "get_kpis",
    "get_day_summary",
    "get_issues",
    "explain_metric",
    "get_expiring",
    "get_outlook",
    "get_history",
    "evaluate_promo",
    "recall_experiences",
    "get_order_plan",
    "project_inventory",
    "get_supplier_performance",
    "forecast_demand",
    "get_inventory_status",
    "get_deliveries",
    "get_issue_analysis",
    "audit_promo",
    "forecast_daily_flow",
    "get_staffing",
    "get_promos",
    "record_event",
    "check_compliance",
    "get_integrity_issues",
    "get_transfer_options",
    "propose_transfer",
)

# role -> the tools of its toolset. Explicit and literal: no role gets a tool by omission.
ROLES: dict[str, tuple[str, ...]] = {
    # save_manager_note is a deliberate, narrow exception to "the manager turn holds no data
    # tool": it WRITES the manager's own words (backend code stores them) and reads nothing.
    "orchestrator": ("propose_action", "classify_text", "get_briefing", "save_manager_note"),
    "present": (
        "get_snapshot",
        "get_kpis",
        "get_day_summary",
        "get_issues",
        "explain_metric",
        "get_expiring",
        "get_outlook",
    ),
    "past": ("get_snapshot", "get_history", "evaluate_promo", "recall_experiences"),
    "supply": (
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
    ),
    "strategist": (
        "get_snapshot",
        "get_kpis",
        "get_day_summary",
        "evaluate_promo",
        "audit_promo",
        "forecast_daily_flow",
        "get_staffing",
        "get_promos",
        "get_outlook",
    ),
    "sentinel": ("get_history", "record_event", "check_compliance", "classify_text"),
    "auditor": ("get_integrity_issues",),
    "liaison": ("get_transfer_options", "propose_transfer"),
}


class _Refused(Exception):
    """A call the tool answers itself, with this value, without asking the backend."""

    def __init__(self, value: dict[str, Any]) -> None:
        super().__init__(value["error"])
        self.value = value


Arguments = dict[str, Any]
Before = Callable[[Arguments], Arguments]
After = Callable[[Arguments, Any], Any]


# --- forecasts ------------------------------------------------------------------------------------

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


def _target(arguments: Arguments) -> Arguments:
    """The `target_date` as an ISO date; a value that is not a date is answered, not sent."""
    value = arguments.get("target_date")
    try:
        day = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
    except ValueError:
        raise _Refused({"error": "target_date must be an ISO date (YYYY-MM-DD)."}) from None
    return {**arguments, "target_date": day.isoformat()}


def _forecast(arguments: Arguments, data: Any, subject: dict[str, Any]) -> Any:
    """Shared forecast outcome: future-only rule, unavailable handling. Returns the raw body."""
    target = dt.date.fromisoformat(arguments["target_date"])
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


def _demand(arguments: Arguments, reply: Any) -> Any:
    subject = {"sku": arguments.get("sku"), "target_date": arguments["target_date"]}
    data = _forecast(arguments, reply, subject)
    if "error" in data or data.get("forecast_available") is False:
        return data
    qty_range = data.get("qty_range")
    if not isinstance(qty_range, dict) or qty_range.get("p50") is None:
        return _unavailable(subject, "The backend returned no demand range.")
    result: dict[str, Any] = {**subject, "qty": qty_range["p50"], "qty_range": qty_range}
    result.update({k: data[k] for k in _DEMAND_KEYS if k in data})
    return result


def _flow(arguments: Arguments, reply: Any) -> Any:
    subject = {"target_date": arguments["target_date"]}
    data = _forecast(arguments, reply, subject)
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


# --- manager views --------------------------------------------------------------------------------
#
# Each tool calls the backend's internal twin of a public manager screen, so the chat and the web
# quote the same numbers. Replies are compacted with a whitelist of keys (no store codes, no
# internal sources) and every failure the backend can have becomes a structured
# `{"available": false, "reason": ...}` the persona can say out loud.

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


def _pick(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: row[k] for k in keys if k in row} if isinstance(row, dict) else {}


def _pick_all(rows: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_pick(r, keys) for r in rows] if isinstance(rows, list) else []


class _Unexpected(Exception):
    """A reply that is not the shape its view describes: said as unavailable, never guessed at."""


def _calendar(data: dict[str, Any]) -> dict[str, Any]:
    days = data.get("days")
    if days is not None and not isinstance(days, list):
        raise _Unexpected
    return {
        "window_days": data.get("window_days"),
        "days": [
            {
                "date": day.get("date"),
                "deliveries": _pick_all(day.get("deliveries"), _DELIVERY_KEYS),
            }
            for day in (days or [])
            if isinstance(day, dict)
        ],
    }


def _view_unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": f"{reason} Say the data is unavailable; never estimate it yourself.",
    }


def _view(what: str, shape: Callable[[dict[str, Any]], dict[str, Any]]) -> After:
    """One manager-view outcome: compacted data, or `available: false` when it cannot be had."""

    def outcome(arguments: Arguments, data: Any) -> Any:
        if isinstance(data, dict) and "error" in data:
            status = data.get("status")
            if status is None or status in (404, 503):
                return _view_unavailable(f"The {what} is not available right now.")
            return data
        if not isinstance(data, dict):
            return _view_unavailable(f"The backend returned an unexpected {what}.")
        try:
            shaped = shape(data)
        except _Unexpected:
            return _view_unavailable(f"The backend returned an unexpected {what}.")
        return {"available": True, **_pick(data, _ENVELOPE_KEYS), **shaped}

    return outcome


def _enums_are_respected(tool: Tool[Any]) -> Before:
    """Refuse an unknown enum value BEFORE the call, in the old tools' words.

    The view outcome turns every error value without a status into "unavailable" (the backend
    could not be reached), so a bad `sort` must be answered here and never reach it.
    """
    properties = tool.function_schema.json_schema.get("properties", {})
    enums = {n: p["enum"] for n, p in properties.items() if "enum" in p}

    def check(arguments: Arguments) -> Arguments:
        for name, allowed in enums.items():
            value = arguments.get(name)
            if value is not None and value not in allowed:
                raise _Refused({"error": f"{name} must be one of {', '.join(allowed)}."})
        return arguments

    return check


def _required_are_present(tool: Tool[Any]) -> Before:
    """Say a missing required argument BEFORE the call, in the bridge's words.

    The bridge answers it as an error value without an HTTP status, which the forecast and view
    outcomes read as "the backend is down"; the model must hear that IT left something out.
    """
    required = tool.function_schema.json_schema.get("required", [])

    def check(arguments: Arguments) -> Arguments:
        for name in required:
            if arguments.get(name) is None:
                raise _Refused({"error": f"{name} is required"})
        return arguments

    return check


def _discount_is_a_percentage(arguments: Arguments) -> Arguments:
    try:
        pct = float(arguments.get("discount_pct"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pct = float("nan")
    if not 0.0 <= pct <= 100.0:  # NaN fails the comparison too
        raise _Refused({"error": "discount_pct must be a number between 0 and 100."})
    return {**arguments, "discount_pct": pct}


_SHAPES: dict[str, After] = {
    "forecast_demand": _demand,
    "forecast_daily_flow": _flow,
    "get_inventory_status": _view(
        "inventory status",
        lambda d: {
            "coverage": d.get("coverage"),
            "sort": d.get("sort"),
            "items": _pick_all(d.get("items"), _INVENTORY_ITEM_KEYS),
        },
    ),
    "get_expiring": _view(
        "expiring list",
        lambda d: {
            "days": d.get("days"),
            "total_items": d.get("total_items"),
            "total_waste_usd": d.get("total_waste_usd"),
            "by_day": _pick_all(d.get("by_day"), ("date", "waste_usd")),
            "items": _pick_all(d.get("items"), _EXPIRING_ITEM_KEYS),
        },
    ),
    "get_deliveries": _view("delivery calendar", _calendar),
    "get_staffing": _view(
        "staffing recommendation",
        lambda d: {
            "benchmark": d.get("benchmark"),
            "note": d.get("note"),
            "planned_hours_note": d.get("planned_hours_note"),
            "days": _pick_all(d.get("days"), _STAFFING_DAY_KEYS),
        },
    ),
    "get_promos": _view(
        "promotions list",
        lambda d: {
            "window_days": d.get("window_days"),
            "items": _pick_all(d.get("items"), _PROMO_KEYS),
        },
    ),
    "get_outlook": _view(
        "outlook", lambda d: {"days": _pick_all(d.get("days"), _OUTLOOK_DAY_KEYS)}
    ),
    "get_issue_analysis": _view("issue analysis", lambda d: _pick(d, _ANALYSIS_KEYS)),
}


def _before(name: str, tool: Tool[Any]) -> Before | None:
    if name in ("forecast_demand", "forecast_daily_flow"):
        return _chain(_required_are_present(tool), _target)
    if name in ("get_inventory_status", "get_promos"):
        return _enums_are_respected(tool)
    if name == "get_issue_analysis":
        return _required_are_present(tool)
    if name == "audit_promo":
        return _discount_is_a_percentage
    return None


def _chain(*guards: Before) -> Before:
    def run(arguments: Arguments) -> Arguments:
        for guard in guards:
            arguments = guard(arguments)
        return arguments

    return run


def _shaped(name: str, tool: Tool[Any]) -> Tool[Any]:
    """The bridge's tool with this module's guard/outcome around it (same name, schema, replay)."""
    before, after = _before(name, tool), _SHAPES.get(name)
    if before is None and after is None:
        return tool
    inner: Callable[..., Awaitable[Any]] = tool.function  # type: ignore[assignment]

    async def run(ctx: Any, **raw: Any) -> Any:
        try:
            arguments = before(raw) if before is not None else raw
        except _Refused as refusal:
            return refusal.value
        data = await inner(ctx, **arguments)
        return after(arguments, data) if after is not None else data

    run.__name__ = name
    shaped: Tool[Any] = Tool.from_schema(
        run, name, tool.description, tool.function_schema.json_schema, takes_ctx=True
    )
    shaped.metadata = tool.metadata
    return shaped


def _source(settings: Mapping[str, Any]) -> str:
    url = str(settings.get("openapi_url") or "")
    return url or f"{str(settings['base_url']).rstrip('/')}/openapi.json"


def build_role(settings: Mapping[str, Any], role: str) -> FunctionToolset[Any]:
    """A fresh toolset for `role`: its tools, taken from the (once-fetched) contract."""
    contract = build_bridge_toolset(
        settings,
        openapi=_source(settings),
        overlay=OVERLAY,
        expect=CONTRACT,
        transport=TRANSPORT,
    )
    return FunctionToolset([_shaped(name, contract.tools[name]) for name in ROLES[role]])
