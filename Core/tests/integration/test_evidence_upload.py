"""POST /evidence/{corr_id} - the upload route. docs/TASKS.md#t-f7-08.

Phase:   F7 - Multimodal input and evidence
Covers:  adapters/driving/http/routes.py

WHAT THIS FILE DOES NOT NEED
    No database, no channel, no workflow. `IngestMedia` (t-f7-03) already owns the
    size-then-sniff-then-accept order and its own unit test pins it by counting calls on
    a fake `MediaStore`. What THIS route can get wrong is different: forwarding an upload
    to `IngestMedia` at all, and refusing a correlation handle that does not name a live,
    unused evidence request. So `FakeMediaStore` is IMPORTED from tests/fakes/ports.py -
    the one shared stand-in for the port (docs/TASKS.md#t-f7-02,
    tests/unit/test_ports_media_store.py's `test_exactly_one_media_store_fake_exists_...`
    is the guard that keeps it that way) - rather than redeclared here, which would be
    exactly the defect that guard exists to stop. This file adds only the fake
    "resolve_evidence" collaborator, standing in for whatever composition wires against
    `HumanGateway` later - this task owns the route, not that wiring.

THE TWO PROPERTIES THIS FILE PINS
    1. ORDER SURVIVES THE HTTP BOUNDARY. An oversized upload must never reach `sniff` or
       `MediaStore.put` - `store.calls == []` is the only thing that can tell "rejected
       before storing" apart from "rejected after storing, then rolled back", exactly per
       tests/unit/test_ingest_media.py's own reasoning.
    2. A CORRELATION HANDLE IS SINGLE-USE, AND A STRANGER'S GUESS LOOKS THE SAME AS A
       SPENT ONE. Both an unknown handle and one already resolved must refuse without
       touching `MediaStore` - collapsing the two into one answer is deliberate, the same
       choice `HumanGateway.correlate` and `DecideApproval`'s `UnknownCorrelationError`
       already make: telling a stranger POSTing a guessed id "actually that one was
       already used" confirms a real id exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.routes import create_app
from agent_core.application.ingest_media import IngestMedia
from agent_core.domain.media import MediaKind, MediaPolicy
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId
from tests.fakes.ports import FakeAuditSink, FakeMediaStore

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _png(payload_size: int = 32) -> bytes:
    return PNG_HEADER + b"\x00" * payload_size


_PROFILE = AgentProfile(
    id="evidence-profile",
    persona="collects evidence",
    model="test-model",
    media=MediaPolicy(
        accepted_kinds=frozenset({MediaKind.IMAGE}),
        max_bytes=1_000,
    ),
)


@dataclass
class FakeEvidenceCorrelate:
    """Stands in for whatever composition wires against `HumanGateway.correlate` plus a
    profile lookup. Returns `None` for BOTH an unknown handle and one already consumed -
    the route must not be able to tell them apart, per the module docstring.

    `live` starts as the set of handles that currently name a pending, unanswered
    request. Each successful resolution consumes its handle, exactly like
    `HumanGateway.correlate` cannot be asked twice for a handle nobody re-published.
    """

    live: dict[str, TurnId] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def __call__(self, correlation_id: str) -> tuple[TurnId, AgentProfile] | None:
        self.calls.append(correlation_id)
        turn_id = self.live.pop(correlation_id, None)
        if turn_id is None:
            return None
        return turn_id, _PROFILE


def _client(
    *, store: FakeMediaStore, resolve: FakeEvidenceCorrelate
) -> TestClient:
    ingest_media = IngestMedia(media=store, audit=FakeAuditSink())

    async def _unused_starter(request: object) -> object:  # pragma: no cover - unused
        raise AssertionError("the evidence route must not start a turn")

    app = create_app(
        start_turn=_unused_starter,  # type: ignore[arg-type]
        ingest_media=ingest_media,
        resolve_evidence=resolve,
    )
    return TestClient(app)


def test_an_oversized_upload_is_rejected_before_it_is_sniffed_or_stored() -> None:
    store = FakeMediaStore()
    resolve = FakeEvidenceCorrelate(live={"handle-1": TurnId("turn-1")})
    client = _client(store=store, resolve=resolve)

    # A VALID png, one byte over the profile's max_bytes. If the route sniffed or stored
    # before checking size, this payload would sail through both - it is genuine image
    # data, not garbage a sniff-first implementation would also refuse.
    oversized = _png(_PROFILE.media.max_bytes)
    assert len(oversized) > _PROFILE.media.max_bytes

    response = client.post(
        "/evidence/handle-1",
        files={"file": ("photo.png", oversized, "image/png")},
    )

    assert response.status_code in (400, 413), response.text
    assert store.calls == [], (
        "MediaStore.put was called for an oversized upload - it must be rejected before "
        "the payload is sniffed or stored, exactly like tests/unit/test_ingest_media.py "
        "pins for IngestMedia itself."
    )


def test_upload_against_an_unknown_correlation_id_is_refused() -> None:
    store = FakeMediaStore()
    resolve = FakeEvidenceCorrelate(live={})  # nothing pending, ever
    client = _client(store=store, resolve=resolve)

    response = client.post(
        "/evidence/no-such-handle",
        files={"file": ("photo.png", _png(), "image/png")},
    )

    assert response.status_code == 404
    assert store.calls == [], (
        "an unknown correlation handle must never reach MediaStore - refusing after "
        "storing the bytes would have already paid for the untrusted upload."
    )


def test_upload_against_an_already_resolved_correlation_id_is_refused() -> None:
    store = FakeMediaStore()
    resolve = FakeEvidenceCorrelate(live={"handle-1": TurnId("turn-1")})
    client = _client(store=store, resolve=resolve)

    first = client.post(
        "/evidence/handle-1",
        files={"file": ("photo.png", _png(), "image/png")},
    )
    assert first.status_code == 202, first.text
    assert len(store.calls) == 1

    # The SAME handle, posted again. `FakeEvidenceCorrelate` already consumed it, so this
    # must look exactly like the unknown-handle case above - a stranger who intercepts a
    # spent handle must learn nothing from the response.
    second = client.post(
        "/evidence/handle-1",
        files={"file": ("photo.png", _png(), "image/png")},
    )

    assert second.status_code == 404
    assert len(store.calls) == 1, (
        "a second upload against an already-resolved correlation id reached MediaStore - "
        "the handle must be refused, not consumed twice."
    )


def test_an_unwired_evidence_route_refuses_loudly_rather_than_guessing() -> None:
    """`ingest_media`/`resolve_evidence` are optional seats, exactly like `lookup_turn`
    and `decide` above them. Unwired, the route must 503 rather than accept an upload it
    cannot validate or store."""

    async def _unused_starter(request: object) -> object:  # pragma: no cover - unused
        raise AssertionError("the evidence route must not start a turn")

    client = TestClient(create_app(start_turn=_unused_starter))  # type: ignore[arg-type]

    response = client.post(
        "/evidence/handle-1",
        files={"file": ("photo.png", _png(), "image/png")},
    )

    assert response.status_code == 503


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
