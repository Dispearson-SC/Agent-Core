"""Use case: RouteTurn - consult a profile's router before the model runs (D95).

Phase:   Phase 5 (fast communication), P1
Tests:   Core/tests/unit/test_turn_routing.py

LAYER RULE
    Imports domain/ and ports/ only (and the standard library). Testable with fakes.

WHO CALLS IT
    `adapters/driving/workflow/turn_workflow.py::_step_route`, a `@DBOS.step()` that runs before
    the turn starts. The step's RESULT is the routed request and its record, so a replayed or
    resumed workflow reads the decision back from the operation log and never classifies again:
    the route a turn took is a recorded fact, not a recomputation.

WHAT IT GUARANTEES
    * Only a turn a person or a channel started (`request.owner is None`) of a profile that
      declares `routing:` is ever routed (`applies`). Everything else is returned unchanged
      without a router call or an audit row.
    * A route lands only on an allowed target (`domain/routing.py::resolve_route`). The routed
      turn then runs that profile's full policy, approvals and budgets: this changes WHICH
      profile `StartTurn` resolves, nothing else.
    * Never fails the turn. A router slower than `timeout_ms`, one that raises, a decision that
      cannot land and a route that cannot be audited all run the requested profile, with the
      reason recorded. A route is applied only once its audit row is written.
    * The audit row is bounded: the label, model, intent and fallback reason a router produced
      are cut to `MAX_AUDIT_TEXT` characters, whichever adapter answered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import (
    FALLBACK_AUDIT_UNAVAILABLE,
    FALLBACK_TIMEOUT,
    RouteDecision,
    RouteRecord,
    RouteRequest,
    error_reason,
    resolve_route,
)
from agent_core.domain.turn import TurnId, TurnRequest
from agent_core.ports.route_audit import RouteAudit
from agent_core.ports.turn_router import TurnRouter

__all__ = ["DEFAULT_ROUTER_TIMEOUT_MS", "MAX_AUDIT_TEXT", "RouteTurn", "RoutedTurn"]

_LOG = logging.getLogger(__name__)

DEFAULT_ROUTER_TIMEOUT_MS = 1500
# The longest text field of a route's audit row (label, model, intent, fallback reason).
MAX_AUDIT_TEXT = 200


@dataclass(frozen=True, slots=True)
class RoutedTurn:
    """The request the turn runs with, and the consultation (None when no router applied)."""

    request: TurnRequest
    record: RouteRecord | None = None


class RouteTurn:
    def __init__(
        self,
        *,
        routers: TurnRouter,
        audit: RouteAudit,
        profiles: Mapping[str, AgentProfile],
        timeout_ms: int = DEFAULT_ROUTER_TIMEOUT_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_ms < 1:
            raise ValueError(f"the router timeout must be at least 1 ms, got {timeout_ms}")
        self._routers = routers
        self._audit = audit
        self._profiles = profiles
        self._timeout = timeout_ms / 1000
        self._clock = clock

    def applies(self, request: TurnRequest) -> bool:
        """Whether `request` consults a router: a channel start of a profile declaring routing."""
        if request.owner is not None:
            return False
        profile = self._profiles.get(request.profile_id)
        return profile is not None and profile.routing is not None

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> RoutedTurn:
        if not self.applies(request):
            return RoutedTurn(request)
        profile = self._profiles[request.profile_id]
        policy = profile.routing
        assert policy is not None  # `applies` checked it

        started = self._clock()
        decision: RouteDecision | None = None
        failure: str | None = None
        try:
            decision = await asyncio.wait_for(
                self._routers.route(policy.router, _route_request(request)), self._timeout
            )
        except TimeoutError:
            failure = FALLBACK_TIMEOUT
        except Exception as error:
            failure = _bounded(error_reason(error))
            _LOG.warning(
                "router %r failed for turn %s; running the requested profile",
                policy.router,
                turn_id,
                exc_info=True,
            )
        elapsed_ms = max(0, round((self._clock() - started) * 1000))

        target, reason = request.profile_id, failure
        if decision is not None:
            target, reason = resolve_route(
                decision,
                policy,
                requested=request.profile_id,
                entry_points=self._entry_points(),
            )
        record = RouteRecord(
            router=policy.router,
            requested_profile=request.profile_id,
            profile_id=target,
            decision=None if decision is None else decision.kind,
            elapsed_ms=elapsed_ms,
            label=None if decision is None else _bounded(decision.label),
            confidence=None if decision is None else decision.confidence,
            calibrated=False if decision is None else decision.calibrated,
            model=None if decision is None else _bounded(decision.model),
            intent=None if decision is None else _bounded(decision.intent),
            fallback_reason=reason,
        )
        try:
            await self._audit.record_route(turn_id, str(request.session.tenant_id), record)
        except Exception:
            _LOG.warning(
                "could not audit the route of turn %s; running the requested profile",
                turn_id,
                exc_info=True,
            )
            unapplied = replace(
                record, profile_id=request.profile_id, fallback_reason=FALLBACK_AUDIT_UNAVAILABLE
            )
            return RoutedTurn(request, unapplied)
        if not record.routed:
            return RoutedTurn(request, record)
        return RoutedTurn(replace(request, profile_id=target), record)

    def _entry_points(self) -> frozenset[str]:
        return frozenset(
            pid for pid, profile in self._profiles.items() if profile.mode.startable_by_channel
        )


def _bounded(text: str | None) -> str | None:
    """`text` cut to `MAX_AUDIT_TEXT`: any `TurnRouter` adapter may answer, and an audit row must
    not grow with whatever it said."""
    return None if text is None else text[:MAX_AUDIT_TEXT]


def _route_request(request: TurnRequest) -> RouteRequest:
    return RouteRequest(
        text=request.input.text,
        profile_id=request.profile_id,
        channel=request.caller.channel,
        tenant_id=str(request.caller.tenant_id),
        modality=request.caller.modality,
    )
