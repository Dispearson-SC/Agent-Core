"""Driven adapter: AgentMailbox over the A2A wire protocol (D2).

Phase:   D2 (full A2A wire protocol)
Tasks:   docs/TASKS.md#t-d2-04
Status:  DONE
Tests:   Core/tests/integration/test_mailbox_a2a.py
Implements: ports/agent_mailbox.py

THIS ADAPTER REPLACES PgAgentMailbox BEHIND THE PORT, NOT ALONGSIDE IT
    `t-f9-03`'s `PgAgentMailbox` stays for deployments that talk to peers through the
    durable Postgres queue. This module is the OTHER implementation of the same
    `ports/agent_mailbox.py` Protocol (FROZEN): swapping one for the other in
    `composition.py` is the entire change. See
    `test_the_a2a_adapter_has_the_same_method_shape_as_the_postgres_one` for the contract
    that makes that true - same method names, same call shapes, on both classes.

WHY THIS ONE HAS NO TABLE
    `PgAgentMailbox` needs a table because ITS transport (a shared Postgres row) has no
    other way to survive a restart. Over A2A the durable half of "ask a peer" is not this
    adapter's job at all: the wire call is made from inside a DBOS step exactly like the
    Postgres path, and step re-execution after a crash is DBOS's job, not this module's.
    What this adapter owns is turning one `ask()` into one real HTTP request and giving
    back a handle; `_answers` is bookkeeping for that one wire round trip's lifetime, not a
    queue. A synchronous peer (the common case: fast task, inline artifact) never touches
    it - the answer is already in the HTTP response.

DISCOVER REFRESHES FROM THE WIRE, THE POSTGRES ADAPTER DOES NOT
    `PgAgentMailbox.discover` returns `policy.peers` verbatim - a configured allowlist has
    nothing else to consult. Over A2A the peer publishes its own agent card at a
    well-known path, and that is the live source of truth for what it currently claims it
    can do. A peer with no configured `endpoint` cannot be asked over the wire at all, so
    its ref is returned unchanged rather than guessed at.

`read_answer` IS THE PORT'S, NOT THIS CLASS'S (t-f9-09)
    It used to be an extra on this class and an extra on `PgAgentMailbox`, which is why
    `test_mailbox_a2a.py` calls it on the concrete type: nothing typed on the Protocol
    could. `ports/agent_mailbox.py` now declares it, so the swap this module's opening
    paragraph describes covers the read-back too - and the port, not this file, is what
    requires the answer to come back wrapped.

NON-NEGOTIABLE #10, SHARPENED BY THE WIRE
    Over Postgres the peer's answer was already untrusted content, but the bytes came from
    a table this process also writes to. Here they arrive from a socket a compromised or
    impersonated peer controls end to end, so `read_answer` reuses `wrap_peer_answer` from
    the Postgres adapter rather than re-deriving the wrapping - CLAUDE.md's own reasoning
    for exporting it: two implementations of one boundary is how a boundary stops being
    one, and that is doubly true across a process boundary this adapter did not write.

WHAT `hop`, `turn_id` AND `from_session` DO HERE
    `PgAgentMailbox` persists them because a claimed row must find its way back to the
    asking turn later, on a completely separate connection. A2A's task lives inside one
    request/response pair the caller already holds, so nothing here needs to look them up
    independently - they travel as A2A message metadata instead, so a compliant peer (or a
    future receiving-side adapter for this same wire protocol) can see the hop count and
    enforce its own side of the allowlist, per `ports/agent_mailbox.py`'s "checked on both
    sides" note. As on the Postgres side, `policy.may_ask` and `policy.max_hops` are NOT
    enforced here - that is `docs/TASKS.md#t-f9-05`, on both adapters equally.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.turn import SessionRef, TurnId

_AGENT_CARD_PATH: Final[str] = "/.well-known/agent-card.json"


class A2AAgentMailbox:
    """A2A wire-protocol adapter for `ports.agent_mailbox.AgentMailbox`.

    D13: every member is async; `httpx.AsyncClient` is native async I/O, so unlike the
    Postgres adapter there is no synchronous driver to push into `asyncio.to_thread`.
    """

    __slots__ = ("_client", "_answers")

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        # Per-instance bookkeeping for one wire round trip's lifetime - see the module
        # docstring for why this is not a queue. Keyed by correlation id (the A2A task
        # id), value is the peer's answer VERBATIM: wrapping happens on read, exactly as
        # in PgAgentMailbox, so the stored text and an audit trail of it always agree.
        self._answers: dict[str, str] = {}

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        """Refresh `policy.peers` from each reachable peer's live agent card.

        A peer with no configured `endpoint` cannot be reached over A2A at all, so it is
        passed through unchanged rather than guessed at.
        """
        refreshed: list[AgentRef] = []
        for peer in policy.peers:
            if peer.endpoint is None:
                refreshed.append(peer)
                continue
            response = await self._client.get(peer.endpoint + _AGENT_CARD_PATH)
            response.raise_for_status()
            card: dict[str, Any] = response.json()
            skills: list[dict[str, Any]] = card.get("skills", [])
            refreshed.append(
                AgentRef(
                    agent_id=peer.agent_id,
                    display_name=str(card.get("name", peer.display_name)),
                    capabilities=tuple(str(skill["id"]) for skill in skills),
                    endpoint=peer.endpoint,
                )
            )
        return tuple(refreshed)

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
        """Send one A2A `message/send` request and return the task id as correlation id.

        A task that completes inline (an artifact in the same HTTP response - a real,
        valid A2A shape for a fast peer) has its answer recorded immediately, so
        `read_answer` can return it with no further wire traffic. A task reported
        `submitted` records nothing yet; `answer()` is how a later delivery arrives.
        """
        endpoint = _endpoint_for(policy, target)
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": question}],
                    # Not enforced here (see module docstring) - carried so a compliant
                    # peer, or a future receiving-side A2A adapter, can enforce its own
                    # side of the allowlist and the hop limit.
                    "metadata": {
                        "from_session_id": str(from_session.session_id),
                        "from_tenant_id": str(from_session.tenant_id),
                        "turn_id": str(turn_id),
                        "hop": hop,
                    },
                }
            },
        }
        response = await self._client.post(endpoint, json=payload)
        response.raise_for_status()
        envelope: dict[str, Any] = response.json()
        result: dict[str, Any] = envelope["result"]
        correlation_id = str(result["id"])
        if result["status"]["state"] == "completed":
            artifacts: list[dict[str, Any]] = result.get("artifacts", [])
            if artifacts:
                self._answers[correlation_id] = str(artifacts[0]["parts"][0]["text"])
        return correlation_id

    async def answer(self, correlation_id: str, answer: str) -> None:
        """Record a delivered answer against its correlation id. Idempotent.

        `setdefault` makes the first delivery win, exactly like `PgAgentMailbox.answer`'s
        `answered_at IS NULL` guard: a retried delivery for one correlation id must not
        change what a suspended turn resumes with.
        """
        self._answers.setdefault(correlation_id, answer)

    async def read_answer(self, correlation_id: str) -> str | None:
        """The peer's answer, WRAPPED as untrusted content, or None if none has arrived.

        `ports.agent_mailbox.AgentMailbox.read_answer` - the other half of `ask`, and the
        port is what requires the wrapping (t-f9-09).

        Never returns the raw bytes - see CLAUDE.md non-negotiable #10 and the module
        docstring on reusing `wrap_peer_answer` rather than re-deriving the boundary.
        """
        raw = self._answers.get(correlation_id)
        if raw is None:
            return None
        return wrap_peer_answer(raw)


def _endpoint_for(policy: PeerPolicy, target: AgentId) -> str:
    """The configured endpoint for `target`, or raise if none is reachable.

    Mirrors `PgAgentMailbox`'s reliance on `policy.peers` as the sole source of routing
    truth - there is no separate registry to fall back to.
    """
    for peer in policy.peers:
        if peer.agent_id == target and peer.endpoint is not None:
            return peer.endpoint
    raise ValueError(f"no reachable A2A endpoint configured for peer {target!r}")
