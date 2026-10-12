"""C22 (D53): `GET /turns/{id}/events`, the same authorisation as reading the turn.

Subject: adapters/driving/http/routes.py (`create_app(event_feed=...)`) and event_routes.py.
The feed is the real `EventFeed` over an in-memory store; the turn lookup is a fake.

What is defended: identity is required (401), an unknown turn and another tenant's turn are the
same 404 and reach the feed never, an unwired process says 503 rather than 404, and an authorised
caller gets `text/event-stream` whose first frame is a snapshot of THEIR tenant's tree.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.routes import TurnView, create_app
from agent_core.application.event_feed import EventFeed
from agent_core.domain.events import EventKind
from agent_core.domain.turn import CallerIdentity, TurnId
from tests.unit.test_event_feed import ROOT, Store, _event

HEADERS = {"X-Subject-Id": "u", "X-Tenant-Id": "t"}


class _Starter:
    async def __call__(self, request: object) -> object:
        raise AssertionError("not used")


class _Lookup:
    def __init__(self, known: dict[tuple[str, str], str]) -> None:
        self.known = known
        self.calls: list[tuple[str, CallerIdentity]] = []

    async def __call__(self, turn_id: TurnId, caller: CallerIdentity) -> TurnView | None:
        self.calls.append((str(turn_id), caller))
        status = self.known.get((str(caller.tenant_id), str(turn_id)))
        return None if status is None else TurnView(turn_id=turn_id, status="finished")  # type: ignore[arg-type]


def _settled_store() -> Store:
    store = Store()
    for event in (
        _event(ROOT, EventKind.STARTED),
        _event(ROOT, EventKind.FINISHED, elapsed=4),
        _event(ROOT, EventKind.STARTED, tenant="other", root=ROOT, n=1),
    ):
        store.add(event)
    return store


def _app(store: Store, lookup: _Lookup | None = None, *, feed: bool = True) -> TestClient:
    return TestClient(
        create_app(
            start_turn=_Starter(),  # type: ignore[arg-type]
            lookup_turn=lookup or _Lookup({("t", ROOT): "finished"}),
            event_feed=EventFeed(store, store, poll_seconds=0.01) if feed else None,
        )
    )


def test_an_authorised_caller_gets_a_stream_that_starts_with_their_snapshot() -> None:
    with _app(_settled_store()) as client:
        response = client.get(f"/turns/{ROOT}/events", headers=HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    snapshot, end = response.text.strip().split("\n\n")
    assert snapshot.startswith("event: snapshot\nid: 2\n"), "tenant `t`'s events only (seq 1-2)"
    assert json.loads(snapshot.split("data: ", 1)[1])["turns"][0]["state"] == "finished"
    assert end.startswith("event: end")


def test_an_unknown_turn_and_another_tenants_turn_are_the_same_404_and_never_reach_the_feed() -> (
    None
):
    store = _settled_store()
    lookup = _Lookup({("other", ROOT): "finished"})  # exists, but not for tenant `t`
    with _app(store, lookup) as client:
        foreign = client.get(f"/turns/{ROOT}/events", headers=HEADERS)
        unknown = client.get("/turns/never/events", headers=HEADERS)

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == {"detail": "No turn with that id in this tenant."} == unknown.json()
    assert all(call[1].tenant_id == "t" for call in lookup.calls), "the lookup got the caller"


def test_the_stream_requires_an_identity() -> None:
    with _app(_settled_store()) as client:
        response = client.get(f"/turns/{ROOT}/events")

    assert response.status_code == 401


def test_a_process_without_a_feed_says_unavailable_not_missing() -> None:
    with _app(_settled_store(), feed=False) as client:
        response = client.get(f"/turns/{ROOT}/events", headers=HEADERS)

    assert response.status_code == 503
