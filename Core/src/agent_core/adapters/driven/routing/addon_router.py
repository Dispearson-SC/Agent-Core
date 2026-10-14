"""Driven adapter: a `TurnRouter` over the routers the enabled addons provide (D95).

Phase:   Phase 5 (fast communication), P1
Tests:   Core/tests/unit/test_router_addons.py
Implements: ports/turn_router.py

THE ADDON SIDE OF THE CONTRACT IS PLAIN DATA
    An addon never imports Core's domain, application or ports (A8, D82), so it cannot build a
    `RouteDecision`. Its `route` function reads a plain `dict`:

        {"text": str, "profile_id": str, "channel": str, "tenant_id": str, "modality": str|None}

    and answers a plain mapping:

        {"decision": "pass" | "route" | "intent",
         "profile_id": str,              # route: where to send the turn
         "intent": str, "arguments": {}, # intent: what to run
         "label": str, "confidence": float in [0, 1], "calibrated": bool, "model": str}

    Every key but `decision` is optional. This adapter is the one place that turns that mapping
    into a `RouteDecision`, strictly: an unknown key, a wrong type or an impossible value raises,
    and `RouteTurn` falls back to the normal path with the error class recorded. A sync `route`
    runs in a worker thread so a blocking HTTP client cannot stall the event loop (and the
    timeout still answers on time; the thread finishes on its own).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from typing import Any, Final

from agent_core.domain.routing import RouteDecision, RouteKind, RouteRequest

__all__ = ["AddonTurnRouter", "RouterFunction", "decision_from_mapping"]

RouterFunction = Callable[[dict[str, Any]], Any]
"""An addon's `route`, settings already bound: a plain request mapping in, a mapping out (or an
awaitable of one)."""

_KEYS: Final = frozenset(
    {"decision", "profile_id", "intent", "arguments", "label", "confidence", "calibrated", "model"}
)
_MAX_TEXT: Final = 200


class AddonTurnRouter:
    def __init__(self, routers: Mapping[str, RouterFunction]) -> None:
        self._routers = dict(routers)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._routers)

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        function = self._routers[router]
        plain = {
            "text": request.text,
            "profile_id": request.profile_id,
            "channel": request.channel,
            "tenant_id": request.tenant_id,
            "modality": request.modality,
        }
        if inspect.iscoroutinefunction(function):
            answer = await function(plain)
        else:
            answer = await asyncio.to_thread(function, plain)
            if inspect.isawaitable(answer):
                answer = await answer
        return decision_from_mapping(answer)


def _optional_text(answer: Mapping[str, Any], key: str) -> str | None:
    value = answer.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_TEXT:
        raise ValueError(f"router answer {key!r} must be a string of at most {_MAX_TEXT} chars")
    return value


def decision_from_mapping(answer: object) -> RouteDecision:
    """The `RouteDecision` a router's plain answer describes. Raises on anything malformed."""
    if not isinstance(answer, Mapping):
        raise TypeError(f"a router must answer a mapping, got {type(answer).__name__}")
    unknown = set(answer) - _KEYS
    if unknown:
        raise ValueError(f"router answer has unknown keys: {sorted(map(str, unknown))}")
    kind = RouteKind(str(answer.get("decision")))
    confidence = answer.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        raise ValueError("router answer 'confidence' must be a number")
    calibrated = answer.get("calibrated", False)
    if not isinstance(calibrated, bool):
        raise ValueError("router answer 'calibrated' must be true or false")
    arguments = answer.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ValueError("router answer 'arguments' must be a mapping")
    return RouteDecision(
        kind=kind,
        profile_id=_optional_text(answer, "profile_id"),
        intent=_optional_text(answer, "intent"),
        arguments=dict(arguments),
        label=_optional_text(answer, "label"),
        confidence=None if confidence is None else float(confidence),
        calibrated=calibrated,
        model=_optional_text(answer, "model"),
    )
