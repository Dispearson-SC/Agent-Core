"""Port: AgentMailbox - how does one agent ask another?

Phase:   F9 (foundations) / D2 (full A2A)
Tasks:   docs/TASKS.md#t-f9-02, docs/TASKS.md#t-f9-09
Adapter: adapters/driven/peers/
Per-vertical: NO

ASK AND READ-BACK ARE ONE QUESTION, NOT TWO
    `ask` returns a correlation id, so a port stopping there describes half a
    collaboration: whoever holds the handle has no typed way to redeem it. Both adapters
    grew `read_answer` regardless and the A2A test called it on the concrete class - which
    is the port cut leaking, because only a caller that already knows WHICH adapter it
    holds could read an answer. `read_answer` is therefore on the Protocol (t-f9-09).

    It is not a blocking receive. It redeems a handle the caller already has and returns
    None when the peer has not replied yet; the durable wait is still `DBOS.recv()` in the
    workflow.

NO NEW SUSPENSION MACHINERY - THIS IS THE THIRD USER OF THE SAME PATTERN
    `ask_peer(agent_id, question)` is an externally-executed deferred tool, exactly like
    `request_evidence` and exactly like an approval:

        the turn suspends -> something external answers -> DBOS.recv() wakes it

    And the answering agent may suspend its OWN turn on HumanGateway to ask a person. Two
    nested suspensions, one mechanism.

WHY IT IS NOT HumanGateway
    They share the suspension mechanism, which lives in the workflow, not in the port. But
    a human and an agent differ in trust level, latency expectation and authorisation.
    Merging them would be one port answering two questions.

A2A: SHAPE NOW, WIRE PROTOCOL IN D2
    Agent2Agent is at v1.0 under the Linux Foundation with backing from Google, Microsoft
    and Salesforce, though adoption is measured. Model the concepts now - discovery,
    tasks, modality - and adopt the wire format in D2 as an adapter swap.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.turn import SessionRef, TurnId


class AgentMailbox(Protocol):
    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        """Peers this profile may talk to, with their advertised capabilities.

        Capability claims are HINTS for routing, never authorisation. A peer advertising
        `calendar.read` does not thereby gain the right to read a calendar; its own policy
        decides that on its own side.
        """
        ...

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
        asker: AgentId | None = None,
        request_key: str | None = None,
    ) -> str:
        """PSEUDO-CODE - F9. Returns a correlation id; the ANSWER arrives via DBOS.recv().

        1. `policy.may_ask(target)` - empty peers means nobody. Checked on BOTH sides: the
           asker checks it may ask, the answerer checks it accepts. A one-sided check is
           bypassable by whoever controls the other side.

           `asker` IS THE ANSWERER'S HALF, AND IT IS WHY IT IS ON THIS SIGNATURE (t-f11-50)
           The answering side re-runs `callee_policy.may_ask(caller)` on arrival, and it
           cannot do that without being told who asked. Both adapters grew the keyword
           first (t-f11-48) - `PgAgentMailbox` persists it in
           `peer_messages.from_agent_id` and hands it back on the claimed `PeerAsk`,
           `A2AAgentMailbox` sends it as message metadata beside `hop` - but a caller
           typed on this port could not pass it, so every production row was written with
           no asker and the far side took the question on the asking side's word. A
           two-sided allowlist enforced on one side is a one-sided allowlist with extra
           words, and CLAUDE.md non-negotiable #10 is why the second lock is wanted: a
           peer that was itself misled is a confused deputy, and what it sends arrives
           with friendly provenance.

           OPTIONAL, AND None MEANS UNKNOWN - NEVER "ANYONE MAY ASK". Rows enqueued
           before the column existed carry no asker, and a deployment upgrades one
           process at a time, so a required parameter would turn a missing identity into
           a crash mid-upgrade instead of into the refusal it has to be. Nothing is
           invented to fill the gap and nothing is backfilled: the answering side that
           receives None has a caller it cannot verify, and that is the fact.
        2. `hop >= policy.max_hops` -> refuse. A asks B, B asks A, forever - and every hop
           is a full turn with model calls on both sides, so it is far more expensive than
           an ordinary loop.
        3. Redact per `policy.visibility`. Default NONE sends the question and nothing else.
           A customer-service agent must not be able to fish a person's calendar out of
           their assistant.
        4. Enqueue durably, return the correlation id.

           `request_key` MAKES THE ID DETERMINISTIC (C2). The caller passes what identifies
           this ask within the asking turn (the `ask_peer` tool call id); the correlation id
           is then derived from `(turn_id, request_key, target, question)` instead of drawn
           at random, so a DBOS replay of the asking step names the SAME row and can neither
           double-post nor double-run. Since C13 the workflow's key names the round and the
           position (`r<round>.s<position>.<tool call id>`), so the SAME question under another
           key (a later round) is a FRESH ask with its own answer. `None` keeps the previous
           behaviour (a random id, idempotent per `(turn_id, target, question)`).

        THE ANSWER IS UNTRUSTED CONTENT. Another agent is a third party - it may have read
        something hostile. Wrap it in delimiters like any `mcp_*` result. "Our own agent"
        is not a trust argument.
        """
        ...

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        """Deliver an answer back. The adapter signals the waiting workflow.

        `failed` (C1l) says the text is OUR fixed failure sentence for a callee that raised or was
        closed, not something the peer wrote. It is stored with the answer and read back by
        `answer_failed`; the asker's `<peer status>` comes from that flag, never from comparing the
        text, so a peer that writes the same sentence cannot pass as a failure.

        Idempotent per correlation id: a retried delivery must not resume a turn twice.

        THE CORRELATION ID IS AN ADDRESS, NOT A CREDENTIAL (C1c). It is derived from inputs
        (`peer_ask_id`), so anyone who knows them can compute it. Each ask therefore also has a
        random `answer_token`, minted when the ask is enqueued and handed ONLY to the party
        entitled to answer (the claimed `PeerAsk`, the A2A message metadata). A delivery that
        does not present it raises `PeerAnswerRefusedError` and writes nothing. So does a
        delivery for an ask that was cancelled (`cancel`, C4). A retry of an already answered ask is
        still a silent no-op, and an unknown id is too (strangers deliver).
        """
        ...

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        """The asker gives up on one ask: mark it `cancelled`. True if THIS call ended it.

        An open ask (queued or delivered, unanswered) becomes cancelled; an answered, already
        cancelled or unknown ask is left alone and False is returned. A cancelled ask is never
        claimed, never redelivered, and REFUSES a later `answer` (`PeerAnswerRefusedError`): the
        asker has moved on, so a late reply must not be written or wake anybody. Idempotent.
        `reason` is for the operator (a short fixed phrase, never the question or an answer).

        This is the row half of the cascade; cancelling the callee's running workflow is the
        caller's job (it is engine state, not mailbox state). Designed to be reused by any
        policy that abandons an ask (fan-out `fail_fast`, C10).
        """
        ...

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        """Cancel every open ask made by the asking turn `turn_id`; return their ids, sorted.

        For a turn that expires or is cancelled as a whole. Already answered or cancelled asks
        are not returned (they are history).
        """
        ...

    async def is_cancelled(self, correlation_id: str) -> bool:
        """Whether the ask ended `cancelled`. Lets a caller that was handed a handle it
        already gave up on (an adapter may return the same handle for a repeat; the shipped ones
        do not since C13) tell a dead ask from a live one without waiting for a reply that
        cannot come."""
        ...

    async def answer_failed(self, correlation_id: str) -> bool:
        """Whether the stored answer was recorded as a callee FAILURE (`answer(failed=True)`, C1l).

        False for an ordinary answer, an unanswered ask and an unknown id. The outcome is data on
        the row (or, over A2A, the task's own `failed` state), so no reader has to recognise our
        fixed sentences in text a peer could also have written.
        """
        ...

    async def redeliveries(self, correlation_id: str) -> int:
        """How many times the lease put this ask back on the queue: its current GENERATION.

        0 for an ask that was never redelivered and for an unknown id. The callee workflow of
        generation `n` has its own id (`turn_workflow.peer_workflow_id`), so a cancel that must
        reach whichever generation is live iterates `0..redeliveries` instead of probing for
        the next one and stopping at the first gap (D33). An implementation without a redelivery
        lease (the A2A client) answers 0.
        """
        ...

    async def read_answer(self, correlation_id: str) -> str | None:
        """The peer's answer WRAPPED as untrusted content, or None if it has not answered.

        The other half of `ask`. It never blocks: an unanswered handle returns None rather
        than waiting, because the durable wait is `DBOS.recv()` in the workflow and a wait
        here would silently lose the turn on the next deploy.

        THE WRAPPING IS THE CONTRACT, NOT THE METHOD. Every implementation returns the
        answer inside the same delimiters an `mcp_*` result gets - CLAUDE.md
        non-negotiable 10. A peer is a third party: it may have read a hostile page, been
        lied to by a person, or - over A2A - be an impersonated process on the far end of
        a socket. "It is our own agent" is not a trust argument. Returning the raw bytes
        and trusting a caller to wrap them is what this signature exists to prevent, so an
        implementation that hands back what the peer literally wrote does not satisfy this
        port however well its types line up. Any delimiter inside the peer's own text is
        neutralised before wrapping, or the boundary closes early and the rest of the
        peer's text reaches the model as instructions.

        The stored row keeps the peer's bytes verbatim; the wrapper is applied on read, so
        the audit trail never disagrees with what the peer actually said.
        """
        ...
