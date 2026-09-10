"""Driving adapter: FastAPI routes.

Phase:   F0 (one route) / F3 (decision route) / F7 (upload route)
Tasks:   docs/TASKS.md#t-f0-03
Status:  POST /turns IMPLEMENTED (t-f0-03) - the other three routes are still PSEUDO-CODE
Implements: nothing - it CALLS use cases

ROUTES
    POST /turns                  start a turn; returns turn_id immediately, does NOT block
    GET  /turns/{turn_id}        poll status
    POST /decisions/{corr_id}    a human approves or refuses            (F3)
    POST /evidence/{corr_id}     a human uploads a requested file       (F7)

THE ONE RULE THAT SHAPES ALL OF THEM
    A route NEVER waits for a turn. It starts the workflow and returns. The durable wait
    is DBOS.recv() inside the workflow.

    A route that blocks on an approval holds a connection for three days, dies on the next
    deploy, and takes the turn with it.

PSEUDO-CODE - POST /turns
    1. Authenticate -> CallerIdentity. This is the input to every policy decision, so
       getting identity wrong here silently mis-authorises everything downstream.
    2. Build TurnRequest.
    3. Start the DBOS workflow (async handle).
    4. Return 202 with turn_id.

PSEUDO-CODE - POST /decisions/{corr_id}
    1. DecideApproval.execute(...) -> (turn_id, tool_call_id)
    2. DBOS.send(turn_id, payload) to wake the waiting workflow.
    3. Unknown or expired handle -> 404. NEVER guess: routing a stray reply into the
       wrong turn approves an action nobody approved, and it looks like success in the log.

WHY THE STARTER IS INJECTED AND NOT IMPORTED
    `create_app` takes a `TurnStarter` rather than reaching for the DBOS workflow itself.
    Two reasons, and only the second one is about testing.

    The first is that this file must not be able to wait even by accident. A starter hands
    back a HANDLE - an id now, a result later, somebody else's problem - and the narrowed
    handle type below exposes nothing to await. There is no result here to block on.

    The second is that F2 owns `run_turn_workflow` and it does not exist yet. Importing it
    would tie F0's end-to-end proof to a phase that has not started; injecting it lets the
    workflow drop into the same seat the day it lands, with nothing in this file changing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)

__all__ = ["StartTurnBody", "TurnHandle", "TurnStarter", "create_app"]

_DEFAULT_CHANNEL = "http"


class TurnHandle(Protocol):
    """A started turn: the id, and NOTHING this adapter could wait on.

    DBOS's own workflow handle is wider than this on purpose - the workflow needs to wait,
    a route must not. Narrowing it here means a route cannot reach a result by accident:
    the attribute is not on the type it holds.
    """

    @property
    def turn_id(self) -> TurnId: ...


# Starting is the only thing a route awaits, and it returns as soon as the workflow is
# enqueued. `run_turn_workflow` (F2) is what fills this seat in production.
TurnStarter = Callable[[TurnRequest], Awaitable[TurnHandle]]


class StartTurnBody(BaseModel):
    """What the caller may say about the turn - deliberately not much.

    Unknown fields are IGNORED rather than rejected, and that matters most for the fields
    a caller would love to send: `subject_id`, `tenant_id`, `roles`. They have no effect
    here whatsoever. Identity comes from the authenticated request and from nowhere else -
    a caller who can name their own subject can name somebody else's, and every policy
    decision downstream would believe them.
    """

    session_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    text: str = ""


def _authenticate(
    subject_id: str | None, tenant_id: str | None, channel: str | None, roles: str | None
) -> CallerIdentity:
    """Build the identity, or refuse the request. There is no third answer.

    Day 1 trusts headers set by the edge proxy. Whatever replaces that - a signed token,
    mTLS - replaces the body of this function and nothing else, which is why it is a
    function rather than four lines inside the route.

    A missing subject or tenant is 401, never a default. `CallerIdentity` is the input to
    every `ToolPolicy` decision, so an anonymous fallback does not fail: it succeeds,
    quietly, carrying somebody else's permissions.
    """
    if not subject_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An authenticated subject and tenant are required to start a turn.",
        )

    return CallerIdentity(
        subject_id=subject_id,
        channel=channel or _DEFAULT_CHANNEL,
        tenant_id=TenantId(tenant_id),
        roles=frozenset(part.strip() for part in (roles or "").split(",") if part.strip()),
    )


def create_app(*, start_turn: TurnStarter) -> FastAPI:
    """The HTTP surface, wired to one way of starting a turn."""
    app = FastAPI(title="agent-core")

    @app.post("/turns", status_code=status.HTTP_202_ACCEPTED)
    async def post_turns(
        body: StartTurnBody,
        response: Response,
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        """202 and a turn_id. Never a result, never a wait.

        The tenant on the session is the AUTHENTICATED tenant, never one the body asked
        for: a session id is guessable, and a body-supplied tenant would let a caller
        append their turn to another tenant's conversation.
        """
        # Identity first, before anything downstream is touched. An unidentified request
        # must leave no half-started turn behind for an operator to puzzle over.
        caller = _authenticate(x_subject_id, x_tenant_id, x_channel, x_roles)

        request = TurnRequest(
            session=SessionRef(
                session_id=SessionId(body.session_id), tenant_id=caller.tenant_id
            ),
            caller=caller,
            profile_id=body.profile_id,
            input=UserInput(text=body.text),
        )

        # The whole route in one line: start it, then let go. Awaiting anything of the
        # turn here is the bug the 202 exists to prevent.
        handle = await start_turn(request)

        response.headers["Location"] = f"/turns/{handle.turn_id}"
        return {"turn_id": str(handle.turn_id), "status": "accepted"}

    return app
