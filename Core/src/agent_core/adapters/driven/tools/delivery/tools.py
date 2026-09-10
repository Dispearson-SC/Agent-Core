"""Vertical: delivery optimizer tools.

Phase:   F4 - FIRST VERTICAL, and the contract test
Tasks:   docs/TASKS.md#t-f4-01
Status:  IMPLEMENTED - the four local tools, explicitly registered

WHY DELIVERY IS FIRST AND NOT FRAUD
    Short turns, bounded domain, simple approvals. It exercises the entire core without
    regulatory weight, and its domain complexity will not mask architectural errors.

    F4 exists to prove the port contract holds. It should not also be the hardest domain
    available. docs/DECISIONS.md#d11.

TOOLS - THIS MODULE, EXPLICITLY, AND NOTHING ELSE
    routing_estimate(order_id)                read-only
    pricing_quote(order_id)                   read-only
    pricing_apply(order_id, new_price)        MUTATING - approval above 15% change
    orders_lookup(order_id)                   read-only

    `request_evidence` is NOT here. It is the deferred-tool mechanism itself (D9), shared
    by every vertical, and lives in its own module once F7 builds it
    (docs/TASKS.md#t-f7-05) - see the note at that anchor for why parking it inside a
    vertical's package was the wrong call the first time. Same for `ask_peer` (F9,
    docs/TASKS.md#t-f9-04). A vertical's `tools.py` imports shared mechanisms; it does not
    grow its own copy.

NO AUTO-DISCOVERY (CLAUDE.md #8, docs/TASKS.md#t-f1-07)
    `build_toolset()` names its four functions in a literal list. It does not call
    `inspect.getmembers`, walk `globals()`/`vars(sys.modules[__name__])`, or use a
    decorator registry that scans this module's namespace at import time. Adding a fifth
    function to this file does not add a fifth tool - only editing the list below does.

    This is not a style preference. `ports/tool_provider.py` freezes the same rule for the
    provider that composes every vertical's package, and states the cost of getting it
    wrong: one `discover_builtin_tools()` call in Hermes AST-scanned `tools/*.py` and
    turned a handful of intended tools into an accidental ~396-module import closure. A
    vertical package that auto-discovered internally would reintroduce exactly that
    failure one level down, where the composite provider's own discipline could not see it.

THE IN-MEMORY CATALOG BELOW IS A STAND-IN, NAMED AS ONE
    There is no `OrderRepository` port - CLAUDE.md's contract for a vertical is one profile
    file plus one tools package, not a new port, and an order-tracking system is this
    vertical's own concern, not the core's. `_ORDERS` is a fixed, in-process fixture so the
    four tools are runnable and testable without inventing a fake external service. Wiring
    this vertical to a real orders backend is vertical-owned integration work, tracked by
    this package, not by `ports/`.

THE ACCEPTANCE CRITERION FOR THIS WHOLE PHASE
    The vertical works end to end AND the diff touches no file under domain/, application/
    or ports/. If it does, the port cut is wrong - stop and revisit it before continuing.
    CLAUDE.md.

    tests/unit/test_contract.py enforces this. Do not weaken that test to make a change
    pass; that is the one shortcut that quietly ends the architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic_ai.toolsets import FunctionToolset


class UnknownOrderError(ValueError):
    """`order_id` does not exist in the delivery catalog.

    Raised rather than returning a sentinel like `None`, so a bad id from the model
    surfaces as a tool-call error the agent can see and react to instead of silently
    quoting a routing estimate or price for an order that was never placed.
    """


@dataclass(frozen=True, slots=True)
class _Order:
    order_id: str
    zone: str
    distance_km: Decimal
    current_price_usd: Decimal
    status: str


# Fixture data for the first vertical - see the module docstring. Keyed by order_id so a
# lookup never has to scan.
_ORDERS: dict[str, _Order] = {
    "ord-1": _Order(
        order_id="ord-1",
        zone="downtown",
        distance_km=Decimal("3.2"),
        current_price_usd=Decimal("8.50"),
        status="in_transit",
    ),
    "ord-2": _Order(
        order_id="ord-2",
        zone="suburbs",
        distance_km=Decimal("11.7"),
        current_price_usd=Decimal("15.00"),
        status="pending_pickup",
    ),
}

# Minutes per kilometer, by zone - a placeholder routing model, not a traffic API. F6 adds
# a real maps/traffic MCP server per the profile's `mcp_servers` comment; until then this
# keeps `routing_estimate` honest about being an estimate rather than reaching for network
# I/O this file has no business doing.
_MINUTES_PER_KM_BY_ZONE: dict[str, Decimal] = {
    "downtown": Decimal("3"),
    "suburbs": Decimal("2"),
}
_DEFAULT_MINUTES_PER_KM = Decimal("2.5")


def _order(order_id: str) -> _Order:
    order = _ORDERS.get(order_id)
    if order is None:
        raise UnknownOrderError(f"No such order: {order_id!r}")
    return order


def orders_lookup(order_id: str) -> dict[str, str]:
    """Return the order's zone, distance, current price and status."""
    order = _order(order_id)
    return {
        "order_id": order.order_id,
        "zone": order.zone,
        "distance_km": str(order.distance_km),
        "current_price_usd": str(order.current_price_usd),
        "status": order.status,
    }


def routing_estimate(order_id: str) -> dict[str, str]:
    """Estimate delivery time in minutes for the order's zone and distance."""
    order = _order(order_id)
    minutes_per_km = _MINUTES_PER_KM_BY_ZONE.get(order.zone, _DEFAULT_MINUTES_PER_KM)
    estimated_minutes = order.distance_km * minutes_per_km
    return {
        "order_id": order.order_id,
        "zone": order.zone,
        "estimated_minutes": str(estimated_minutes),
    }


def pricing_quote(order_id: str) -> dict[str, str]:
    """Return the order's current price - read-only, no state change."""
    order = _order(order_id)
    return {
        "order_id": order.order_id,
        "current_price_usd": str(order.current_price_usd),
    }


def pricing_apply(order_id: str, new_price: str) -> dict[str, str]:
    """Set the order's price. MUTATING.

    `new_price` arrives as a string - the model's own tool-call argument - and is parsed
    with `Decimal`, never `float`, because this is currency (CLAUDE.md: silent-bug areas
    exist for exactly this class of mistake going unnoticed).

    The percentage-change gate itself is NOT this function's job. `AgentProfile
    .requires_approval_for()` (docs/TASKS.md#t-f4-02, `domain/profile.py`) evaluates the
    profile's `approval_rules` condition - `abs(pct_change) > 15` per
    `Core/profiles/delivery_optimizer.yaml` - and the policy/approval machinery decides
    whether this tool body ever runs at all. By the time `pricing_apply` executes, that
    decision is already made; duplicating it here would be a second, driftable copy of a
    rule this vertical's profile YAML already states once.
    """
    order = _order(order_id)
    try:
        parsed_price = Decimal(new_price)
    except InvalidOperation as exc:
        raise ValueError(f"new_price is not a valid decimal amount: {new_price!r}") from exc
    if parsed_price <= 0:
        raise ValueError(f"new_price must be positive, got {new_price!r}")

    previous_price = order.current_price_usd
    _ORDERS[order_id] = _Order(
        order_id=order.order_id,
        zone=order.zone,
        distance_km=order.distance_km,
        current_price_usd=parsed_price,
        status=order.status,
    )
    pct_change = (parsed_price - previous_price) / previous_price * Decimal("100")
    return {
        "order_id": order.order_id,
        "previous_price_usd": str(previous_price),
        "new_price_usd": str(parsed_price),
        "pct_change": str(pct_change),
    }


def build_toolset() -> FunctionToolset[None]:
    """The delivery vertical's toolset - exactly these four names, explicitly listed.

    Called fresh rather than exposed as a shared module-level singleton: `FunctionToolset`
    is cheap to construct and a fresh instance per call means nothing in this module holds
    mutable registration state a caller could accidentally share across profiles or turns.

    See the module docstring's "NO AUTO-DISCOVERY" section - this literal list IS the
    security property, not an implementation detail of it.
    """
    return FunctionToolset(
        [
            routing_estimate,
            pricing_quote,
            pricing_apply,
            orders_lookup,
        ]
    )
