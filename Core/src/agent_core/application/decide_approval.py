"""Use case: DecideApproval - record a human's answer and wake the waiting turn.

Phase:   F3
Tasks:   docs/TASKS.md#t-f3-03
Status:  IMPLEMENTED (t-f3-03). Four-eyes (t-f3-05) still open - see STEP 2 below.

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

__all__ = ["DecideApproval", "DecisionSignal", "UnknownCorrelationError"]

# Waking the suspended turn: fire-and-return, nothing to await a result on. `note` travels
# with it because a REFUSAL's reason is what lets the model adapt instead of retrying -
# ResumeTurn builds the refusal result out of it.
DecisionSignal = Callable[[TurnId, ToolCallId, bool, str | None], Awaitable[None]]


class UnknownCorrelationError(LookupError):
    """The handle resolved to nothing: unknown, expired, or somebody else's.

    A subclass of `LookupError` so the HTTP adapter maps one exception type to 404 without
    knowing anything about correlation. It is an EXCEPTION here even though
    `HumanGateway.correlate` returns None, and the difference is deliberate: a miss is a
    normal value for the port, but for this use case there is no decision to record and
    nothing to wake, so continuing is never right. Guessing which turn a stray reply
    belongs to approves an action nobody approved, and it looks like success in the log.
    """


class DecideApproval:
    def __init__(
        self, *, gateway: HumanGateway, audit: AuditSink, signal: DecisionSignal
    ) -> None:
        self._gateway = gateway
        self._audit = audit
        self._signal = signal

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

        STEP 2 - AUTHORISE THE DECIDER (still open)
            The person replying must be allowed to decide THIS request. Do not assume the
            channel proves identity: a shared inbox, a forwarded message or a group chat
            all break that assumption.

            TODO(t-f3-05): decide whether the approver must differ from the requester
            (four-eyes). For the fraud vertical it almost certainly must. `subject_id` is
            recorded either way, so the answer is reconstructible from the audit trail even
            for decisions taken before the rule lands.

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

        # BEFORE the signal. See the docstring; these two statements must not swap.
        await self._audit.record_human_decision(
            turn_id, tool_call_id, subject_id, approved, note
        )
        await self._signal(turn_id, tool_call_id, approved, note)

        return turn_id, tool_call_id
