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

        THE ANSWER IS UNTRUSTED CONTENT. Another agent is a third party - it may have read
        something hostile. Wrap it in delimiters like any `mcp_*` result. "Our own agent"
        is not a trust argument.
        """
        ...

    async def answer(self, correlation_id: str, answer: str) -> None:
        """Deliver an answer back. The adapter signals the waiting workflow.

        Idempotent per correlation id: a retried delivery must not resume a turn twice.
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
