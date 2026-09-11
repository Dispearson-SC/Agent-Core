"""Driving adapter: the knowledge admin HTTP surface. NOT the chat surface.

Phase:   F8 - Knowledge: Skills and FULL_TEXT
Tasks:   docs/TASKS.md#t-f8-06
Status:  DONE - upsert, soft delete and listing, behind an admin credential
Implements: nothing - it calls ports/knowledge_admin.py through an injected port

ROUTES
    POST   /admin/knowledge/documents                          create or update a version
    DELETE /admin/knowledge/documents/{doc_id}                 soft delete
    GET    /admin/knowledge/collections/{collection}/documents list, optionally with history

A SEPARATE MODULE, A SEPARATE CREDENTIAL, A SEPARATE IDENTITY TYPE
    Non-negotiable #9: "`AdminIdentity` is never derived from `CallerIdentity`. Different
    types, different routes, different policies. No code path widens a chat client into an
    administrator."

    `routes.py` and `transcript_routes.py` authenticate a chat caller from `X-Subject-Id`
    and friends and build a `CallerIdentity`. This module NEVER reads those headers and
    never imports that type. It authenticates one thing - an opaque admin credential - and
    what comes back is a `TenantAdminScope`, minted by the verifier that checked the
    credential and by nothing else.

    That is why the authenticator is INJECTED rather than implemented here. The wall this
    file defends is "an administrator is established by the admin route", and the route
    can only keep that promise if the thing that establishes one is a seam a deployment
    fills with its real identity provider. Implementing a token table here would put a
    credential store in a driving adapter and would make the seam untestable at the same
    time.

WHY `X-Roles: admin` DELIBERATELY DOES NOT WORK HERE
    `transcript_routes.py` gates its wider READ projection on exactly that role, and says
    why that is the correct-weight control there: a transcript projection is already safe
    to hand a low-privilege caller in its USER form. This is not that. A write to the
    corpus changes what the agent tells every future customer, so the control is not a
    role on the chat identity - it is a different identity entirely, arriving through a
    different header, verified by a different verifier.

    A chat caller sending `X-Roles: admin` here gets 401, and the write port is not called
    at all. `Core/tests/integration/test_admin_routes.py` asserts both halves, because a
    401 alone does not prove a route did not authorise late, after writing.

THE TENANT COMES FROM THE CREDENTIAL, NEVER FROM THE BODY
    `TenantAdminScope.tenant_id` is set by the verifier. The request body cannot carry a
    tenant: the payload model forbids unknown fields, so a body naming one is a 422 rather
    than a value that is quietly ignored. Ignoring it would be nearly as bad - the caller
    believes they addressed tenant B, the write lands in tenant A, and nothing says so.

WHAT THIS FILE DOES NOT DO
    It does not version, authorise a collection, or write SQL: `KnowledgeAdmin` and its
    Postgres adapter own all three. This module authenticates, maps a payload onto a
    `KnowledgeDoc`, calls the port, and turns a refusal into a status code.

    It is not mounted into any app here, and deliberately not: `composition.py` and
    `main.py` belong to other anchors, and an admin surface that mounts itself is an admin
    surface that appears in a deployment nobody decided to give one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict

from agent_core.domain.knowledge import CollectionId, DocId, KnowledgeDoc
from agent_core.ports.knowledge_admin import KnowledgeAdmin, TenantAdminScope

__all__ = ["AdminAuthenticator", "create_admin_router"]

AdminAuthenticator = Callable[[str], TenantAdminScope | None]
"""Verify an opaque admin credential and return the scope it carries, or None.

The parameter is a bare `str` on purpose. Anything richer would be a shape a chat request
could be poured into; a token is an opaque string a deployment's identity provider issued,
and the only thing this module is allowed to know about it is whether the verifier
recognises it.
"""

_ADMIN_TOKEN_HEADER = "X-Admin-Token"


class _DocIn(BaseModel):
    """The write payload. NOTE WHAT IS ABSENT: there is no tenant seat.

    `extra="forbid"` is the enforcement. Without it, `tenant_id` in a body would be
    silently dropped and the caller would never learn that the write went somewhere else.
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    collection: str
    title: str
    body: str
    effective_from: datetime | None = None


class _DocOut(BaseModel):
    doc_id: str
    collection: str
    title: str
    body: str
    version: int
    effective_from: str | None
    superseded_by: str | None
    updated_by: str | None
    updated_at: str | None


class _ListOut(BaseModel):
    documents: list[_DocOut]


def _doc_out(doc: KnowledgeDoc) -> _DocOut:
    return _DocOut(
        doc_id=str(doc.doc_id),
        collection=str(doc.collection),
        title=doc.title,
        body=doc.body,
        version=doc.version,
        effective_from=None if doc.effective_from is None else doc.effective_from.isoformat(),
        superseded_by=None if doc.superseded_by is None else str(doc.superseded_by),
        updated_by=doc.updated_by,
        updated_at=None if doc.updated_at is None else doc.updated_at.isoformat(),
    )


def create_admin_router(
    *, knowledge_admin: KnowledgeAdmin, authenticate: AdminAuthenticator
) -> APIRouter:
    """The admin surface, wired to one `KnowledgeAdmin` and one credential verifier.

    Not mounted here - see the module docstring. A later anchor decides which deployments
    expose this router at all, and that decision must be visible where the app is
    assembled rather than made by importing a module.
    """
    router = APIRouter()

    def _scope(token: str | None) -> TenantAdminScope:
        """The ONLY place a scope enters this module, and it enters from a credential.

        Written as a closure over the injected verifier rather than as a module-level
        helper taking an identity, so there is no callable here that could be handed
        anything resembling a chat caller in the first place.
        """
        scope = None if not token else authenticate(token)
        if scope is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A verified administrator credential is required.",
                headers={"WWW-Authenticate": _ADMIN_TOKEN_HEADER},
            )
        return scope

    @router.post("/admin/knowledge/documents", response_model=_DocOut)
    async def upsert_document(
        payload: _DocIn,
        x_admin_token: Annotated[str | None, Header()] = None,
    ) -> _DocOut:
        scope = _scope(x_admin_token)
        doc = KnowledgeDoc(
            doc_id=DocId(payload.doc_id),
            collection=CollectionId(payload.collection),
            title=payload.title,
            body=payload.body,
            effective_from=payload.effective_from,
        )
        try:
            stored = await knowledge_admin.upsert(
                scope, doc, effective_from=payload.effective_from
            )
        except PermissionError as denied:
            raise _forbidden(denied) from denied
        return _doc_out(stored)

    @router.delete(
        "/admin/knowledge/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_document(
        doc_id: Annotated[str, Path()],
        x_admin_token: Annotated[str | None, Header()] = None,
    ) -> Response:
        scope = _scope(x_admin_token)
        try:
            await knowledge_admin.delete(scope, DocId(doc_id))
        except PermissionError as denied:
            raise _forbidden(denied) from denied
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/admin/knowledge/collections/{collection}/documents", response_model=_ListOut
    )
    async def list_documents(
        collection: Annotated[str, Path()],
        x_admin_token: Annotated[str | None, Header()] = None,
        include_superseded: Annotated[bool, Query()] = False,
    ) -> _ListOut:
        scope = _scope(x_admin_token)
        try:
            docs = await knowledge_admin.list_docs(
                scope, CollectionId(collection), include_superseded=include_superseded
            )
        except PermissionError as denied:
            raise _forbidden(denied) from denied
        return _ListOut(documents=[_doc_out(doc) for doc in docs])

    return router


def _forbidden(denied: PermissionError) -> HTTPException:
    """A grant this administrator does not hold is 403, not 500.

    The message is the port's, and it names a collection the caller already named. It never
    reports whether a document exists: `KnowledgeAdmin.delete` deliberately answers "not
    yours" and "not there" the same way, because telling them apart confirms the document
    exists, and that is itself a leak.
    """
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(denied))
