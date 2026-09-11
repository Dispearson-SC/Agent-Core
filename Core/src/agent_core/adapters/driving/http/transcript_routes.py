"""Driving adapter: FastAPI routes over TranscriptReader - the four read endpoints.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-06
Status:  DONE - page() and list_conversations(), one USER/ADMIN route pair each
Implements: nothing - it calls ports/transcript_reader.py through an injected reader

ROUTES
    GET /conversations                     the caller's own inbox, USER-projected
    GET /conversations/{session_id}        the caller's own conversation, USER-projected
    GET /admin/conversations               the operator inbox, ADMIN-projected
    GET /admin/conversations/{session_id}  any conversation in the tenant, ADMIN-projected

WHY FOUR ROUTES INSTEAD OF ONE ROUTE PLUS A ROLE BRANCH
    `domain/transcript.py` spent its whole design keeping `Audience` from being filled in
    by a fallback - "written cell by cell precisely so that a new audience cannot inherit a
    default". A single endpoint that reads a header and branches into `Audience.ADMIN`
    would reintroduce exactly that shape one layer up here: one more `if` standing between
    a caller and every tool name in the conversation. Splitting the path makes the admin
    projection an explicit destination this module deliberately exposes, not a query-string
    flag it could forget to check. The `Audience` each handler asks the reader for is a
    literal at the call site, never a value threaded through a conditional.

    Authorization for the `/admin/*` pair is a plain role check on the already-authenticated
    `CallerIdentity` - it does not mint a new identity type. CLAUDE.md non-negotiable #9
    ("`AdminIdentity` is never derived from `CallerIdentity`") is about the ability to WRITE
    the knowledge base through `ports/knowledge_admin.py`; this is a read-only projection
    that is already safe to hand a low-privilege caller in its USER form, so gating the
    wider projection behind a role is the correct-weight control here, not a shortcut
    around #9.

WHO THE SESSION BELONGS TO
    The tenant is the AUTHENTICATED tenant, exactly like `routes.py#post_turns` - never one
    a path segment or query string asks for. `SessionRef` exists so a session can never be
    named without a tenant riding along with it; accepting a caller-supplied tenant here
    would let one tenant read another's conversation by guessing a session id.

WHAT THIS FILE DOES NOT DO
    `adapters/driven/persistence_pg/transcript_repository.py` already does the filtering,
    the placeholder substitution and the tenant-scoped SQL. This module only authenticates,
    picks the `Audience` the route name promises, calls the reader, and serialises the
    result - "the HTTP surface over them, nothing more." It is not mounted into any app
    here; `t-f0-05` mounts every router in a later wave.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel

from agent_core.domain.transcript import Audience, ConversationSummaryRow, TranscriptPage
from agent_core.domain.turn import CallerIdentity, SessionId, SessionRef, TenantId
from agent_core.ports.transcript_reader import TranscriptReader

__all__ = ["create_transcript_router"]

_ADMIN_ROLE = "admin"
_DEFAULT_CHANNEL = "http"


def _authenticate(
    subject_id: str | None, tenant_id: str | None, channel: str | None, roles: str | None
) -> CallerIdentity:
    """Same shape as `routes.py#_authenticate` - identity is built once, the same way,
    everywhere it is needed. A second, slightly different implementation is how two auth
    paths drift apart."""
    if not subject_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An authenticated subject and tenant are required.",
        )
    return CallerIdentity(
        subject_id=subject_id,
        channel=channel or _DEFAULT_CHANNEL,
        tenant_id=TenantId(tenant_id),
        roles=frozenset(part.strip() for part in (roles or "").split(",") if part.strip()),
    )


def _require_admin(caller: CallerIdentity) -> None:
    if _ADMIN_ROLE not in caller.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The admin transcript view requires the admin role.",
        )


class _EntryOut(BaseModel):
    entry_id: str
    turn_id: str
    kind: str
    at: str
    payload: dict[str, Any]
    cost_usd: str | None = None


class _PageOut(BaseModel):
    session_id: str
    audience: str
    entries: list[_EntryOut]
    next_cursor: str | None
    has_more: bool


class _SummaryOut(BaseModel):
    session_id: str
    profile_id: str
    last_activity_at: str
    message_count: int
    is_suspended: bool
    waiting_since: str | None
    total_cost_usd: str | None


class _ListOut(BaseModel):
    conversations: list[_SummaryOut]
    next_cursor: str | None


def _page_out(page: TranscriptPage) -> _PageOut:
    return _PageOut(
        session_id=str(page.session.session_id),
        audience=str(page.audience),
        entries=[
            _EntryOut(
                entry_id=entry.entry_id,
                turn_id=str(entry.turn_id),
                kind=str(entry.kind),
                at=entry.at.isoformat(),
                payload=entry.payload,
                cost_usd=str(entry.cost_usd) if entry.cost_usd is not None else None,
            )
            for entry in page.entries
        ],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _summary_out(row: ConversationSummaryRow) -> _SummaryOut:
    return _SummaryOut(
        session_id=str(row.session.session_id),
        profile_id=row.profile_id,
        last_activity_at=row.last_activity_at.isoformat(),
        message_count=row.message_count,
        is_suspended=row.is_suspended,
        waiting_since=row.waiting_since.isoformat() if row.waiting_since is not None else None,
        total_cost_usd=str(row.total_cost_usd) if row.total_cost_usd is not None else None,
    )


def create_transcript_router(*, reader: TranscriptReader) -> APIRouter:
    """The HTTP surface, wired to one `TranscriptReader`.

    Not mounted here - `t-f0-05` mounts every router into the app in a later wave.
    """
    router = APIRouter()

    async def _handle_page(
        *,
        session_id: str,
        audience: Audience,
        x_subject_id: str | None,
        x_tenant_id: str | None,
        x_channel: str | None,
        x_roles: str | None,
        cursor: str | None,
        limit: int,
    ) -> _PageOut:
        caller = _authenticate(x_subject_id, x_tenant_id, x_channel, x_roles)
        if audience is Audience.ADMIN:
            _require_admin(caller)
        session = SessionRef(session_id=SessionId(session_id), tenant_id=caller.tenant_id)
        page = await reader.page(session, audience, cursor=cursor, limit=limit)
        return _page_out(page)

    async def _handle_list(
        *,
        audience: Audience,
        x_subject_id: str | None,
        x_tenant_id: str | None,
        x_channel: str | None,
        x_roles: str | None,
        profile_id: str | None,
        suspended_only: bool,
        cursor: str | None,
        limit: int,
    ) -> _ListOut:
        caller = _authenticate(x_subject_id, x_tenant_id, x_channel, x_roles)
        if audience is Audience.ADMIN:
            _require_admin(caller)
        rows, next_cursor = await reader.list_conversations(
            caller.tenant_id,
            audience,
            profile_id=profile_id,
            suspended_only=suspended_only,
            cursor=cursor,
            limit=limit,
        )
        return _ListOut(
            conversations=[_summary_out(row) for row in rows], next_cursor=next_cursor
        )

    @router.get("/conversations/{session_id}", response_model=_PageOut)
    async def get_conversation_as_user(
        session_id: str,
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = 50,
    ) -> _PageOut:
        return await _handle_page(
            session_id=session_id,
            audience=Audience.USER,
            x_subject_id=x_subject_id,
            x_tenant_id=x_tenant_id,
            x_channel=x_channel,
            x_roles=x_roles,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/admin/conversations/{session_id}", response_model=_PageOut)
    async def get_conversation_as_admin(
        session_id: str,
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = 50,
    ) -> _PageOut:
        return await _handle_page(
            session_id=session_id,
            audience=Audience.ADMIN,
            x_subject_id=x_subject_id,
            x_tenant_id=x_tenant_id,
            x_channel=x_channel,
            x_roles=x_roles,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/conversations", response_model=_ListOut)
    async def list_conversations_as_user(
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
        profile_id: Annotated[str | None, Query()] = None,
        suspended_only: Annotated[bool, Query()] = False,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = 50,
    ) -> _ListOut:
        return await _handle_list(
            audience=Audience.USER,
            x_subject_id=x_subject_id,
            x_tenant_id=x_tenant_id,
            x_channel=x_channel,
            x_roles=x_roles,
            profile_id=profile_id,
            suspended_only=suspended_only,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/admin/conversations", response_model=_ListOut)
    async def list_conversations_as_admin(
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
        profile_id: Annotated[str | None, Query()] = None,
        suspended_only: Annotated[bool, Query()] = False,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = 50,
    ) -> _ListOut:
        return await _handle_list(
            audience=Audience.ADMIN,
            x_subject_id=x_subject_id,
            x_tenant_id=x_tenant_id,
            x_channel=x_channel,
            x_roles=x_roles,
            profile_id=profile_id,
            suspended_only=suspended_only,
            cursor=cursor,
            limit=limit,
        )

    return router
