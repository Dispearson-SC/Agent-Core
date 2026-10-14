"""The turn-router seam: a router consulted before the model when a turn starts.

Subject: domain/routing.py, application/route_turn.py and the `routing:` block of a profile.

THE RULES UNDER TEST. A router answers `pass`, `route(profile_id)` or `intent(name, arguments)`.
Only a profile that declares `routing:` consults one, and only a turn a person or a channel
started (never a peer's or a subagent's). A route lands only on a profile the declaring profile
lists in `routing.targets` that is loaded and is an entry point; anything else, a timeout, a
router that raises and (until intent executors exist) an intent all fall back to the requested
profile with the reason recorded. Every consultation is audited with its label, confidence,
calibration, model and elapsed time; a route that cannot be audited is not applied.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from agent_core.application.route_turn import MAX_AUDIT_TEXT, RouteTurn
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.routing import (
    FALLBACK_AUDIT_UNAVAILABLE,
    FALLBACK_INTENT_NOT_SUPPORTED,
    FALLBACK_TARGET_NOT_ALLOWED,
    FALLBACK_TARGET_NOT_STARTABLE,
    FALLBACK_TIMEOUT,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
    RoutingPolicy,
    resolve_route,
)
from agent_core.domain.turn import (
    CallerIdentity,
    PeerOwner,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)

FRONT = "front_desk"
BILLING = "billing"
HELPER = "helper"  # a peer-only agent: never a route target


def _profiles(**routing: Any) -> dict[str, AgentProfile]:
    front = AgentProfile.from_mapping(
        {
            "id": FRONT,
            "persona": "You greet.",
            "model": "m",
            "routing": {"router": "keyword", "targets": [BILLING, HELPER], **routing},
        }
    )
    return {
        FRONT: front,
        BILLING: AgentProfile(id=BILLING, persona="You bill.", model="m"),
        HELPER: AgentProfile(id=HELPER, persona="You help.", model="m", mode=AgentMode.PEER),
        "plain": AgentProfile(id="plain", persona="No routing.", model="m"),
    }


def _request(profile_id: str = FRONT, *, text: str = "my invoice is wrong") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s-1"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=profile_id,
        input=UserInput(text=text),
    )


class ScriptedRouter:
    """A `TurnRouter` that answers one decision, sleeps or raises, and records what it saw."""

    def __init__(
        self,
        decision: RouteDecision | None = None,
        *,
        sleep: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.decision = decision or RouteDecision(kind=RouteKind.PASS)
        self.sleep = sleep
        self.error = error
        self.seen: list[tuple[str, RouteRequest]] = []

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        self.seen.append((router, request))
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.error is not None:
            raise self.error
        return self.decision


class RecordedRoutes:
    """A `RouteAudit`: a list, append-only."""

    def __init__(self) -> None:
        self.calls: list[tuple[TurnId, str, RouteRecord]] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        self.calls.append((turn_id, tenant_id, record))


class FailingAudit(RecordedRoutes):
    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        raise RuntimeError("the audit database is down")


def _use_case(
    router: ScriptedRouter,
    *,
    audit: RecordedRoutes | None = None,
    profiles: dict[str, AgentProfile] | None = None,
    timeout_ms: int = 1500,
) -> tuple[RouteTurn, RecordedRoutes]:
    sink = audit or RecordedRoutes()
    ticks = iter(range(0, 10_000, 7))
    return (
        RouteTurn(
            routers=router,
            audit=sink,
            profiles=profiles or _profiles(),
            timeout_ms=timeout_ms,
            clock=lambda: next(ticks) / 1000,
        ),
        sink,
    )


def _run(use_case: RouteTurn, request: TurnRequest | None = None) -> Any:
    return asyncio.run(use_case.execute(TurnId("turn-1"), request or _request()))


# --- the decision type -----------------------------------------------------------------------


def test_a_decision_is_pass_route_or_intent_and_each_carries_what_it_needs() -> None:
    assert RouteDecision(kind=RouteKind.PASS).profile_id is None
    routed = RouteDecision(
        kind=RouteKind.ROUTE, profile_id=BILLING, label="billing", confidence=0.91,
        calibrated=True, model="clf-1",
    )
    assert (routed.profile_id, routed.label, routed.calibrated) == (BILLING, "billing", True)
    intent = RouteDecision(kind=RouteKind.INTENT, intent="refund", arguments={"order": "7"})
    assert (intent.intent, dict(intent.arguments)) == ("refund", {"order": "7"})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": RouteKind.ROUTE},  # a route names no profile
        {"kind": RouteKind.ROUTE, "profile_id": "  "},
        {"kind": RouteKind.INTENT},  # an intent names nothing
        {"kind": RouteKind.PASS, "confidence": 1.5},
        {"kind": RouteKind.PASS, "confidence": -0.1},
    ],
)
def test_a_decision_that_says_too_little_or_an_impossible_confidence_is_refused(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        RouteDecision(**kwargs)


# --- what a route may land on ----------------------------------------------------------------

POLICY = RoutingPolicy(router="keyword", targets=(BILLING, HELPER))
ENTRY = frozenset({FRONT, BILLING, "plain"})


def test_a_route_to_a_listed_entry_point_lands_and_a_pass_stays() -> None:
    route = RouteDecision(kind=RouteKind.ROUTE, profile_id=BILLING)
    assert resolve_route(route, POLICY, requested=FRONT, entry_points=ENTRY) == (BILLING, None)
    passed = RouteDecision(kind=RouteKind.PASS)
    assert resolve_route(passed, POLICY, requested=FRONT, entry_points=ENTRY) == (FRONT, None)


def test_a_route_never_lands_on_a_profile_the_config_does_not_allow() -> None:
    elsewhere = RouteDecision(kind=RouteKind.ROUTE, profile_id="plain")
    assert resolve_route(elsewhere, POLICY, requested=FRONT, entry_points=ENTRY) == (
        FRONT,
        FALLBACK_TARGET_NOT_ALLOWED,
    )


def test_a_route_never_lands_on_a_listed_profile_that_is_not_an_entry_point() -> None:
    peer_only = RouteDecision(kind=RouteKind.ROUTE, profile_id=HELPER)
    assert resolve_route(peer_only, POLICY, requested=FRONT, entry_points=ENTRY) == (
        FRONT,
        FALLBACK_TARGET_NOT_STARTABLE,
    )


def test_an_intent_is_accepted_and_treated_as_a_pass_until_intent_executors_exist() -> None:
    intent = RouteDecision(kind=RouteKind.INTENT, intent="refund")
    assert resolve_route(intent, POLICY, requested=FRONT, entry_points=ENTRY) == (
        FRONT,
        FALLBACK_INTENT_NOT_SUPPORTED,
    )


# --- the profile declaration -----------------------------------------------------------------


def test_a_profile_declares_routing_with_a_router_and_its_allowed_targets() -> None:
    profile = _profiles()[FRONT]
    assert profile.routing == RoutingPolicy(router="keyword", targets=(BILLING, HELPER))
    assert AgentProfile(id="x", persona="p", model="m").routing is None


@pytest.mark.parametrize(
    "routing",
    [
        {"targets": [BILLING]},  # no router
        {"router": "", "targets": [BILLING]},
        {"router": "keyword"},  # no targets
        {"router": "keyword", "targets": []},
        {"router": "keyword", "targets": "billing"},
        {"router": "keyword", "targets": [BILLING], "fallback": "x"},  # unknown key
    ],
)
def test_a_malformed_routing_block_refuses_to_load(routing: dict[str, Any]) -> None:
    with pytest.raises(ProfileValidationError):
        AgentProfile.from_mapping({"id": "a", "persona": "p", "model": "m", "routing": routing})


def test_routing_is_part_of_the_content_hash() -> None:
    base = {"id": "a", "persona": "p", "model": "m"}
    plain = AgentProfile.from_mapping(base)
    routed = AgentProfile.from_mapping({**base, "routing": {"router": "r", "targets": ["b"]}})
    assert plain.content_hash != routed.content_hash


# --- who consults a router -------------------------------------------------------------------


def test_only_a_channel_started_turn_of_a_profile_that_declares_routing_consults_one() -> None:
    use_case, _ = _use_case(ScriptedRouter())
    assert use_case.applies(_request(FRONT))
    assert not use_case.applies(_request("plain")), "a profile without routing never routes"
    assert not use_case.applies(_request("nobody")), "an unknown profile is StartTurn's to refuse"
    peer_turn = replace(
        _request(FRONT),
        owner=PeerOwner(root_turn_id="r", asker_turn_id="a", asker="x", call="c"),
    )
    assert not use_case.applies(peer_turn), "an agent's ask is never re-routed"


# --- the use case ----------------------------------------------------------------------------


def test_a_confident_route_changes_the_profile_and_is_audited_with_its_evidence() -> None:
    decision = RouteDecision(
        kind=RouteKind.ROUTE, profile_id=BILLING, label="billing", confidence=0.93,
        calibrated=True, model="clf-1",
    )
    router = ScriptedRouter(decision)
    use_case, audit = _use_case(router)

    routed = _run(use_case)

    assert routed.request.profile_id == BILLING
    assert routed.request.input == _request().input, "only the profile changes"
    name, seen = router.seen[0]
    assert name == "keyword"
    assert (seen.text, seen.profile_id, seen.channel, seen.tenant_id) == (
        "my invoice is wrong", FRONT, "http", "t",
    )
    record = routed.record
    assert (record.requested_profile, record.profile_id, record.decision) == (
        FRONT, BILLING, RouteKind.ROUTE,
    )
    assert (record.label, record.confidence, record.calibrated, record.model) == (
        "billing", 0.93, True, "clf-1",
    )
    assert record.elapsed_ms == 7 and record.fallback_reason is None and record.routed
    assert audit.calls == [(TurnId("turn-1"), "t", record)]


def test_a_pass_keeps_the_requested_profile_and_is_still_audited() -> None:
    use_case, audit = _use_case(ScriptedRouter(RouteDecision(kind=RouteKind.PASS, label="other")))

    routed = _run(use_case)

    assert routed.request == _request()
    assert routed.record.decision is RouteKind.PASS and not routed.record.routed
    assert len(audit.calls) == 1


def test_a_router_slower_than_its_timeout_falls_back_to_the_normal_path() -> None:
    use_case, audit = _use_case(
        ScriptedRouter(RouteDecision(kind=RouteKind.ROUTE, profile_id=BILLING), sleep=1.0),
        timeout_ms=20,
    )

    routed = _run(use_case)

    assert routed.request == _request()
    assert routed.record.decision is None
    assert routed.record.fallback_reason == FALLBACK_TIMEOUT
    assert len(audit.calls) == 1


def test_a_router_that_raises_falls_back_and_names_the_error_class_never_its_text() -> None:
    use_case, _ = _use_case(ScriptedRouter(error=ConnectionError("token=secret refused")))

    routed = _run(use_case)

    assert routed.request == _request()
    assert routed.record.fallback_reason == "error:ConnectionError"
    assert "secret" not in repr(routed.record)


def test_a_route_outside_the_allowed_targets_falls_back_with_the_reason() -> None:
    use_case, _ = _use_case(
        ScriptedRouter(RouteDecision(kind=RouteKind.ROUTE, profile_id="plain", label="x"))
    )

    routed = _run(use_case)

    assert routed.request.profile_id == FRONT
    assert routed.record.decision is RouteKind.ROUTE, "what the router said is kept"
    assert routed.record.fallback_reason == FALLBACK_TARGET_NOT_ALLOWED


def test_an_intent_runs_the_normal_path_with_the_reason_recorded() -> None:
    use_case, _ = _use_case(
        ScriptedRouter(RouteDecision(kind=RouteKind.INTENT, intent="refund", label="refund"))
    )

    routed = _run(use_case)

    assert routed.request.profile_id == FRONT
    assert routed.record.decision is RouteKind.INTENT
    assert routed.record.fallback_reason == FALLBACK_INTENT_NOT_SUPPORTED


def test_a_route_that_cannot_be_audited_is_not_applied() -> None:
    use_case, _ = _use_case(
        ScriptedRouter(RouteDecision(kind=RouteKind.ROUTE, profile_id=BILLING)),
        audit=FailingAudit(),
    )

    routed = _run(use_case)

    assert routed.request.profile_id == FRONT
    assert routed.record.fallback_reason == FALLBACK_AUDIT_UNAVAILABLE


def test_a_turn_the_router_does_not_apply_to_is_returned_unchanged_without_a_call() -> None:
    router = ScriptedRouter(RouteDecision(kind=RouteKind.ROUTE, profile_id=BILLING))
    use_case, audit = _use_case(router)

    routed = _run(use_case, _request("plain"))

    assert routed.request == _request("plain") and routed.record is None
    assert router.seen == [] and audit.calls == []


def test_what_a_router_says_is_recorded_bounded_however_long_it_is() -> None:
    # Any `TurnRouter` adapter may answer; the audit row must not grow with what it says.
    long = "x" * 10_000
    decision = RouteDecision(kind=RouteKind.INTENT, intent=long, label=long, model=long)
    use_case, audit = _use_case(ScriptedRouter(decision))

    record = _run(use_case).record

    for value in (record.label, record.model, record.intent):
        assert value is not None and len(value) == MAX_AUDIT_TEXT and value.startswith("xxx")
    assert audit.calls[0][2] == record


def test_the_error_class_a_router_raised_is_recorded_bounded() -> None:
    error_type = type("E" * 500, (RuntimeError,), {})
    use_case, _ = _use_case(ScriptedRouter(error=error_type("boom")))

    reason = _run(use_case).record.fallback_reason

    assert reason is not None and reason.startswith("error:EEE")
    assert len(reason) == MAX_AUDIT_TEXT


# --- the router's reason and usage are audited (L12) --------------------------------------------


def test_the_routers_reason_and_token_usage_are_audited_bounded() -> None:
    decision = RouteDecision(
        kind=RouteKind.PASS, label="billing", confidence=0.31, reason="below_threshold" + "x" * 500,
        usage={"input_tokens": 318, "output_tokens": 34, "cached": 2},
    )
    use_case, audit = _use_case(ScriptedRouter(decision))

    record = _run(use_case).record

    assert record.reason is not None and record.reason.startswith("below_threshold")
    assert len(record.reason) == MAX_AUDIT_TEXT
    assert (record.input_tokens, record.output_tokens) == (318, 34)
    assert audit.calls[0][2] == record


def test_a_router_that_says_no_reason_and_no_usage_records_none() -> None:
    use_case, _ = _use_case(ScriptedRouter(RouteDecision(kind=RouteKind.PASS)))

    record = _run(use_case).record

    assert (record.reason, record.input_tokens, record.output_tokens) == (None, None, None)


def test_a_decision_refuses_negative_or_non_integer_usage() -> None:
    for usage in ({"input_tokens": -1}, {"input_tokens": 1.5}, {"output_tokens": True}):
        with pytest.raises(ValueError, match="usage"):
            RouteDecision(kind=RouteKind.PASS, usage=usage)  # type: ignore[arg-type]
