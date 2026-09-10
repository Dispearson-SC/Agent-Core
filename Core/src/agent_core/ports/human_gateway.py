"""Port: HumanGateway - who do I ask, and how do I wait for the answer?

Phase:      F3 (approvals) / F7 (evidence reuses it unchanged)
Tasks:      docs/TASKS.md#t-f3-01
Adapter:    adapters/driven/human/
Per-vertical: NO (the channel is configuration)

WHY IT IS NOT CALLED ApprovalGateway
    Pydantic AI's deferred tools cover TWO cases, not one: tools requiring approval, and
    tools executed EXTERNALLY. Both end the run and return DeferredToolRequests.

    So "ask the user for a photo of the package" needs no new machinery - it is an
    externally-executed tool walking the same path as an approval. One mechanism covers
    approvals, evidence requests, and any future mid-turn human interaction.

    The right port name describes the question it answers, not the first use case that
    motivated it. docs/DECISIONS.md#d9.

THE WAIT DOES NOT LIVE HERE
    This port PUBLISHES and it CORRELATES. It does not block. The durable wait is
    DBOS.recv() in adapters/driving/workflow/ - that is what survives a restart, a
    redeploy, and three days of a human not answering.

    If you ever find a sleep, a poll loop, or a thread join in an implementation of this
    port, it is wrong: it will silently lose the turn on the next deploy.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.turn import PendingRequest, SessionRef, TurnId


class HumanGateway(Protocol):
    def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        """PSEUDO-CODE - F3.

        1. Render each request for a human. For APPROVAL, the ask is
           `PolicyDecision.reason` plus the tool arguments; for EVIDENCE it is
           `PendingRequest.reason`.
        2. Deliver on the session's channel.
        3. Include a correlation handle carrying (turn_id, tool_call_id) so the reply can
           be routed back without the human having to quote anything.

        MUST be idempotent per (turn_id, tool_call_id). A DBOS step can be retried after a
        crash, and re-publishing means the human is asked the same question twice - which
        is how you get two conflicting approvals for one action.

        NEVER put raw tool arguments into the message without redaction. Arguments can
        contain credentials, tokens or personal data, and the channel is usually less
        trusted than the database.
        """
        ...

    def correlate(self, correlation_id: str) -> tuple[TurnId, str] | None:
        """Resolve an inbound human reply back to (turn_id, tool_call_id).

        Returns None for an unknown or expired handle. The HTTP adapter must treat None
        as 404 and MUST NOT guess: routing a stray reply into the wrong turn approves an
        action nobody approved.
        """
        ...
