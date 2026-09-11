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

    Nor does OUTBOUND DELIVERY of a finished `TurnResult` live here. That is a channel
    registry shared with the workflow's delivery step, not a port. docs/DECISIONS.md#d23.

WHY BOTH MEMBERS ARE ASYNC (D13)
    `publish` writes a correlation row and then hands a message to WhatsApp or Telegram
    over the network; `correlate` reads that row back on the inbound path. Both are I/O,
    and a sync `def` here would block the event loop for the whole channel round-trip -
    stalling every other turn in the process. Any `@DBOS.transaction` underneath stays
    synchronous and the adapter wraps it in `asyncio.to_thread`, exactly as in
    `ConversationStore`.

    Async is also what keeps the "no waiting" rule honest. A sync member that has to
    produce an answer has only one way to get one: block. An awaitable member is free to
    return immediately and let the workflow own the wait.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.turn import PendingRequest, SessionRef, ToolCallId, TurnId


class HumanGateway(Protocol):
    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        """PSEUDO-CODE - F3.

        ASYNC (D13): a correlation write followed by a channel send over the network.

        1. Render each request for a human: THAT something is pending, plus a `reason` -
           for APPROVAL, `PolicyDecision.reason`; for EVIDENCE, `PendingRequest.reason`.
           NEVER the tool name and NEVER its arguments, redacted or otherwise -
           non-negotiable #11's WHICH-tool half, not just argument-value redaction. See
           the only adapter's REDACTION note (adapters/driven/human/gateway.py) for why
           that is wider than it first looks: a tool's own NAME already tells a user
           which tool is pending, before a single argument value leaves the process.
        2. Deliver on the session's channel.
        3. Include a correlation handle carrying (turn_id, tool_call_id) so the reply can
           be routed back without the human having to quote anything.

        MUST be idempotent per (turn_id, tool_call_id). A DBOS step can be retried after a
        crash, and re-publishing means the human is asked the same question twice - which
        is how you get two conflicting approvals for one action.
        """
        ...

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        """Resolve an inbound human reply back to (turn_id, tool_call_id).

        ASYNC (D13): a lookup against the correlation table on the inbound request path.

        Returns None for an unknown or expired handle - that is the NORMAL case, not an
        error. Correlation ids expire and strangers POST at the decision route, so a miss
        is a value the caller must handle, never an exception it may forget to catch.

        The HTTP adapter must treat None as 404 and MUST NOT guess: routing a stray reply
        into the wrong turn approves an action nobody approved.

        The second element is the domain `ToolCallId`, not a bare `str`. It is the id
        Pydantic AI issued for the deferred call, and it is what `ResumeTurn` keys its
        idempotency on and what `AuditSink.record_human_decision` files the approval
        under. Handing back a loose string would push an unchecked cast into every
        consumer of this port.
        """
        ...
