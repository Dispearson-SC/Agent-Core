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
    and `RouteTurn` falls back to the normal path with the error class recorded.

SYNC ROUTERS RUN ON THEIR OWN, CAPPED THREADS
    A sync `route` runs in a daemon thread so a blocking HTTP client cannot stall the event loop,
    and the caller's timeout still answers on time. A thread cannot be cancelled, so one that
    hangs keeps running after its caller gave up. At most `max_sync_calls` such threads live at
    once (counting the hung ones, until they finish); one more call is refused at once with
    `RouterSaturatedError` (a fallback to the normal path) instead of queueing behind them or
    taking a thread the rest of the process needs. Daemon threads never hold up shutdown.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable, Mapping
from typing import Any, Final

from agent_core.domain.routing import RouteDecision, RouteKind, RouteRequest

__all__ = [
    "DEFAULT_MAX_SYNC_CALLS",
    "AddonTurnRouter",
    "RouterFunction",
    "RouterSaturatedError",
    "decision_from_mapping",
]

RouterFunction = Callable[[dict[str, Any]], Any]
"""An addon's `route`, settings already bound: a plain request mapping in, a mapping out (or an
awaitable of one)."""

_KEYS: Final = frozenset(
    {"decision", "profile_id", "intent", "arguments", "label", "confidence", "calibrated", "model"}
)
_MAX_TEXT: Final = 200
DEFAULT_MAX_SYNC_CALLS: Final = 4


class RouterSaturatedError(RuntimeError):
    """Every sync-router thread is still busy: the call is refused instead of queued."""


class AddonTurnRouter:
    def __init__(
        self,
        routers: Mapping[str, RouterFunction],
        *,
        max_sync_calls: int = DEFAULT_MAX_SYNC_CALLS,
    ) -> None:
        if max_sync_calls < 1:
            raise ValueError(f"max_sync_calls must be at least 1, got {max_sync_calls}")
        self._routers = dict(routers)
        self._max_sync_calls = max_sync_calls
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def sync_calls_in_flight(self) -> int:
        """Sync router threads still running, including those whose caller gave up."""
        with self._lock:
            return self._in_flight

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
            answer = await self._in_thread(function, plain)
            if inspect.isawaitable(answer):
                answer = await answer
        return decision_from_mapping(answer)

    async def _in_thread(self, function: RouterFunction, plain: dict[str, Any]) -> Any:
        with self._lock:
            if self._in_flight >= self._max_sync_calls:
                raise RouterSaturatedError(
                    f"{self._in_flight} sync router calls are still running "
                    f"(max_sync_calls={self._max_sync_calls})"
                )
            self._in_flight += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def work() -> None:
            answer: Any = None
            failure: BaseException | None = None
            try:
                answer = function(plain)
            except BaseException as error:  # handed to the awaiting caller, never swallowed
                failure = error
            finally:
                with self._lock:
                    self._in_flight -= 1
            try:
                loop.call_soon_threadsafe(_settle, future, answer, failure)
            except RuntimeError:
                pass  # the loop closed while the router hung: nobody is waiting any more

        threading.Thread(target=work, name="turn-router", daemon=True).start()
        return await future


def _settle(future: asyncio.Future[Any], answer: Any, failure: BaseException | None) -> None:
    if future.done():  # the caller timed out or was cancelled
        return
    if failure is not None:
        future.set_exception(failure)
    else:
        future.set_result(answer)


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
