"""Integration tests for the four transcript HTTP read endpoints.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-06
Covers:  adapters/driving/http/transcript_routes.py

WHY THIS NEEDS NO DATABASE
    The reader arrives through the seam `ports/transcript_reader.py` defines, exactly like
    `PgTranscriptReader` does in production - here it is a fake. What is under test is the
    HTTP boundary: which `Audience` reaches the reader for which route, whether a caller
    without the admin role is turned away before the reader is ever called, and whether the
    route serialises the answer back without adding a leak of its own. Whether the real
    Postgres projection filters correctly is `test_transcript_repository.py`'s job, not
    this file's.

THE PROPERTY NON-NEGOTIABLE #11 ACTUALLY DEMANDS
    Read as USER and as ADMIN, the same conversation must come back with different
    `EntryKind`s (the tool call is admin-only), the USER projection must never carry the
    tool NAME anywhere in its body, and the USER projection must still show that something
    is pending - a `PENDING_PLACEHOLDER` standing in for the hidden `PENDING_REQUEST`.
    Losing any one of those three silently reopens #11 from the HTTP side even though
    `domain/transcript.py` and the Postgres adapter both already hold it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.transcript_routes import create_transcript_router
from agent_core.domain.transcript import (
    VISIBILITY,
    VISIBLE_TO,
    Audience,
    ConversationSummaryRow,
    EntryKind,
    TranscriptEntry,
    TranscriptPage,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId

_TENANT = TenantId("t-1")
_SESSION = SessionRef(session_id=SessionId("s-1"), tenant_id=_TENANT)
_TOOL_NAME = "issue_refund"
_AT = datetime(2026, 1, 1, tzinfo=UTC)

_RAW_ENTRIES: tuple[TranscriptEntry, ...] = (
    TranscriptEntry(
        entry_id="e-1",
        turn_id=TurnId("turn-1"),
        kind=EntryKind.USER_MESSAGE,
        at=_AT,
        payload={"text": "please refund my order"},
    ),
    TranscriptEntry(
        entry_id="e-2",
        turn_id=TurnId("turn-1"),
        kind=EntryKind.TOOL_CALL,
        at=_AT,
        payload={"tool": _TOOL_NAME, "arguments": {"order_id": "o-1"}},
    ),
    TranscriptEntry(
        entry_id="e-3",
        turn_id=TurnId("turn-1"),
        kind=EntryKind.PENDING_REQUEST,
        at=_AT,
        payload={"tool": _TOOL_NAME, "reason": "needs approval"},
    ),
)


def _substitutes_placeholder(audience: Audience) -> bool:
    """Same rule `transcript_repository.py` states: hidden from them, placeholder visible
    to them - reproduced here so the fake filters exactly the way the real projection
    does, rather than trusting the route to do a job that is not its job."""
    return (
        not VISIBILITY[EntryKind.PENDING_REQUEST][audience]
        and VISIBILITY[EntryKind.PENDING_PLACEHOLDER][audience]
    )


@dataclass
class FakeTranscriptReader:
    """Filters exactly like `PgTranscriptReader`, so what is under test is whether the
    ROUTE asked for the right `Audience` for the right caller - not whether filtering
    itself works."""

    pages: list[tuple[SessionRef, Audience]] = field(default_factory=list)

    async def page(
        self,
        session: SessionRef,
        audience: Audience,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TranscriptPage:
        self.pages.append((session, audience))
        visible = VISIBLE_TO[audience]
        entries: list[TranscriptEntry] = []
        for entry in _RAW_ENTRIES:
            if entry.kind is EntryKind.PENDING_REQUEST and _substitutes_placeholder(audience):
                entries.append(
                    TranscriptEntry(
                        entry_id=entry.entry_id,
                        turn_id=entry.turn_id,
                        kind=EntryKind.PENDING_PLACEHOLDER,
                        at=entry.at,
                        payload={"since": entry.at.isoformat()},
                    )
                )
                continue
            if entry.kind in visible:
                entries.append(entry)
        return TranscriptPage(session=session, audience=audience, entries=tuple(entries))

    async def list_conversations(
        self,
        tenant: TenantId,
        audience: Audience,
        *,
        profile_id: str | None = None,
        suspended_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[tuple[ConversationSummaryRow, ...], str | None]:
        return (), None


def _client(reader: FakeTranscriptReader) -> TestClient:
    app = FastAPI()
    app.include_router(create_transcript_router(reader=reader))
    return TestClient(app)


_HEADERS_USER = {"X-Subject-Id": "u-1", "X-Tenant-Id": "t-1"}
_HEADERS_ADMIN = {"X-Subject-Id": "op-1", "X-Tenant-Id": "t-1", "X-Roles": "admin"}


def test_the_same_conversation_reads_differently_for_user_and_admin() -> None:
    reader = FakeTranscriptReader()
    client = _client(reader)

    user_response = client.get("/conversations/s-1", headers=_HEADERS_USER)
    admin_response = client.get("/admin/conversations/s-1", headers=_HEADERS_ADMIN)

    assert user_response.status_code == 200
    assert admin_response.status_code == 200

    user_kinds = {entry["kind"] for entry in user_response.json()["entries"]}
    admin_kinds = {entry["kind"] for entry in admin_response.json()["entries"]}

    assert user_kinds != admin_kinds
    assert "tool_call" in admin_kinds
    assert "tool_call" not in user_kinds
    assert "pending_request" in admin_kinds
    assert "pending_request" not in user_kinds


def test_the_user_projection_never_contains_a_tool_name() -> None:
    reader = FakeTranscriptReader()
    client = _client(reader)

    response = client.get("/conversations/s-1", headers=_HEADERS_USER)

    body_text = json.dumps(response.json())
    assert _TOOL_NAME not in body_text


def test_the_user_projection_still_shows_that_something_is_pending() -> None:
    reader = FakeTranscriptReader()
    client = _client(reader)

    response = client.get("/conversations/s-1", headers=_HEADERS_USER)

    kinds = [entry["kind"] for entry in response.json()["entries"]]
    assert "pending_placeholder" in kinds


def test_the_admin_route_requires_the_admin_role() -> None:
    reader = FakeTranscriptReader()
    client = _client(reader)

    response = client.get(
        "/admin/conversations/s-1", headers={"X-Subject-Id": "u-1", "X-Tenant-Id": "t-1"}
    )

    assert response.status_code == 403
    assert reader.pages == []


def test_the_tenant_is_the_authenticated_tenant_never_a_query_parameter() -> None:
    reader = FakeTranscriptReader()
    client = _client(reader)

    client.get("/conversations/s-1", headers=_HEADERS_USER)

    session, audience = reader.pages[0]
    assert session == _SESSION
    assert audience is Audience.USER
