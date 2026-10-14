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
      turn then runs that profile's full policy, approvals and budgets.
    * A route that lands runs DELEGATED (S8): as the peer identity acting on behalf of the person
      (`domain/turn.py::delegated_identity`), one hop deeper, exactly as an ask_peer from the
      requested profile to the target would run. So it lands only where that ask could have been
      made: the two-sided peer gate (`peer_gate`, bound to `authorise_hop` by composition) allows
      the hop, and the PERSON's policy allows `ask_peer` with that target. Anything else falls back
      to the requested profile, as the person, with the reason recorded. Without a gate or a policy
      no route is applied (`delegation_unavailable`). The decision is part of the step result, so a
      recovered turn never re-evaluates it.
    * Never fails the turn. A router slower than `timeout_ms`, one that raises, a decision that
      cannot land and a route that cannot be audited all run the requested profile, with the
      reason recorded. A route is applied only once its audit row is written.
    * The audit row is bounded: the label, model, intent, reason and fallback reason a router
      produced are cut to `MAX_AUDIT_TEXT` characters, whichever adapter answered. Its token usage
      is recorded as input and output tokens (L12).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from agent_core.domain.intents import IntentCall, IntentPlan
from agent_core.domain.policy import Effect
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import (
    FALLBACK_ASK_PEER_NOT_ALLOWED,
    FALLBACK_AUDIT_UNAVAILABLE,
    FALLBACK_DELEGATION_UNAVAILABLE,
    FALLBACK_PEER_REFUSED,
    FALLBACK_TIMEOUT,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
    error_reason,
    resolve_route,
)
from agent_core.domain.turn import TurnId, TurnRequest, delegated_identity
from agent_core.ports.route_audit import RouteAudit
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.turn_router import TurnRouter

__all__ = ["DEFAULT_ROUTER_TIMEOUT_MS", "MAX_AUDIT_TEXT", "PeerGate", "RouteTurn", "RoutedTurn"]

# The tool a route stands in for (StartTurn's `ASK_PEER_TOOL`; an application module may not import
# another, so the name is repeated, and test_delegated_routing.py pins that they agree).
ASK_PEER_TOOL = "ask_peer"

PeerGate = Callable[[AgentProfile, AgentProfile, int], tuple[int | None, str | None]]
"""May the entry profile ask the target at this hop? `(next_hop, None)` when it may, `(None,
reason)` when the two-sided peer gate refuses. Composition binds it to the gate ask_peer uses."""

_LOG = logging.getLogger(__name__)

DEFAULT_ROUTER_TIMEOUT_MS = 1500
# The longest text field of a route's audit row (label, model, intent, fallback reason).
MAX_AUDIT_TEXT = 200


@dataclass(frozen=True, slots=True)
class RoutedTurn:
    """The request the turn runs with, the consultation (None when no router applied) and, when an
    intent landed (D97), the intent whose plan the turn runs. A record written by an older build
    has no `intent` slot: read it with `getattr(routed, "intent", None)`."""

    request: TurnRequest
    record: RouteRecord | None = None
    intent: IntentCall | None = None


class RouteTurn:
    def __init__(
        self,
        *,
        routers: TurnRouter,
        audit: RouteAudit,
        profiles: Mapping[str, AgentProfile],
        timeout_ms: int = DEFAULT_ROUTER_TIMEOUT_MS,
        clock: Callable[[], float] = time.monotonic,
        plans: Mapping[str, IntentPlan] | None = None,
        peer_gate: PeerGate | None = None,
        policy: ToolPolicy | None = None,
    ) -> None:
        if timeout_ms < 1:
            raise ValueError(f"the router timeout must be at least 1 ms, got {timeout_ms}")
        self._routers = routers
        self._audit = audit
        self._profiles = profiles
        self._timeout = timeout_ms / 1000
        self._clock = clock
        # D97. intent -> owning profile; None when no intent executor is wired (every intent then
        # falls back with `intent_not_supported`, as before P3).
        self._owners = (
            None if plans is None else {name: plan.profile_id for name, plan in plans.items()}
        )
        # S8. Both are needed to delegate; without either no route is applied.
        self._peer_gate = peer_gate
        self._policy = policy

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
                plans=self._owners,
            )
        next_hop: int | None = None
        if reason is None and target != request.profile_id:
            next_hop, reason = await self._delegation(request, target)
            if reason is not None:
                target = request.profile_id
        delegated = next_hop is not None
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
            reason=None if decision is None else _bounded(decision.reason),
            input_tokens=None if decision is None else _tokens(decision.usage, _INPUT_KEYS),
            output_tokens=None if decision is None else _tokens(decision.usage, _OUTPUT_KEYS),
            actor=delegated_identity(request.caller).subject_id if delegated else None,
            on_behalf_of=request.caller.subject_id if delegated else None,
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
                record,
                profile_id=request.profile_id,
                fallback_reason=FALLBACK_AUDIT_UNAVAILABLE,
                actor=None,
                on_behalf_of=None,
            )
            return RoutedTurn(request, unapplied)
        intent = None
        if decision is not None and decision.kind is RouteKind.INTENT and reason is None:
            intent = IntentCall(decision.intent or "", decision.arguments)
        if not record.routed or next_hop is None:
            return RoutedTurn(request, record, intent)
        delegated_request = replace(
            request,
            profile_id=target,
            caller=delegated_identity(request.caller),
            hop=next_hop,
        )
        return RoutedTurn(delegated_request, record, intent)

    async def _delegation(self, request: TurnRequest, target: str) -> tuple[int | None, str | None]:
        """`(next_hop, None)` when the requested profile could itself ask `target`, else `(None,
        reason)`. The checks an ask_peer to `target` would meet, in its order: the two-sided peer
        gate (switches, the target's mode, the hop limit, both allowlists), then the PERSON's policy
        for `ask_peer` with that target (a rule narrowed to another peer does not open this one).
        Only ALLOW lands: a route never asks a person to approve it and never runs a denied ask."""
        if self._peer_gate is None or self._policy is None:
            return None, FALLBACK_DELEGATION_UNAVAILABLE
        if request.caller.on_behalf_of is not None:
            return None, FALLBACK_DELEGATION_UNAVAILABLE  # delegation never nests
        entry = self._profiles[request.profile_id]
        callee = self._profiles[target]
        next_hop, refusal = self._peer_gate(entry, callee, request.hop)
        if refusal is not None or next_hop is None:
            return None, _bounded(f"{FALLBACK_PEER_REFUSED}:{refusal or 'refused'}")
        try:
            rules = await self._policy.load_rules(request.caller)
            verdict = self._policy.decide(rules, ASK_PEER_TOOL, {"target": target})
        except Exception as error:
            _LOG.warning("could not decide the delegated route's ask; not routing", exc_info=True)
            return None, _bounded(error_reason(error))
        if verdict.effect is not Effect.ALLOW:
            return None, f"{FALLBACK_ASK_PEER_NOT_ALLOWED}:{verdict.effect.value}"
        return next_hop, None

    def _entry_points(self) -> frozenset[str]:
        return frozenset(
            pid for pid, profile in self._profiles.items() if profile.mode.startable_by_channel
        )


# The names a router may give its token counts (its provider's words), in order of preference.
_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens")


def _tokens(usage: Mapping[str, int], keys: tuple[str, ...]) -> int | None:
    return next((usage[key] for key in keys if key in usage), None)


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
