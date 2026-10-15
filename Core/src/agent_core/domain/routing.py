"""Turn routing: a fast router consulted before the model when a turn starts (D95). Pure.

A profile that declares `routing:` names ONE router (an addon's `provides.routers` entry) and the
profiles that router may send its turns to. When a person or a channel starts a turn of that
profile, the router reads the text and answers one of three things:

  * `pass`                 run the turn as requested (the normal path),
  * `route(profile_id)`    run it as another profile instead, and
  * `intent(name, args)`   a typed intent a code executor runs (D97, `domain/intents.py`): the
                           turn runs under the plan's owning profile, which must be an allowed
                           target like a route's. A profile that did not opt in
                           (`routing.intents`) treats it as a `pass` with the reason recorded
                           (`FALLBACK_INTENT_NOT_SUPPORTED`); an intent nobody declared a plan
                           for is `FALLBACK_INTENT_UNKNOWN`.

WHAT A ROUTER IS NOT
    It is not a safety barrier. A route only chooses WHICH profile runs the turn; the turn then
    runs that profile's full policy, approvals and budgets, exactly as if the person had asked
    for it. So a route may land only on a profile the declaring profile lists in
    `routing.targets` that is loaded and is an entry point (`resolve_route`); anything else falls
    back to the requested profile with the reason recorded. A router never widens what a turn
    may do, and a misrouted turn is a wrong answer, never an escalation.

WHO A ROUTED TURN RUNS AS (S8)
    A route that lands runs its target the way an ask_peer from the requested profile to that
    target runs: under the peer identity (`domain/turn.py::delegated_identity`), acting on behalf
    of the person, one hop deep. So policy grants it what a peer turn of the target holds and
    nothing the person holds, and the person's own identity keeps its rules. It lands only where
    that ask could have been made: the two-sided peer gate allows the hop and the person's policy
    allows `ask_peer` to the target (`application/route_turn.py`). Otherwise it falls back with
    `peer_refused:<reason>`, `ask_peer_not_allowed:<effect>` or `delegation_unavailable`. A route
    can never grant more than the requested profile's own ask_peer could.

WHAT IS RECORDED
    `RouteRecord` is the audit row and the step result: what the router answered (or that it
    answered nothing), its label, confidence, whether that confidence is calibrated, the model
    that produced it, how long it took, the profile the turn runs under and why it fell back.
    It holds no model text and no argument values: a label is a class name, a fallback reason is
    a fixed code or `error:<ExceptionClass>`. The router's own `reason` (why it passed: `escape`,
    `below_threshold`, `http_429`...) and its token usage are recorded too (L12), so a router that
    abstains or costs is visible in the trail and not only in a log line.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from agent_core.domain.fast_path import COMPOSITE, Assessment, FastPathMode
from agent_core.domain.turn import restore_state

__all__ = [
    "FALLBACK_ASK_PEER_NOT_ALLOWED",
    "FALLBACK_AUDIT_UNAVAILABLE",
    "FALLBACK_DELEGATION_UNAVAILABLE",
    "FALLBACK_FOLLOW_UP",
    "FALLBACK_INTENT_NOT_SUPPORTED",
    "FALLBACK_INTENT_UNKNOWN",
    "FALLBACK_PEER_REFUSED",
    "FALLBACK_TARGET_NOT_ALLOWED",
    "FALLBACK_TARGET_NOT_STARTABLE",
    "FALLBACK_TIMEOUT",
    "RouteDecision",
    "RouteKind",
    "RouteRecord",
    "RouteRequest",
    "RoutingPolicy",
    "error_reason",
    "resolve_route",
]

FALLBACK_TIMEOUT: Final[str] = "timeout"
FALLBACK_TARGET_NOT_ALLOWED: Final[str] = "target_not_allowed"
FALLBACK_TARGET_NOT_STARTABLE: Final[str] = "target_not_startable"
FALLBACK_INTENT_NOT_SUPPORTED: Final[str] = "intent_not_supported"
FALLBACK_INTENT_UNKNOWN: Final[str] = "intent_unknown"
FALLBACK_AUDIT_UNAVAILABLE: Final[str] = "audit_unavailable"
# L18. The turn continues a conversation: a router reads one message alone, so "and which of those
# is the most urgent?" lands on whatever "those" sounds like, and a plan's compose has no history.
# Such a turn is not routed; the router is not asked.
FALLBACK_FOLLOW_UP: Final[str] = "follow_up"
# S8. A route runs delegated, as an ask_peer from the requested profile to its target would. These
# say why it could not: no peer gate or policy is wired (`delegation_unavailable`), the two-sided
# peer gate refused the hop (`peer_refused:<gate reason>`) or the person's policy does not allow
# `ask_peer` to that target (`ask_peer_not_allowed:<effect>`).
FALLBACK_DELEGATION_UNAVAILABLE: Final[str] = "delegation_unavailable"
FALLBACK_PEER_REFUSED: Final[str] = "peer_refused"
FALLBACK_ASK_PEER_NOT_ALLOWED: Final[str] = "ask_peer_not_allowed"


def error_reason(error: BaseException) -> str:
    """The fallback reason of a router that raised: the class, never the message (which may hold
    a credential or a customer's words)."""
    return f"error:{type(error).__name__}"


class RouteKind(StrEnum):
    PASS = "pass"
    ROUTE = "route"
    INTENT = "intent"


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """What a router reads: the person's text, the profile they asked for and where they are."""

    text: str
    profile_id: str
    channel: str
    tenant_id: str
    modality: str | None = None
    # S10. Set only when the fast path is shadow or on: ask the extra questions (`Assessment`), and
    # the recent conversation (oldest first, short lines) when the session has one.
    fast_path: bool = False
    history: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """What a router answered. `confidence` is in [0, 1] when given; `calibrated` says whether it
    is a calibrated probability (a classifier's) or a score nobody calibrated (a model's guess)."""

    kind: RouteKind
    profile_id: str | None = None
    intent: str | None = None
    arguments: Mapping[str, object] = field(default_factory=dict)
    label: str | None = None
    confidence: float | None = None
    calibrated: bool = False
    model: str | None = None
    # L12. Why the router answered what it did (a fixed code, never text) and what it spent.
    reason: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    # S10. The router's answers to the fast path's questions; None when it was not asked or did
    # not answer them.
    assessment: Assessment | None = None

    def __post_init__(self) -> None:
        if self.kind is RouteKind.ROUTE and not (self.profile_id or "").strip():
            raise ValueError("a route decision must name the profile to route to")
        if self.kind is RouteKind.INTENT and not (self.intent or "").strip():
            raise ValueError("an intent decision must name the intent")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence!r}")
        for name, count in self.usage.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"usage {name!r} must be a whole number of at least 0")
        # Plain dict copies: the record is pickled into the workflow's operation log.
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "usage", dict(self.usage))


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """A profile's `routing:` block: the router it consults, where that router may send it and
    whether an intent may run its declared plan (D97; off unless the profile says `intents: true`).
    """

    router: str
    targets: tuple[str, ...]
    intents: bool = False


@dataclass(frozen=True, slots=True)
class RouteRecord:
    """One consultation, as audited and as recorded in the turn's operation log.

    `decision` is None when the router gave no answer (a timeout, an error). `profile_id` is the
    profile the turn runs under, which equals `requested_profile` unless the route was applied.
    """

    router: str
    requested_profile: str
    profile_id: str
    decision: RouteKind | None
    elapsed_ms: int
    label: str | None = None
    confidence: float | None = None
    calibrated: bool = False
    model: str | None = None
    intent: str | None = None
    fallback_reason: str | None = None
    # L12. The router's reason and token usage (None when it said nothing).
    reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # S8. Both identities of a delegated route: the subject the turn runs as (the peer identity)
    # and the person it acts for. None when the route was not applied (the turn runs as the person).
    actor: str | None = None
    on_behalf_of: str | None = None
    # S10. The fast path: its mode (None when off), its verdict (`domain/fast_path.py` codes; in
    # shadow what it WOULD have done), the intents it detected (comma separated) and every answer
    # with its value (`Assessment.summary`).
    fast_path_mode: str | None = None
    fast_path: str | None = None
    fast_path_intents: str | None = None
    assessment: str | None = None

    def __setstate__(self, state: list[object]) -> None:
        """A record pickled by an older build reads back with the newer fields at their default."""
        restore_state(self, state)

    @property
    def routed(self) -> bool:
        return self.profile_id != self.requested_profile

    @property
    def composite(self) -> bool:
        """Whether the turn answers a composite plan (the fast path on and its verdict applied)."""
        return self.fast_path_mode == FastPathMode.ON.value and self.fast_path == COMPOSITE

    @property
    def outcome(self) -> str:
        """`routed`, `passed`, `composite` or `fallback:<reason>`: the short label the event feed
        shows."""
        if self.fallback_reason is not None:
            return f"fallback:{self.fallback_reason}"
        if self.composite:
            return COMPOSITE
        return "routed" if self.routed else "passed"


def resolve_route(
    decision: RouteDecision,
    policy: RoutingPolicy,
    *,
    requested: str,
    entry_points: Collection[str],
    plans: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """The profile the turn runs under, and why it fell back (None when it did not).

    `entry_points` are the loaded profiles a channel may start. A route to the requested profile
    itself is a pass. `plans` maps each intent with a declared plan to its owning profile (None:
    no intent executor is wired); an intent lands on that profile by the rules a route lands by.
    """
    match decision.kind:
        case RouteKind.PASS:
            return requested, None
        case RouteKind.INTENT:
            if not policy.intents or plans is None:
                return requested, FALLBACK_INTENT_NOT_SUPPORTED
            owner = plans.get(decision.intent or "")
            if owner is None:
                return requested, FALLBACK_INTENT_UNKNOWN
            return _land(owner, policy, requested=requested, entry_points=entry_points)
        case RouteKind.ROUTE:
            return _land(
                decision.profile_id or requested,
                policy,
                requested=requested,
                entry_points=entry_points,
            )


def _land(
    target: str, policy: RoutingPolicy, *, requested: str, entry_points: Collection[str]
) -> tuple[str, str | None]:
    if target == requested:
        return requested, None
    if target not in policy.targets:
        return requested, FALLBACK_TARGET_NOT_ALLOWED
    if target not in entry_points:
        return requested, FALLBACK_TARGET_NOT_STARTABLE
    return target, None
