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
    * A turn that continues a conversation (the session already holds another turn, `has_history`)
      is NOT routed and its router is not asked (L18): a router reads the message alone, so a
      follow-up such as "and which of those is the most urgent?" lands on a wrong plan, and a
      plan's compose has no history. It runs the requested profile with reason `follow_up`
      (audited); a history check that fails does the same with the error class. Without
      `has_history` every turn is routed, as before.
    * The audit row is bounded: the label, model, intent, reason and fallback reason a router
      produced are cut to `MAX_AUDIT_TEXT` characters, whichever adapter answered. Its token usage
      is recorded as input and output tokens (L12).

THE FAST PATH (S10, `domain/fast_path.py`)
    `fast_path` is the deployment's switch. OFF (the default) is everything above, unchanged: the
    router gets the request it always got. SHADOW and ON ask the router the extra questions
    (`RouteRequest.fast_path`), with the recent conversation when the session has one
    (`recent_history`), so a follow-up IS sent to the router then; its verdict, the detected
    intents and every answer are audited on the route row. SHADOW then does exactly what OFF would
    have done (a follow-up is still not routed). ON applies the verdict: a `pass:*` runs the entry
    profile as the person (`pass:uncovered` included: the message asks for more than the detected
    intents, P13), a `composite` runs the plans of every detected intent and one compose
    (`RunIntent.run_composite`), and a `defer:*` runs the logic above. A composite is built only
    when the entry profile opted into intents and EVERY detected intent has a declared plan whose
    owner is an allowed, startable target the entry profile may ask (the S8 guard, per owner);
    otherwise it is `defer:<intent>:<reason>`. The composite turn stays the person's turn on the
    entry profile; each member records the hop its calls run delegated at. Like a route, it is
    applied only once its audit row is written.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace

from agent_core.domain.fast_path import (
    Assessment,
    CompositeCall,
    CompositeMember,
    FastPathMode,
    FastPathThresholds,
    FastPathVerdict,
    decide_fast_path,
)
from agent_core.domain.intents import IntentCall, IntentPlan
from agent_core.domain.policy import Effect
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import (
    FALLBACK_ASK_PEER_NOT_ALLOWED,
    FALLBACK_AUDIT_UNAVAILABLE,
    FALLBACK_DELEGATION_UNAVAILABLE,
    FALLBACK_FOLLOW_UP,
    FALLBACK_INTENT_NOT_SUPPORTED,
    FALLBACK_INTENT_UNKNOWN,
    FALLBACK_PEER_REFUSED,
    FALLBACK_TIMEOUT,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
    RoutingPolicy,
    error_reason,
    resolve_route,
)
from agent_core.domain.turn import (
    SessionRef,
    TurnId,
    TurnRequest,
    delegated_identity,
    restore_state,
)
from agent_core.ports.route_audit import RouteAudit
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.turn_router import TurnRouter

__all__ = [
    "DEFAULT_ROUTER_TIMEOUT_MS",
    "MAX_AUDIT_TEXT",
    "HasHistory",
    "PeerGate",
    "RecentHistory",
    "RouteTurn",
    "RoutedTurn",
]

# The tool a route stands in for (StartTurn's `ASK_PEER_TOOL`; an application module may not import
# another, so the name is repeated, and test_delegated_routing.py pins that they agree).
ASK_PEER_TOOL = "ask_peer"

PeerGate = Callable[[AgentProfile, AgentProfile, int], tuple[int | None, str | None]]
"""May the entry profile ask the target at this hop? `(next_hop, None)` when it may, `(None,
reason)` when the two-sided peer gate refuses. Composition binds it to the gate ask_peer uses."""

HasHistory = Callable[[SessionRef, TurnId], Awaitable[bool]]
"""Does the session already hold a turn other than this one? Composition binds it to the store's
own table, like `RunIntent`'s `RequestPersisted`: one read, not a port member."""

RecentHistory = Callable[[SessionRef, TurnId], Awaitable[tuple[str, ...]]]
"""S10. The session's recent conversation before this turn, oldest first, as short lines (empty:
none). What the fast path's follow-up question reads; composition binds it to the store."""

_LOG = logging.getLogger(__name__)

DEFAULT_ROUTER_TIMEOUT_MS = 1500
# The longest text field of a route's audit row (label, model, intent, fallback reason).
MAX_AUDIT_TEXT = 200
# S10. The longest fast path summary (every answer with its value) and intent list on that row.
MAX_ASSESSMENT_TEXT = 2000


@dataclass(frozen=True, slots=True)
class RoutedTurn:
    """The request the turn runs with, the consultation (None when no router applied) and, when an
    intent landed (D97), the intent whose plan the turn runs. A record written by an older build
    has no `intent` slot: read it with `getattr(routed, "intent", None)`."""

    request: TurnRequest
    record: RouteRecord | None = None
    intent: IntentCall | None = None
    # S10. The composite the turn answers (the fast path on); None otherwise.
    composite: CompositeCall | None = None

    def __setstate__(self, state: list[object]) -> None:
        """A record pickled by an older build reads back with the newer fields at their default."""
        restore_state(self, state)


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
        has_history: HasHistory | None = None,
        fast_path: FastPathMode = FastPathMode.OFF,
        thresholds: FastPathThresholds | None = None,
        recent_history: RecentHistory | None = None,
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
        # L18. None routes every turn (follow-ups included), as before.
        self._has_history = has_history
        # S10. The fast path switch, its thresholds and the conversation its follow-up question
        # reads (None: no excerpt; the follow-up check then uses `has_history`).
        self._fast_path = fast_path
        self._thresholds = thresholds or FastPathThresholds()
        self._recent_history = recent_history

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
        fast = self._fast_path is not FastPathMode.OFF

        started = self._clock()
        decision: RouteDecision | None = None
        failure: str | None = None
        history: tuple[str, ...] = ()

        async def consult() -> tuple[str | None, RouteDecision | None, tuple[str, ...]]:
            if not fast:
                follow_up = await self._follow_up(turn_id, request)
                if follow_up is not None:
                    return follow_up, None, ()
                return None, await self._routers.route(policy.router, _route_request(request)), ()
            # S10. The router is asked even about a follow-up: its answers say whether the message
            # depends on the conversation, which only it can tell.
            excerpt, follow_up = await self._conversation(turn_id, request)
            asked = _route_request(request, fast_path=True, history=excerpt)
            return follow_up, await self._routers.route(policy.router, asked), excerpt

        try:
            # ONE deadline for the follow-up check and the router (slice 39): a stuck history read
            # must not hold the turn any longer than a slow router may.
            failure, decision, history = await asyncio.wait_for(consult(), self._timeout)
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

        verdict: FastPathVerdict | None = None
        members: tuple[CompositeMember, ...] = ()
        if fast and decision is not None:
            continued = bool(history) or failure == FALLBACK_FOLLOW_UP
            verdict, members = await self._verdict(decision, policy, request, continued)
        # ON applies the verdict; a history read that failed (an error, not `follow_up`) is today's
        # path whatever the verdict, since nobody knows whether the message continues one.
        applied = (
            verdict
            if verdict is not None
            and self._fast_path is FastPathMode.ON
            and failure in (None, FALLBACK_FOLLOW_UP)
            else None
        )

        target, reason = request.profile_id, failure
        next_hop: int | None = None
        if applied is not None and applied.composite:
            reason = None
        elif applied is not None and applied.passes:
            pass  # the entry profile, as the person; `follow_up` stays recorded when it applied
        elif decision is not None and failure is None:
            target, reason = resolve_route(
                decision,
                policy,
                requested=request.profile_id,
                entry_points=self._entry_points(),
                plans=self._owners,
            )
        if reason is None and target != request.profile_id:
            next_hop, reason = await self._delegation(request, target)
            if reason is not None:
                target = request.profile_id
        composite = (
            CompositeCall(members) if applied is not None and applied.composite else None
        )
        delegated = next_hop is not None or (
            composite is not None and any(m.hop is not None for m in members)
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
            reason=None if decision is None else _bounded(decision.reason),
            input_tokens=None if decision is None else _tokens(decision.usage, _INPUT_KEYS),
            output_tokens=None if decision is None else _tokens(decision.usage, _OUTPUT_KEYS),
            actor=delegated_identity(request.caller).subject_id if delegated else None,
            on_behalf_of=request.caller.subject_id if delegated else None,
            **_fast_path_fields(self._fast_path, verdict, decision),
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
        if composite is not None:
            return RoutedTurn(request, record, None, composite)
        intent = None
        if (
            applied is None
            and decision is not None
            and decision.kind is RouteKind.INTENT
            and reason is None
        ):
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

    async def _conversation(
        self, turn_id: TurnId, request: TurnRequest
    ) -> tuple[tuple[str, ...], str | None]:
        """S10. The recent conversation and the follow-up fallback a turn takes without the fast
        path (`follow_up`, an error reason, or None). Without `recent_history` it is `has_history`
        alone, and no excerpt."""
        if self._recent_history is None:
            return (), await self._follow_up(turn_id, request)
        try:
            excerpt = tuple(await self._recent_history(request.session, turn_id))
        except Exception as error:
            _LOG.warning("could not read the recent conversation of turn %s", turn_id)
            return (), _bounded(error_reason(error))
        if not excerpt:
            return (), None
        # A deployment that routes follow-ups (`has_history` unset) keeps doing so.
        return excerpt, None if self._has_history is None else FALLBACK_FOLLOW_UP

    async def _verdict(
        self,
        decision: RouteDecision,
        policy: RoutingPolicy,
        request: TurnRequest,
        continued: bool,
    ) -> tuple[FastPathVerdict, tuple[CompositeMember, ...]]:
        """The fast path's verdict on `decision.assessment` and, for a composite, its members; a
        composite that cannot be built in full is `defer:<intent>:<reason>`."""
        assessment: Assessment | None = decision.assessment
        if assessment is None:
            return FastPathVerdict("defer:unassessed"), ()
        routed = (
            (decision.intent, decision.confidence)
            if decision.kind is RouteKind.INTENT
            and decision.intent
            and decision.confidence is not None
            else None
        )
        verdict = decide_fast_path(
            assessment, self._thresholds, has_history=continued, routed=routed
        )
        if not verdict.composite:
            return verdict, ()
        if not policy.intents or self._owners is None:
            return FastPathVerdict(f"defer:{FALLBACK_INTENT_NOT_SUPPORTED}", verdict.intents), ()
        members: list[CompositeMember] = []
        for intent in verdict.intents:
            owner = self._owners.get(intent)
            if owner is None:
                return FastPathVerdict(
                    _bounded(f"defer:{intent}:{FALLBACK_INTENT_UNKNOWN}") or "", verdict.intents
                ), ()
            if owner == request.profile_id:
                members.append(CompositeMember(intent, owner))
                continue
            landed, refusal = resolve_route(
                RouteDecision(kind=RouteKind.ROUTE, profile_id=owner),
                policy,
                requested=request.profile_id,
                entry_points=self._entry_points(),
            )
            hop: int | None = None
            if refusal is None and landed == owner:
                hop, refusal = await self._delegation(request, owner)
            if refusal is not None or hop is None:
                code = f"defer:{intent}:{refusal or 'refused'}"
                return FastPathVerdict(_bounded(code) or "", verdict.intents), ()
            members.append(CompositeMember(intent, owner, hop))
        return verdict, tuple(members)

    async def _follow_up(self, turn_id: TurnId, request: TurnRequest) -> str | None:
        """`follow_up` when the session already holds another turn, an error reason when that
        cannot be told (the normal path is always safe), else None (route it)."""
        if self._has_history is None:
            return None
        try:
            continued = await self._has_history(request.session, turn_id)
        except Exception as error:
            _LOG.warning("could not tell whether turn %s continues a conversation", turn_id)
            return _bounded(error_reason(error))
        return FALLBACK_FOLLOW_UP if continued else None

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


def _fast_path_fields(
    mode: FastPathMode, verdict: FastPathVerdict | None, decision: RouteDecision | None
) -> dict[str, str | None]:
    """The route row's fast path columns: nothing when it is off, else the mode, the verdict
    (None when the router gave no answer), the detected intents and every answer (bounded)."""
    if mode is FastPathMode.OFF:
        return {}
    assessment = None if decision is None else decision.assessment
    intents = None if verdict is None or not verdict.intents else ",".join(verdict.intents)
    return {
        "fast_path_mode": mode.value,
        "fast_path": None if verdict is None else _bounded(verdict.code),
        "fast_path_intents": None if intents is None else intents[:MAX_ASSESSMENT_TEXT],
        "assessment": None if assessment is None else assessment.summary()[:MAX_ASSESSMENT_TEXT],
    }


def _route_request(
    request: TurnRequest, *, fast_path: bool = False, history: tuple[str, ...] = ()
) -> RouteRequest:
    return RouteRequest(
        text=request.input.text,
        profile_id=request.profile_id,
        channel=request.caller.channel,
        tenant_id=str(request.caller.tenant_id),
        modality=request.caller.modality,
        fast_path=fast_path,
        history=history,
    )
