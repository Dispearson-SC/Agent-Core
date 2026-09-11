"""Use case: DecideApproval - record a human's answer and wake the waiting turn.

Phase:   F3
Tasks:   docs/TASKS.md#t-f3-03, docs/TASKS.md#t-f3-05, docs/TASKS.md#t-f3-14
Status:  IMPLEMENTED (t-f3-03). Four-eyes DECIDED AND ENFORCED (t-f3-05,
         docs/DECISIONS.md#d25) - see STEP 2 below - and a refused attempt now leaves a
         row through `AuditSink.record_rejected_decision` (t-f3-14), which is the gap
         D25 recorded and left open.

WHERE THIS SITS
    Called from the HTTP adapter when a human replies - possibly days later, from a
    different device, in a different process, after several deploys.

    It does NOT resume the turn itself. It records the decision and SIGNALS the waiting
    workflow. The workflow, blocked in DBOS.recv(), wakes and calls ResumeTurn.

    That separation is what makes "approve tomorrow morning" work: the deciding request is
    a normal short HTTP call, and the long wait lives in durable storage rather than in a
    held connection.

WHY THE SIGNAL IS INJECTED AND NOT IMPORTED
    `DBOS.send_async` is the production wake-up, and `dbos` is banned outside
    adapters/driving/workflow/ - so the use case takes a `DecisionSignal` callable and the
    composition root binds it. Same arrangement, and the same two reasons, as `TurnStarter`
    in adapters/driving/http/routes.py: the seat exists before F2's workflow fills it, and
    the narrow type gives this file nothing it could wait on.

    Injecting it rather than returning the pair and letting the caller send is what makes
    the ordering below a property of the USE CASE instead of a convention the next adapter
    may or may not follow. The pair is still returned, because the route needs it too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agent_core.domain.turn import ToolCallId, TurnId
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.human_gateway import HumanGateway

__all__ = [
    "SELF_APPROVAL",
    "UNKNOWN_REQUESTER",
    "DecideApproval",
    "DecisionSignal",
    "FourEyesError",
    "RequesterLookup",
    "UnknownCorrelationError",
]

# Waking the suspended turn: fire-and-return, nothing to await a result on. `note` travels
# with it because a REFUSAL's reason is what lets the model adapt instead of retrying -
# ResumeTurn builds the refusal result out of it.
DecisionSignal = Callable[[TurnId, ToolCallId, bool, str | None], Awaitable[None]]


# Who STARTED the turn, as a `subject_id`. Injected, not imported, for exactly the two
# reasons `DecisionSignal` above is: `application/` may not reach for a repository, and a
# narrow callable gives this file nothing it could wait on. Returns None when nobody is on
# record for that turn - which D25 treats as a refusal, not as a pass.
RequesterLookup = Callable[[TurnId], Awaitable[str | None]]


class FourEyesError(PermissionError):
    """The approver could not be shown to be a second person. docs/DECISIONS.md#d25.

    Covers both halves of that failure - the approver IS the requester, and the requester
    is unknown - because they are the same outcome: nothing here can demonstrate two
    people, and an approval that cannot be demonstrated is not one.

    A subclass of `PermissionError` so the HTTP adapter maps one exception type to 403
    without knowing anything about four-eyes, mirroring `UnknownCorrelationError` being a
    `LookupError` for 404. The two must stay distinguishable: 404 says the handle is not
    yours, 403 says the handle is yours and the answer still is not.
    """


class UnknownCorrelationError(LookupError):
    """The handle resolved to nothing: unknown, expired, or somebody else's.

    A subclass of `LookupError` so the HTTP adapter maps one exception type to 404 without
    knowing anything about correlation. It is an EXCEPTION here even though
    `HumanGateway.correlate` returns None, and the difference is deliberate: a miss is a
    normal value for the port, but for this use case there is no decision to record and
    nothing to wake, so continuing is never right. Guessing which turn a stray reply
    belongs to approves an action nobody approved, and it looks like success in the log.
    """


# The grounds a refusal is filed under, and the message `FourEyesError` carries. They are
# constants because they are read twice: once by the person the exception reaches now, and
# once by whoever queries `audit_rejected_decisions` months later asking WHICH control
# fired. One shared string would answer the second question with "four-eyes, somehow".
#
# They name the rule and nothing else. No note, no arguments, no correlation handle: an
# audit row is grounds, not a place to spill the payload the attempt carried.
SELF_APPROVAL = (
    "four-eyes: an approval must come from someone other than the human who started "
    "this turn"
)
UNKNOWN_REQUESTER = (
    "four-eyes: nobody is on record as having started this turn, so the approver cannot "
    "be shown to be a second person"
)


def _refusal_grounds(requester_id: str | None, approver_id: str) -> str | None:
    """Why this approval is refused, or None when it is not. docs/DECISIONS.md#d25.

    Both halves of `FourEyesError` reach the same outcome - nothing here can demonstrate
    two people - but they are not the same event, and the trail has to keep them apart.
    """
    if requester_id is None:
        return UNKNOWN_REQUESTER
    if _same_person(requester_id, approver_id):
        return SELF_APPROVAL
    return None


def _same_person(requester_id: str, approver_id: str) -> bool:
    """Compare on `subject_id` alone, stripped and casefolded. docs/DECISIONS.md#d25.

    On `subject_id` ALONE - never on the channel or the roles. The same person answering
    from WhatsApp instead of the web is still the same person, and matching on a tuple
    would let a channel switch defeat the rule.

    Stripped and casefolded because the two mistakes are not symmetric: an exact-match
    rule is defeated by whichever spelling of their own id the requester can persuade a
    channel to send, while over-matching costs one identity provider that issues subjects
    differing only by case - which is a defect there, not a decision here.
    """
    return requester_id.strip().casefold() == approver_id.strip().casefold()


class DecideApproval:
    def __init__(
        self,
        *,
        gateway: HumanGateway,
        audit: AuditSink,
        signal: DecisionSignal,
        requester: RequesterLookup | None = None,
    ) -> None:
        self._gateway = gateway
        self._audit = audit
        self._signal = signal
        self._requester = requester

    async def execute(
        self,
        correlation_id: str,
        subject_id: str,
        approved: bool,
        note: str | None = None,
    ) -> tuple[TurnId, ToolCallId]:
        """Record the human's answer, then wake the turn. Returns (turn_id, tool_call_id).

        ASYNC (D13): a correlation lookup and an audit insert, both I/O.

        THE ORDERING IS THE POINT, AND IT IS NOT A STYLE CHOICE
            Record, THEN signal. It is the same rule, for the same reason, as the DENY path
            in `PolicyEnforcement.before_tool_execute`, which writes its audit row before it
            raises. Signal first and the workflow can resume - and the tool can run - with
            no record of who authorised it. A decision that vanishes because a notification
            failed is a decision nobody can prove was made, and CLAUDE.md non-negotiable #6
            says the sink is append-only and that a failed turn must still leave a trace.

            So a failing signal PROPAGATES and the row stays. The caller sees the failure
            and can retry, which is the honest answer: the decision is on record but the
            turn has not resumed yet, and reporting success would leave the workflow asleep
            with nobody looking for it. Nothing rolls the row back - the sink is
            append-only and writes outside the domain transaction by design.

        STEP 2 - FOUR-EYES (t-f3-05, docs/DECISIONS.md#d25)
            The person replying must be allowed to decide THIS request. Do not assume the
            channel proves identity: a shared inbox, a forwarded message or a group chat
            all break that assumption.

            The rule, decided: an APPROVAL must come from someone other than the human who
            started the turn. "Requester" is that human - `TurnRequest.caller.subject_id` -
            and not the agent, because the agent is not a person and a rule comparing a
            human to a piece of software can never fire.

            It gates `approved=True` only. Refusing your own request removes a permission
            rather than conferring one, and blocking it would leave the turn asleep with
            the one person who wants it stopped unable to stop it.

            An unknown requester is a REFUSAL, not a pass. The approver may well be a
            second person; nothing here can show it, and "probably fine" is how a control
            becomes decoration.

            The check runs before `record_human_decision`, and that member is not what a
            refusal is filed through: it means "a human decided", so a refused attempt
            recorded there would read in the trail as a refusal the human never made.
            The attempt gets `record_rejected_decision` instead (t-f3-14) - its own
            member over its own table, carrying no verdict, written BEFORE the exception
            propagates. That makes this path the same shape as the DENY path in
            `PolicyEnforcement.before_tool_execute` after all: row first, then raise.

            D25 recorded the silence as a gap, and it is now closed. A control nobody can
            show fired is indistinguishable from one that was never wired, and the row is
            append-only and outside the domain transaction (CLAUDE.md non-negotiable #6),
            so the rollback of the turn it refused cannot take it.

        IDEMPOTENCY IS NOT BUILT HERE, AND THAT IS DELIBERATE
            The same handle answered twice must not resume the turn twice - humans
            double-click and retry logic re-posts. Both halves of that already exist:
            `DBOS.send` takes an `idempotency_key` (docs/FIELD-NOTES.md), which the signal
            adapter keys on (turn_id, tool_call_id), and `ResumeTurn` treats an
            already-resolved `tool_call_id` as a no-op. A dedup cache here would be a third
            implementation of the same rule, in memory, in one process - i.e. a lie the
            moment there are two.

            The second audit row a repeat produces is not a defect. Two attempts happened
            and an append-only trail says so; collapsing them would hide a double-click
            from the only reader who might care that it occurred.
        """
        resolved = await self._gateway.correlate(correlation_id)
        if resolved is None:
            raise UnknownCorrelationError(
                "no pending human decision for this correlation handle"
            )
        turn_id, tool_call_id = resolved

        # Four-eyes (D25). After correlation, so a stray reply never becomes a lookup
        # against a turn nobody resolved; before `record_human_decision`, for the reason
        # in STEP 2 - and it writes `record_rejected_decision` instead.
        if approved and self._requester is not None:
            grounds = _refusal_grounds(await self._requester(turn_id), subject_id)
            if grounds is not None:
                # The row FIRST, then the refusal - the same ordering, for the same
                # reason, as `record` before `signal` below. A refusal that propagates
                # before its row is written is a control nobody can show fired.
                await self._audit.record_rejected_decision(
                    turn_id, tool_call_id, subject_id, grounds
                )
                raise FourEyesError(grounds)

        # BEFORE the signal. See the docstring; these two statements must not swap.
        await self._audit.record_human_decision(
            turn_id, tool_call_id, subject_id, approved, note
        )
        await self._signal(turn_id, tool_call_id, approved, note)

        return turn_id, tool_call_id
