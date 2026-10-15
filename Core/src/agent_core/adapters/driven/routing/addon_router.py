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
         "label": str, "confidence": float in [0, 1], "calibrated": bool, "model": str,
         # diagnostics: logged with the router's name, never part of the decision
         "reason": str, "elapsed_ms": number >= 0, "usage": {str: int}}

    Every key but `decision` is optional. With the fast path shadow or on (S10) the request also
    holds `"fast_path": true` and `"history": [str, ...]` (the recent conversation, oldest first;
    empty when there is none), and the answer may hold

         "assessment": {"intents": {<intent>: p in [0, 1]}, "action": p, "follow_up": p | null,
                        "complexity": score in [0, 1], "requests": int >= 1 | null,
                        "request_levels": [p of 1 request, p of 2, ...] | null}

    (`domain/fast_path.py::Assessment`). With the fast path off neither key is sent, so a router
    sees exactly the request it always saw. This adapter is the one place that turns that mapping
    into a `RouteDecision`, strictly: an unknown key, a wrong type or an impossible value raises,
    and `RouteTurn` falls back to the normal path with the error class recorded.

SYNC ROUTERS RUN ON THEIR OWN, CAPPED THREADS
    A sync `route` runs in a daemon thread so a blocking HTTP client cannot stall the event loop,
    and the caller's timeout still answers on time. A thread cannot be cancelled, so one that
    hangs keeps running after its caller gave up. At most `max_sync_calls` such threads live at
    once (counting the hung ones, until they finish). One more call waits for a slot, without a
    thread, for at most `slot_wait_seconds` (a burst of quick calls queues briefly instead of
    failing), then is refused with `RouterSaturatedError` (a fallback to the normal path) rather
    than taking a thread the rest of the process needs. The caller's own timeout cancels the wait
    too. The thread runs in a copy of the caller's context, so context variables (a trace id, a
    request scope) reach the router as they would reach an async one. Daemon threads never hold
    up shutdown.

DIAGNOSTICS ARE LOGGED, NOT DECIDED
    `reason`, `elapsed_ms` and `usage` say why and at what cost a router answered (an abstention
    below its threshold, a classifier's token usage). They are validated like the rest and logged
    at INFO with the router's name, label, confidence and model; the person's text never is.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any, Final

from agent_core.domain.fast_path import Assessment
from agent_core.domain.routing import RouteDecision, RouteKind, RouteRequest

__all__ = [
    "DEFAULT_MAX_SYNC_CALLS",
    "DEFAULT_SLOT_WAIT_SECONDS",
    "AddonTurnRouter",
    "RouterFunction",
    "RouterSaturatedError",
    "decision_from_mapping",
]

RouterFunction = Callable[[dict[str, Any]], Any]
"""An addon's `route`, settings already bound: a plain request mapping in, a mapping out (or an
awaitable of one)."""

_DIAGNOSTICS: Final = frozenset({"reason", "elapsed_ms", "usage"})
_KEYS: Final = _DIAGNOSTICS | {
    "decision", "profile_id", "intent", "arguments", "label", "confidence", "calibrated", "model",
    "assessment",
}  # fmt: skip
_ASSESSMENT_KEYS: Final = frozenset(
    {"intents", "action", "follow_up", "complexity", "requests", "request_levels"}
)
_MAX_TEXT: Final = 200
DEFAULT_MAX_SYNC_CALLS: Final = 4
DEFAULT_SLOT_WAIT_SECONDS: Final = 0.25
_LOG = logging.getLogger(__name__)


class RouterSaturatedError(RuntimeError):
    """Every sync-router thread is still busy: the call is refused instead of queued."""


class AddonTurnRouter:
    def __init__(
        self,
        routers: Mapping[str, RouterFunction],
        *,
        max_sync_calls: int = DEFAULT_MAX_SYNC_CALLS,
        slot_wait_seconds: float = DEFAULT_SLOT_WAIT_SECONDS,
    ) -> None:
        if max_sync_calls < 1:
            raise ValueError(f"max_sync_calls must be at least 1, got {max_sync_calls}")
        if slot_wait_seconds < 0:
            raise ValueError(f"slot_wait_seconds must not be negative, got {slot_wait_seconds}")
        self._routers = dict(routers)
        self._max_sync_calls = max_sync_calls
        self._slot_wait = slot_wait_seconds
        self._lock = threading.Lock()
        self._in_flight = 0
        # Callers waiting for a slot: woken (all of them) whenever a thread frees one.
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

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
        plain: dict[str, Any] = {
            "text": request.text,
            "profile_id": request.profile_id,
            "channel": request.channel,
            "tenant_id": request.tenant_id,
            "modality": request.modality,
        }
        if request.fast_path:  # S10: only then, so `off` hands a router what it always got
            plain["fast_path"] = True
            plain["history"] = list(request.history)
        if inspect.iscoroutinefunction(function):
            answer = await function(plain)
        else:
            answer = await self._in_thread(function, plain)
            if inspect.isawaitable(answer):
                answer = await answer
        decision = decision_from_mapping(answer)
        _log_diagnostics(router, answer)
        return decision

    async def _acquire_slot(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._slot_wait
        while True:
            with self._lock:
                if self._in_flight < self._max_sync_calls:
                    self._in_flight += 1
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RouterSaturatedError(
                        f"{self._in_flight} sync router calls are still running "
                        f"(max_sync_calls={self._max_sync_calls}, waited {self._slot_wait}s)"
                    )
                waiter: asyncio.Future[None] = loop.create_future()
                self._waiters.append((loop, waiter))
            try:
                await asyncio.wait_for(asyncio.shield(waiter), remaining)
            except TimeoutError:
                pass  # re-checked above: refused unless a slot freed in the meantime
            finally:
                with self._lock:
                    if (loop, waiter) in self._waiters:
                        self._waiters.remove((loop, waiter))

    def _release_slot(self) -> None:
        with self._lock:
            self._in_flight -= 1
            waiters, self._waiters = self._waiters, []
        for loop, waiter in waiters:
            try:
                loop.call_soon_threadsafe(_wake, waiter)
            except RuntimeError:
                pass  # that caller's loop closed: nobody is waiting there any more

    async def _in_thread(self, function: RouterFunction, plain: dict[str, Any]) -> Any:
        await self._acquire_slot()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        context = contextvars.copy_context()

        def work() -> None:
            answer: Any = None
            failure: BaseException | None = None
            try:
                answer = context.run(function, plain)
            except BaseException as error:  # handed to the awaiting caller, never swallowed
                failure = error
            finally:
                self._release_slot()
            try:
                loop.call_soon_threadsafe(_settle, future, answer, failure)
            except RuntimeError:
                pass  # the loop closed while the router hung: nobody is waiting any more

        try:
            threading.Thread(target=work, name="turn-router", daemon=True).start()
        except BaseException:
            self._release_slot()
            raise
        return await future


def _wake(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


def _log_diagnostics(router: str, answer: Mapping[str, Any]) -> None:
    """One INFO line per answer: what was decided and why, never the person's text."""
    _LOG.info(
        "turn router %r answered %s label=%s confidence=%s model=%s reason=%s elapsed_ms=%s "
        "usage=%s",
        router,
        answer.get("decision"),
        answer.get("label"),
        answer.get("confidence"),
        answer.get("model"),
        answer.get("reason"),
        answer.get("elapsed_ms"),
        dict(answer.get("usage") or {}),
    )


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


# The usage an answer may carry: what the audit row's INTEGER columns hold, and a map that does not
# grow with whatever the router said (slice 36). Past these the answer is malformed, like any other
# bad diagnostic, rather than an audit write that fails after the route was decided.
_MAX_USAGE_KEYS = 16
_MAX_USAGE_NAME = 64
_MAX_TOKEN_COUNT = 2**31 - 1


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
    _check_diagnostics(answer)
    usage = answer.get("usage") or {}
    return RouteDecision(
        kind=kind,
        profile_id=_optional_text(answer, "profile_id"),
        intent=_optional_text(answer, "intent"),
        arguments=dict(arguments),
        label=_optional_text(answer, "label"),
        confidence=None if confidence is None else float(confidence),
        calibrated=calibrated,
        model=_optional_text(answer, "model"),
        reason=_optional_text(answer, "reason"),
        usage={str(key): int(value) for key, value in usage.items()},
        assessment=_assessment(answer.get("assessment")),
    )


def _probability(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"router answer {where} must be a number")
    return float(raw)  # the range is `Assessment`'s to check


def _assessment(raw: object) -> Assessment | None:
    """S10. The fast path's answers, strictly; None when the router sent none."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("router answer 'assessment' must be a mapping")
    unknown = set(raw) - _ASSESSMENT_KEYS
    if unknown:
        names = sorted(map(str, unknown))
        raise ValueError(f"router answer 'assessment' has unknown keys: {names}")
    intents = raw.get("intents") or {}
    if not isinstance(intents, Mapping) or not all(isinstance(k, str) for k in intents):
        raise ValueError("router answer 'assessment.intents' must map intent names to numbers")
    optional = {
        name: None if raw.get(name) is None else _probability(raw[name], f"assessment.{name}")
        for name in ("action", "follow_up", "complexity")
    }
    requests = raw.get("requests")  # P13; Assessment refuses a count that is not an int >= 1
    levels = raw.get("request_levels")  # P14; Assessment checks the count and the range
    if levels is not None and not isinstance(levels, (list, tuple)):
        raise ValueError("router answer 'assessment.request_levels' must be a list of numbers")
    return Assessment(
        tuple((name, _probability(v, f"intent {name!r}")) for name, v in intents.items()),
        **optional,
        requests=requests,
        request_levels=None
        if levels is None
        else tuple(_probability(v, "assessment.request_levels") for v in levels),
    )


def _check_diagnostics(answer: Mapping[str, Any]) -> None:
    _optional_text(answer, "reason")
    elapsed = answer.get("elapsed_ms")
    if elapsed is not None and (
        isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0
    ):
        raise ValueError("router answer 'elapsed_ms' must be a number of at least 0")
    usage = answer.get("usage")
    if usage is None:
        return
    if (
        not isinstance(usage, Mapping)
        or len(usage) > _MAX_USAGE_KEYS
        or not all(
            isinstance(key, str)
            and len(key) <= _MAX_USAGE_NAME
            and isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= _MAX_TOKEN_COUNT
            for key, value in usage.items()
        )
    ):
        raise ValueError(
            f"router answer 'usage' must map at most {_MAX_USAGE_KEYS} short names to whole "
            f"numbers between 0 and {_MAX_TOKEN_COUNT}"
        )
