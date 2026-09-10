"""POST /turns - F0's end-to-end proof.

Phase:   F0
Tasks:   docs/TASKS.md#t-f0-03
Covers:  adapters/driving/http/routes.py

WHY THIS NEEDS NO DATABASE
    What is under test is the HTTP boundary: the 202, the identity that is built before
    anything downstream is called, and the refusal to wait for the turn. The thing that
    starts the turn arrives through the seam as a fake, so a missing Postgres cannot make
    this suite pass or fail for the wrong reason.

THE BUG EACH TEST EXISTS TO CATCH
    A route that waits for the turn holds a connection for as long as the agent runs -
    and, once F3 lands, for as long as a human takes to answer, which is days. It then
    dies on the next deploy and takes the turn with it. The fakes below RAISE when the
    route reaches for the result or for the pending requests, so a blocking route fails
    loudly here instead of quietly in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.routes import create_app
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
)


class TurnAwaited(AssertionError):
    """The route reached for something only the workflow may touch."""


@dataclass
class FakeTurnHandle:
    """What starting a turn hands back: an id now, everything else later.

    Every member except `turn_id` is a trap. The route is allowed to read the id and
    nothing else.
    """

    turn_id: TurnId
    awaited: bool = False
    resolved: bool = False

    async def result(self) -> object:
        self.awaited = True
        raise TurnAwaited(
            "the route awaited the turn's result; it must answer 202 and let the "
            "workflow finish the turn"
        )

    @property
    def pending(self) -> object:
        raise TurnAwaited(
            "the route read the pending requests of a turn it must never resolve"
        )

    async def resolve(self, *args: object, **kwargs: object) -> object:
        self.resolved = True
        raise TurnAwaited("the route resolved a pending turn; that is the workflow's job")


@dataclass
class FakeTurnStarter:
    """Records what it was asked to start and returns immediately, like DBOS does."""

    started: list[TurnRequest] = field(default_factory=list)
    handles: list[FakeTurnHandle] = field(default_factory=list)

    async def __call__(self, request: TurnRequest) -> FakeTurnHandle:
        self.started.append(request)
        handle = FakeTurnHandle(turn_id=TurnId(f"turn-{len(self.started)}"))
        self.handles.append(handle)
        return handle


_HEADERS = {
    "X-Subject-Id": "u-1",
    "X-Tenant-Id": "t-1",
    "X-Roles": "operator, auditor",
    "X-Channel": "whatsapp",
}

_BODY = {
    "session_id": "s-1",
    "profile_id": "delivery_optimizer",
    "text": "where is my order",
}


def _client(starter: FakeTurnStarter) -> TestClient:
    return TestClient(create_app(start_turn=starter))


def test_post_turns_returns_202_with_a_turn_id_without_awaiting_the_turn() -> None:
    starter = FakeTurnStarter()

    response = _client(starter).post("/turns", json=_BODY, headers=_HEADERS)

    assert response.status_code == 202
    assert response.json()["turn_id"] == "turn-1"
    assert len(starter.started) == 1
    assert starter.handles[0].awaited is False


def test_the_route_never_resolves_a_pending_turn() -> None:
    starter = FakeTurnStarter()

    response = _client(starter).post("/turns", json=_BODY, headers=_HEADERS)
    handle = starter.handles[0]

    assert response.status_code == 202
    assert handle.resolved is False
    assert handle.awaited is False
    # The answer says a turn was accepted and nothing about how it went. A body that
    # carried a result or a pending request could only have come from waiting for one.
    assert set(response.json()) <= {"turn_id", "status"}


def test_identity_is_built_into_calleridentity_before_any_use_case_call() -> None:
    starter = FakeTurnStarter()

    # The body claims an identity. It must not be believed: only the authenticated
    # request carries one, and a caller who can name their own subject_id can name
    # someone else's.
    response = _client(starter).post(
        "/turns", json={**_BODY, "subject_id": "attacker"}, headers=_HEADERS
    )

    assert response.status_code == 202
    started = starter.started[0]
    assert isinstance(started, TurnRequest)
    assert started.caller == CallerIdentity(
        subject_id="u-1",
        channel="whatsapp",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator", "auditor"}),
    )
    assert started.session == SessionRef(
        session_id=SessionId("s-1"), tenant_id=TenantId("t-1")
    )
    assert started.profile_id == "delivery_optimizer"
    assert started.input.text == "where is my order"


def test_a_request_without_an_authenticated_subject_never_reaches_the_use_case() -> None:
    starter = FakeTurnStarter()

    response = _client(starter).post("/turns", json=_BODY, headers={"X-Tenant-Id": "t-1"})

    assert response.status_code == 401
    # Identity is built BEFORE anything downstream runs, so an unidentified request
    # leaves no half-started turn behind.
    assert starter.started == []
