"""Driving adapter: FastAPI routes.

Phase:   F0 (two routes) / F2 (the coalescing enqueue) / F3 (decision route) / F7 (upload)
Tasks:   docs/TASKS.md#t-f0-03, docs/TASKS.md#t-f2-05, docs/TASKS.md#t-f0-06,
         docs/TASKS.md#t-f3-11, docs/TASKS.md#t-f7-08
Status:  POST /turns IMPLEMENTED (t-f0-03); the coalescing starter IMPLEMENTED (t-f2-05);
         GET /turns/{turn_id} IMPLEMENTED (t-f0-06); POST /decisions/{corr_id}
         IMPLEMENTED (t-f3-11); POST /evidence/{corr_id} IMPLEMENTED (t-f7-08).
Implements: nothing - it CALLS use cases

ROUTES
    POST /turns                  start a turn; returns turn_id immediately, does NOT block
    GET  /turns/{turn_id}        poll status                            (F0, t-f0-06)
    POST /decisions/{corr_id}    a human approves or refuses            (F3, t-f3-11)
    POST /evidence/{corr_id}     a human uploads a requested file       (F7)

THE ONE RULE THAT SHAPES ALL OF THEM
    A route NEVER waits for a turn. It starts the workflow and returns. The durable wait
    is DBOS.recv() inside the workflow.

    A route that blocks on an approval holds a connection for three days, dies on the next
    deploy, and takes the turn with it.

WHY GET EXISTS AT ALL, AND WHY IT IS NOT AN AFTERTHOUGHT
    The HTTP surface is 202-plus-poll (D23, docs/GAPS.md A4). `composition.py` registers
    the `http` channel as `PullModeChannel`, whose `send` does nothing DELIBERATELY -
    because the answer is retrieved here. Read that class's docstring before touching
    either half: with no reader, the empty `send` stops being a delivery mode and becomes
    a turn that is paid for, audited as successful, and reaches nobody.

ONE ID FROM THE 202 TO THE AUDIT TRAIL - docs/TASKS.md#t-f0-06
    The id `POST /turns` hands back is the id `GET /turns/{turn_id}` resolves, the id
    every audit row is filed under, and the id `DBOS.send_async` addresses when a human
    answers. That is now one value; `turn_workflow.py`'s ONE ID, NOT TWO says how, and
    which end owns it. It was two, joined by nothing, and every lookup across them
    returned empty forever without raising.

WHY THE STARTER IS INJECTED AND NOT IMPORTED
    `create_app` takes a `TurnStarter` rather than reaching for the DBOS workflow itself.
    Two reasons, and only the second one is about testing.

    The first is that this file must not be able to wait even by accident. A starter hands
    back a HANDLE - an id now, a result later, somebody else's problem - and the narrowed
    handle type below exposes nothing to await. There is no result here to block on.

    The second is that F2 owns `run_turn_workflow` and it does not exist yet. Importing it
    would tie F0's end-to-end proof to a phase that has not started; injecting it lets the
    workflow drop into the same seat the day it lands, with nothing in this file changing.

    F2 HAS NOW LANDED, and `coalescing_turn_starter` below imports the workflow module -
    but `create_app` still does not. The seat is unchanged and `create_app` still takes a
    `TurnStarter` it cannot await; what this file gained is one FACTORY that builds the
    production starter. The first reason above is the one that had to keep holding, and
    it does: the starter still hands back a handle carrying nothing but an id.

THE COALESCING STARTER - docs/TASKS.md#t-f2-05, D19 steps 1 to 3
    People split one thought across sends. Three sentences arriving within a second are
    one question, and answering them as three turns pays for three model calls to produce
    one useful answer - and answers the first two before the user has finished asking.

    So a message does not start a turn directly. It opens a coalescing WINDOW, and a
    message arriving while a window is open joins it. `turn_workflow.enqueue_turn_window`
    is the mechanism and docs/FIELD-NOTES.md explains why it needs a second queue; this
    file owns the half that decides what to do with the message that just joined.

    WHAT THIS FILE OWNS, AND THE ONE RULE IT MUST NOT GET WRONG
        A message that joined an open window is NOT in the enqueued turn's request:
        `duplication_policy="return-existing"` keeps the FIRST message's arguments and
        discards the later ones. So every message that coalesced - and ONLY those - is
        appended to the pending-input buffer, and the turn's first step
        (`_step_drain_pending`, docs/TASKS.md#t-f2-10) folds them back in behind the
        first sentence.

        Buffer nothing and the user's second and third sentences are gone with nothing
        raising. Buffer every message and the drain answers the first sentence twice.
        Neither shows up in a count of turns, which is why the rule is written down in
        both halves and asserted in tests/integration/test_coalescing.py.

THE EVIDENCE UPLOAD - docs/TASKS.md#t-f7-08
    `request_evidence` (t-f7-05) defers a tool call and mints an unguessable correlation
    id - the same generator `HumanGateway` uses for approvals, because a guessable one
    would let a stranger upload into someone else's turn. A human later POSTs the file
    here, at the id the ask carried.

    THIS ROUTE OWNS NEITHER VALIDATION NOR STORAGE. `IngestMedia` (t-f7-03, DONE) already
    enforces size, then sniff, then accept, then store - in that order, on purpose, and
    its own unit test pins the order by counting calls. Re-checking size here before
    calling it would be a second copy of a rule that already lives in one place; the
    route's only job is to hand the bytes to `IngestMedia` and translate its refusals.

    `resolve_evidence` IS THE SECOND SEAT, AND IT IS WHAT MAKES A HANDLE SINGLE-USE. It
    resolves a correlation id to the turn and profile the upload belongs to, or `None`
    for BOTH an unknown handle and one already resolved - collapsed into one answer for
    the same reason `HumanGateway.correlate` collapses unknown and expired
    (docs/TASKS.md#t-f3-04) and `DecideApproval` raises one `UnknownCorrelationError` for
    both (docs/TASKS.md#t-f3-03): telling a stranger POSTing a guessed or spent id
    "actually that one was already used" confirms a real id exists. Composition owns
    what stands behind this seat - a `HumanGateway` plus a profile lookup plus whatever
    marks a handle spent; this file only calls it and refuses on `None`.

    RESOLUTION BEFORE VALIDATION. The size/sniff/accept order inside `IngestMedia` is
    worthless if a stranger can reach it at all by guessing a handle - so `correlation_id`
    is resolved FIRST, and an unknown or spent one never reaches `IngestMedia`, exactly as
    an unauthenticated caller never reaches `TurnStarter` in `post_turns` above.

    AND IT WAKES THE TURN - docs/TASKS.md#t-f7-11. It did not, and said so here; a route
    whose contract ended at "the evidence is validated and stored" left the turn that
    asked for the file waiting three days for a file it had already been given. The send
    is a THIRD SEAT, `signal_evidence`, for the same reason `resolve_evidence` is a seat:
    `turn_workflow.signal_evidence` is keyed on the deferred call's `ToolCallId`, which is
    a fact of the correlation table, and this route reading that table itself would be a
    driving adapter growing a second opinion about it. The route hands over the handle,
    the turn and the stored reference; composition owns the rest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from fastapi import FastAPI, File, Header, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from agent_core.adapters.driving.workflow.turn_workflow import (
    DEFAULT_WINDOW_SECONDS,
    enqueue_turn_window,
)
from agent_core.application.decide_approval import (
    DecideApproval,
    FourEyesError,
    UnknownCorrelationError,
)
from agent_core.application.ingest_media import IngestMedia, MediaRejectedError, MediaTooLargeError
from agent_core.domain.media import MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)

__all__ = [
    "DecisionBody",
    "EvidenceCorrelate",
    "EvidenceSignal",
    "PendingInputSink",
    "StartTurnBody",
    "TurnHandle",
    "TurnLookup",
    "TurnStarter",
    "TurnStatus",
    "TurnView",
    "coalescing_turn_starter",
    "create_app",
]

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


class PendingInputSink(Protocol):
    """Where a message that joined an open window is kept until the turn drains it.

    Only `append`. The mirror of `turn_workflow.PendingInputs`, which declares only
    `drain`: this file must not be able to read the buffer and the workflow must not be
    able to write to it. One direction each, so neither side can quietly become the other
    - a route that drained would race the turn for the user's own sentences.

    `PgPendingInputBuffer` (docs/TASKS.md#t-f2-04) satisfies both.
    """

    async def append(self, session: SessionRef, message: UserInput) -> None: ...


@dataclass(frozen=True, slots=True)
class _CoalescedTurn:
    """The started turn, as much of it as a route may hold: an id, and nothing to await.

    THE ID IS THE TURN'S - docs/TASKS.md#t-f0-06
        It used to be the coalescing WINDOW's, because the turn did not exist yet: it is
        still delayed in DBOS's system table and will not be enqueued for another window's
        length. So a caller was handed a window id typed as a `TurnId`, while the audit
        rows, the correlation table and the wake-up all used a different value the
        workflow minted for itself. Nothing joined them and nothing raised.

        `enqueue_turn_window` now derives the two ids from each other, so the turn's id is
        known before the turn exists and comes back with the answer to "did I coalesce?".
        Three messages that coalesced still get the same id - which is exactly the fact a
        caller needs, "your message joined the answer already being prepared" - and it is
        now the id that answer will actually be filed under.
    """

    turn_id: TurnId


def coalescing_turn_starter(
    *, buffer: PendingInputSink, window_seconds: float = DEFAULT_WINDOW_SECONDS
) -> TurnStarter:
    """Build the production `TurnStarter`: one that coalesces instead of starting a turn.

    docs/TASKS.md#t-f2-05. See THE COALESCING STARTER in the module docstring for why a
    message is buffered if and only if it joined a window already open.

    THE ORDER OF THE TWO CALLS IS THE INTERESTING PART
        The enqueue comes FIRST, because until it returns nobody knows whether this
        message coalesced, and buffering a message that did not coalesce would repeat it
        - it is already travelling in the enqueued turn's own request.

        That leaves one window: a crash between the enqueue returning `coalesced` and the
        append committing loses that one sentence. The alternative - append first, delete
        when it turns out the window was fresh - trades it for a worse failure, a crash
        after the append leaving a sentence that the NEXT turn answers out of nowhere. A
        lost sentence is visible to the user, who repeats it; an invented one is not.
        Closing it properly needs the append and the enqueue in one transaction, which
        DBOS's system database and the application database do not share.

    A LATE COALESCE IS BUFFERED, NEVER DROPPED. A message can join a window in the
    moment between that window's delay expiring and its workflow finishing, and the turn
    behind it may already have drained. That sentence is not lost: it stays in the buffer
    and the next turn on the session folds it in. The exposure is the milliseconds the
    window workflow takes to enqueue and return, which is why it must never await the
    turn (turn_workflow.py's module docstring).
    """

    async def start(request: TurnRequest) -> TurnHandle:
        window = await enqueue_turn_window(request, window_seconds=window_seconds)
        if window.coalesced:
            await buffer.append(request.session, request.input)
        return _CoalescedTurn(turn_id=window.turn_id)

    return start


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


# What a poll can say about a turn. Three answers, not two, and the third one is the
# reason this is not just `TurnOutcome | None`:
#
#   running   started, nothing stored yet
#   waiting   suspended on a human or a peer
#   finished  the answer is here
#
# `TurnOutcome` cannot express "running": it is SUSPENDED or FINISHED by construction (I2),
# so a lookup that returned it would have to answer None for a turn that is merely working
# - indistinguishable from a turn id that does not exist. One of those is 200 and the other
# is 404, and collapsing them makes a live turn look like a typo.
TurnStatus = Literal["running", "waiting", "finished"]


@dataclass(frozen=True, slots=True)
class TurnView:
    """One turn as a poll may see it.

    `text` is None for anything that has not finished. A user must see THAT the turn is
    still going without being told what it is waiting on - the same rule
    `ports/transcript_reader.py` states for `PENDING_PLACEHOLDER`: hiding the suspension
    entirely makes the conversation look broken, and naming the pending tool tells a
    caller which lever to lean on.
    """

    turn_id: TurnId
    status: TurnStatus
    text: str | None = None


# Reading one turn back. Injected for the two reasons `TurnStarter` is: this file must not
# be able to reach a database, and the adapter that can does not exist in every process
# that builds an app.
#
# IT TAKES THE CALLER, and that is not decoration. A turn id names a conversation, so the
# tenant predicate belongs INSIDE the query the adapter runs - the rule
# `ports/transcript_reader.py` spells out. A lookup keyed on the id alone would let anyone
# holding an id read another tenant's answer, and ids leak: logs, screenshots, a shared
# support ticket. Unguessable is not the same as private.
TurnLookup = Callable[[TurnId, CallerIdentity], Awaitable[TurnView | None]]


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


class DecisionBody(BaseModel):
    """A human's answer. Two fields, and neither of them is who is answering.

    The approver is the AUTHENTICATED subject and never a body field, for the reason
    `StartTurnBody` gives about `subject_id`: D25's four-eyes rule compares subject ids,
    so a body-supplied approver lets the person who started the turn approve their own
    request by typing somebody else's name. The one control on the route would then be
    decoration.
    """

    approved: bool
    note: str | None = None


# Resolves a correlation handle to the turn and profile an evidence upload belongs to, or
# `None` for BOTH an unknown handle and one already resolved - see THE EVIDENCE UPLOAD in
# the module docstring for why the two must not be distinguishable from the outside.
# Composition owns what stands behind this: a `HumanGateway.correlate` call, a profile
# lookup by turn, and whatever marks the handle spent so a second POST is refused.
EvidenceCorrelate = Callable[[str], Awaitable[tuple[TurnId, AgentProfile] | None]]

# Wakes the turn that asked for the file, with the file. docs/TASKS.md#t-f7-11.
#
# Composition binds `turn_workflow.signal_evidence` behind this, and it is a SEAT rather
# than a direct call for the reason THE EVIDENCE UPLOAD gives about `resolve_evidence`:
# `signal_evidence` needs the `ToolCallId` of the deferred call, which is a fact of the
# correlation table and not of this request. The route hands over everything it holds -
# the handle the upload arrived at, the turn that handle resolved to, and the reference
# `IngestMedia` returned - and composition, which owns the correlation table, turns the
# handle into the deferred call. A route that resolved it itself would be a second reader
# of that table, in a driving adapter, disagreeing with the first the day either moves.
EvidenceSignal = Callable[[str, TurnId, MediaRef], Awaitable[None]]


def create_app(
    *,
    start_turn: TurnStarter,
    lookup_turn: TurnLookup | None = None,
    decide: DecideApproval | None = None,
    ingest_media: IngestMedia | None = None,
    resolve_evidence: EvidenceCorrelate | None = None,
    signal_evidence: EvidenceSignal | None = None,
) -> FastAPI:
    """The HTTP surface, wired to one way of starting a turn.

    `lookup_turn`, `decide`, `ingest_media` and `resolve_evidence` are OPTIONAL SEATS, and
    unwired they make their route refuse loudly rather than answer something plausible -
    the same choice `turn_workflow.py` makes for `human_gateway`. A `GET` that 404s
    because nothing is bound is indistinguishable from a turn that never existed, a
    `POST /decisions` that accepted an answer nothing recorded would tell a human their
    approval was taken, and a `POST /evidence` that accepted an upload nothing could
    validate would tell them their file was saved when it was not.

    `signal_evidence` IS THE ONE SEAT THAT DOES NOT 503, and the difference is which way
    the lie would run. The other three are asked BEFORE anything happens, so refusing
    costs nothing; this one is reached after `IngestMedia` has already validated and
    stored the file. A 503 there would tell a person their upload failed while it sits
    safely in the store, so the route answers 202 with `status: stored` instead of
    `accepted` - the upload is real, the turn was not told, and the response says so
    rather than reading like success. Wire it: an evidence request that is never resumed
    expires after three days in silence.
    """
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

    @app.get("/turns/{turn_id}")
    async def get_turn(
        turn_id: str,
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
    ) -> dict[str, str | None]:
        """The pull half of the 202 - docs/TASKS.md#t-f0-06.

        The id in the path is the one `POST /turns` handed back, which is the id the turn
        is filed under everywhere else. See ONE ID FROM THE 202 TO THE AUDIT TRAIL above:
        while those were two values this route answered 404 forever and looked exactly
        like a turn that was still running.

        THE CALLER GOES INTO THE LOOKUP, NOT JUST THE ID. `TurnLookup`'s own comment says
        why: a turn id names a conversation, ids leak, and unguessable is not private, so
        the tenant predicate has to be inside the query the adapter runs.

        404, NEVER A GUESS. An unknown id and another tenant's id are the same answer on
        purpose - distinguishing them would confirm that some other tenant holds it.
        """
        caller = _authenticate(x_subject_id, x_tenant_id, x_channel, x_roles)

        if lookup_turn is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "This process was built without a way to read a turn back, so polling "
                    "is unavailable. Answering 404 here would say the turn does not exist."
                ),
            )

        view = await lookup_turn(TurnId(turn_id), caller)
        if view is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No turn with that id in this tenant.",
            )

        return {"turn_id": str(view.turn_id), "status": view.status, "text": view.text}

    @app.post("/decisions/{correlation_id}", status_code=status.HTTP_202_ACCEPTED)
    async def post_decision(
        correlation_id: str,
        body: DecisionBody,
        x_subject_id: Annotated[str | None, Header()] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_channel: Annotated[str | None, Header()] = None,
        x_roles: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        """A human approves or refuses - docs/TASKS.md#t-f3-11. 202, and never a wait.

        `DecideApproval` records the decision and SIGNALS the waiting workflow; it does
        not resume it. So this route returns as soon as the send is durable, which is what
        THE ONE RULE THAT SHAPES ALL OF THEM demands: a route that awaited the resumed turn
        would hold this connection for the length of a model call, and a redeploy in the
        middle of it would take the turn with it.

        THE APPROVER IS THE AUTHENTICATED SUBJECT, never `body`. `DecisionBody` says why:
        D25's four-eyes rule compares subject ids, so a body-supplied approver would let
        the person who started the turn approve their own request by typing another name.

        RESOLVING THE SAME HANDLE TWICE IS A NO-OP AND IS NOT BUILT HERE. `DBOS.send` takes
        an `idempotency_key` (docs/FIELD-NOTES.md) and `turn_workflow.signal_decision` keys
        it on (turn_id, tool_call_id), so the second answer is discarded by the same
        database the durable wait lives in. A dedup cache in this process would be a third
        copy of that rule and a lie the moment there are two processes.

        THE TWO FAILURES ARE DIFFERENT ANSWERS, and the use case keeps them apart so this
        route does not have to know why: `LookupError` is 404 - the handle resolves to
        nothing, and guessing which turn a stray reply meant approves an action nobody
        approved. `PermissionError` is 403 - the handle is yours and the answer still is
        not. Collapsing them would tell an attacker holding a stray handle that it was real.
        """
        caller = _authenticate(x_subject_id, x_tenant_id, x_channel, x_roles)

        if decide is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "This process was built without a way to record a human decision. "
                    "Accepting one here would tell a human their approval was taken."
                ),
            )

        try:
            turn_id, tool_call_id = await decide.execute(
                correlation_id, caller.subject_id, body.approved, body.note
            )
        except UnknownCorrelationError as unknown:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(unknown)
            ) from unknown
        except FourEyesError as refused:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(refused)
            ) from refused

        return {
            "turn_id": str(turn_id),
            "tool_call_id": str(tool_call_id),
            "status": "accepted",
        }

    @app.post("/evidence/{correlation_id}", status_code=status.HTTP_202_ACCEPTED)
    async def post_evidence(
        correlation_id: str,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, str]:
        """A human uploads what `request_evidence` asked for - docs/TASKS.md#t-f7-08.

        See THE EVIDENCE UPLOAD in the module docstring for why `resolve_evidence` runs
        BEFORE `IngestMedia` and why an unknown handle and a spent one are the same 404.

        SIZE, THEN SNIFF, THEN ACCEPT, THEN STORE is entirely `IngestMedia`'s contract
        (t-f7-03); this route never inspects the bytes itself. It reads the upload into
        memory once - `IngestMedia.execute` takes `bytes`, not a stream - and everything
        after that is a call to the use case and a translation of its refusals.

        THEN IT WAKES THE TURN - docs/TASKS.md#t-f7-11, and it did not used to. A stored
        file that reaches nobody is the same failure as an answer with no channel: the
        turn sits in its durable wait until it expires three days later, the person who
        sent the photo was told it was accepted, and nothing raises anywhere. The send is
        `signal_evidence`, it is `DBOS.send` underneath, and it never waits for the turn -
        so this route still answers immediately, as THE ONE RULE THAT SHAPES ALL OF THEM
        requires.

        RE-UPLOADING IS A NO-OP AND IS NOT BUILT HERE, exactly as `post_decision` says of
        a repeated answer: `turn_workflow.signal_evidence` keys its send on
        (turn_id, tool_call_id), so a person who re-sends a file that looked slow resolves
        the same deferred call once, decided by the database the durable wait lives in.

        THE ANSWER SAYS WHICH OF THE TWO HAPPENED. `status` is `accepted` when the waiting
        turn was told and `stored` when this process has no `signal_evidence` bound - the
        upload IS safely stored either way, and an operator reading `stored` is reading a
        wiring gap rather than guessing at silence. It is not a 503: refusing after
        `IngestMedia` has already stored the bytes would tell a human their file was lost
        when it was not.
        """
        if ingest_media is None or resolve_evidence is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "This process was built without a way to accept evidence uploads."
                ),
            )

        # RESOLUTION FIRST. An unknown or already-resolved handle must never reach
        # IngestMedia - refusing after storing the bytes would have already paid for an
        # upload nobody was entitled to make.
        resolved = await resolve_evidence(correlation_id)
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No pending evidence request for this correlation handle.",
            )
        turn_id, profile = resolved

        data = await file.read()
        try:
            ref = await ingest_media.execute(
                turn_id,
                profile,
                data,
                declared_mime=file.content_type or "",
                filename=file.filename,
            )
        except MediaTooLargeError as too_large:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=str(too_large),
            ) from too_large
        except MediaRejectedError as rejected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(rejected)
            ) from rejected

        # t-f7-11. AFTER the store and never before it: signalling a turn with a
        # reference `IngestMedia` has not yet returned would resume it on a file that
        # does not exist, and a refusal after the send would leave the turn already
        # resumed on an upload this route went on to reject.
        if signal_evidence is None:
            return {
                "turn_id": str(turn_id),
                "media_id": str(ref.media_id),
                "status": "stored",
            }

        await signal_evidence(correlation_id, turn_id, ref)

        return {
            "turn_id": str(turn_id),
            "media_id": str(ref.media_id),
            "status": "accepted",
        }

    return app
