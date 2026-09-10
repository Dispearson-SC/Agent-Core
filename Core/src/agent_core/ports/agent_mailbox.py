"""Port: AgentMailbox - how does one agent ask another?

Phase:   F9 (foundations) / D2 (full A2A)
Tasks:   docs/TASKS.md#t-f9-02
Adapter: adapters/driven/peers/
Per-vertical: NO

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
    ) -> str:
        """PSEUDO-CODE - F9. Returns a correlation id; the ANSWER arrives via DBOS.recv().

        1. `policy.may_ask(target)` - empty peers means nobody. Checked on BOTH sides: the
           asker checks it may ask, the answerer checks it accepts. A one-sided check is
           bypassable by whoever controls the other side.
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
