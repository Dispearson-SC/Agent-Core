"""Use case: DecideApproval - record a human's answer and wake the waiting turn.

Phase:   F3
Tasks:   docs/TASKS.md#t-f3-03
Status:  PSEUDO-CODE ONLY

WHERE THIS SITS
    Called from the HTTP adapter when a human replies - possibly days later, from a
    different device, in a different process, after several deploys.

    It does NOT resume the turn itself. It records the decision and SIGNALS the waiting
    workflow. The workflow, blocked in DBOS.recv(), wakes and calls ResumeTurn.

    That separation is what makes "approve tomorrow morning" work: the deciding request is
    a normal short HTTP call, and the long wait lives in durable storage rather than in a
    held connection.
"""

from __future__ import annotations

from agent_core.domain.turn import TurnId
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.human_gateway import HumanGateway


class DecideApproval:
    def __init__(self, *, gateway: HumanGateway, audit: AuditSink) -> None:
        self._gateway = gateway
        self._audit = audit

    def execute(
        self,
        correlation_id: str,
        subject_id: str,
        approved: bool,
        note: str | None = None,
    ) -> tuple[TurnId, str]:
        """PSEUDO-CODE - implement in F3. Returns (turn_id, tool_call_id).

        STEP 1 - CORRELATE
            resolved = self._gateway.correlate(correlation_id)
            if resolved is None: raise NotFound

            NEVER guess. An unknown or expired handle is a 404. Routing a stray reply into
            the wrong turn approves an action nobody approved - and it looks like a
            successful request in every log.

        STEP 2 - AUTHORISE THE DECIDER
            The person replying must be allowed to decide THIS request. Do not assume the
            channel proves identity: a shared inbox, a forwarded message or a group chat
            all break that assumption.

            TODO(F3): decide whether the approver must differ from the requester
            (four-eyes). For the fraud vertical it almost certainly must. Write it down
            here when it is settled.

        STEP 3 - RECORD FIRST, SIGNAL SECOND
            self._audit.record_human_decision(turn_id, tool_call_id, subject_id, approved, note)
            Then signal. If the signal fails, the decision is still on record and can be
            replayed. Signalling first risks the tool running with no record of who
            authorised it - the exact question an auditor will ask.

        STEP 4 - RETURN THE PAIR
            The HTTP adapter passes it to DBOS.send() so the workflow wakes.

        IDEMPOTENCY
            The same correlation handle answered twice must NOT produce two signals.
            Second call returns the stored decision unchanged. Humans double-click; retry
            logic re-posts. Neither should approve an action twice.
        """
        raise NotImplementedError("F3 - docs/TASKS.md#t-f3-03")
