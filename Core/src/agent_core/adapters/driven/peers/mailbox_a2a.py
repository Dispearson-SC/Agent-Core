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

WHO ASKED TRAVELS AS METADATA, BECAUSE THERE IS NO ROW TO READ IT OFF (t-f11-48)
    `PgAgentMailbox` persists the asking agent's id in a column and hands it back on the
    claimed `PeerAsk`. Over the wire there is no claimed row at all, so the identity
    travels in the same place `hop` does: the A2A message metadata, as `from_agent_id`.
    That is what lets a compliant peer - or a future receiving-side adapter for this same
    protocol - re-run `callee_policy.may_ask(caller)` instead of accepting a question
    because the far end said it was allowed.

    One adapter carrying the caller identity and the other dropping it would make the
    check depend on which transport a deployment chose, which is the port cut leaking
    (docs/WAVES.md rule 4). As with `hop`, this adapter CARRIES and does not enforce.

    A receiving side must trust this metadata exactly as far as it trusts the socket it
    arrived on - `hop_limit.py` says the same about the hop count, and whoever terminates
    the wire owns authenticating both.

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

import logging
import secrets
import uuid
from typing import Any, Final

import httpx

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.domain.peers import (
    AgentId,
    AgentRef,
    PeerAnswerRefusedError,
    PeerContinuationRefusedError,
    PeerPolicy,
    new_answer_token,
    peer_ask_id,
)
from agent_core.domain.turn import SessionRef, TurnId

_AGENT_CARD_PATH: Final[str] = "/.well-known/agent-card.json"

_LOG = logging.getLogger(__name__)

# A2A task states that end a task without a result. Both are the callee's failure to answer.
_FAILED_STATES: Final[frozenset[str]] = frozenset({"failed", "rejected"})
_FAILED_TASK_TEXT: Final[str] = "The peer reported that the task failed."


def _status_text(result: dict[str, Any]) -> str:
    """The first text part of the task's status message, or "" when it sent none."""
    message = result["status"].get("message")
    if not isinstance(message, dict):
        return ""
    for part in message.get("parts", []):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return str(part["text"])
    return ""


class A2AAgentMailbox:
    """A2A wire-protocol adapter for `ports.agent_mailbox.AgentMailbox`.

    D13: every member is async; `httpx.AsyncClient` is native async I/O, so unlike the
    Postgres adapter there is no synchronous driver to push into `asyncio.to_thread`.
    """

    __slots__ = ("_answers", "_cancelled", "_client", "_failed", "_tokens", "_turns")

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        # Per-instance bookkeeping for one wire round trip's lifetime - see the module
        # docstring for why this is not a queue. Keyed by correlation id (the A2A task
        # id), value is the peer's answer VERBATIM: wrapping happens on read, exactly as
        # in PgAgentMailbox, so the stored text and an audit trail of it always agree.
        self._answers: dict[str, str] = {}
        # C1c. correlation id -> the secret sent to the peer in the message metadata. A later
        # `answer` delivery must present it; compared in constant time.
        self._tokens: dict[str, str] = {}
        # C4. Local bookkeeping: which asking turn made each task, and which were cancelled.
        # The A2A wire has `tasks/cancel`; sending it needs the endpoint and is D2's. Until
        # then a cancelled ask refuses a late `answer` here and the peer is not told.
        self._turns: dict[str, str] = {}
        self._cancelled: set[str] = set()
        # C1l. Correlation ids whose delivered answer was recorded as a callee failure.
        self._failed: set[str] = set()

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
        asker: AgentId | None = None,
        request_key: str | None = None,
        continue_session: str | None = None,
    ) -> str:
        """Send one A2A `message/send` request and return the task id as correlation id.

        A task that completes inline (an artifact in the same HTTP response - a real,
        valid A2A shape for a fast peer) has its answer recorded immediately, so
        `read_answer` can return it with no further wire traffic. A task reported
        `submitted` records nothing yet; `answer()` is how a later delivery arrives.

        `asker` is the asking agent's own id, sent as message metadata so the receiving
        side can enforce its half of the allowlist - WHO ASKED TRAVELS AS METADATA, above.
        It is an argument BEYOND `ports.agent_mailbox.AgentMailbox.ask` and therefore
        defaulted, exactly as on `PgAgentMailbox.ask`: a caller typed on the port cannot
        pass it yet. Omitting it sends no `from_agent_id` key at all rather than a null
        one, and says so at WARNING - an absent claim is not the same message as a claim
        of nobody, and a peer must be able to tell the two apart.

        `request_key` (C2) is sent as the A2A `messageId`, derived with `peer_ask_id`, so a
        replayed send carries the same id and a peer that de-duplicates on it runs the task
        once. Omitted, no `messageId` is sent (the previous behaviour).
        """
        if continue_session is not None:
            # The A2A peer owns its sessions: this adapter could only forward a context id the peer
            # may not honour, and a follow-up that silently starts fresh is worse than a refusal.
            raise PeerContinuationRefusedError("this peer cannot continue a session over A2A")
        endpoint = _endpoint_for(policy, target)
        answer_token = new_answer_token()
        if asker is None:
            _LOG.warning(
                "an A2A peer ask is being sent to %r with no asking agent id, so the "
                "receiving side cannot re-run callee_policy.may_ask(caller) and accepts "
                "the question on this side's word alone. docs/TASKS.md#t-f11-48",
                target,
            )
        metadata: dict[str, Any] = {
            "from_session_id": str(from_session.session_id),
            "from_tenant_id": str(from_session.tenant_id),
            "turn_id": str(turn_id),
            "hop": hop,
            "answer_token": answer_token,
        }
        if asker is not None:
            metadata["from_agent_id"] = str(asker)
        message_id: dict[str, str] = (
            {}
            if request_key is None
            else {"messageId": peer_ask_id(str(turn_id), request_key, str(target), question)}
        )
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    **message_id,
                    "parts": [{"kind": "text", "text": question}],
                    # Not enforced here (see module docstring) - carried so a compliant
                    # peer, or a future receiving-side A2A adapter, can enforce its own
                    # side of the allowlist and the hop limit.
                    "metadata": metadata,
                }
            },
        }
        response = await self._client.post(endpoint, json=payload)
        response.raise_for_status()
        envelope: dict[str, Any] = response.json()
        result: dict[str, Any] = envelope["result"]
        correlation_id = str(result["id"])
        self._tokens.setdefault(correlation_id, answer_token)
        self._turns.setdefault(correlation_id, str(turn_id))
        state = result["status"]["state"]
        if state == "completed":
            artifacts: list[dict[str, Any]] = result.get("artifacts", [])
            if artifacts:
                self._answers[correlation_id] = str(artifacts[0]["parts"][0]["text"])
        elif state in _FAILED_STATES:
            # C1m. The callee's failure is DATA on the answer (C1l), and over A2A it is the task
            # state. Without this a remote failure never resolved the ask (or, once delivered
            # through `answer`, was indistinguishable from a reply). The text is the peer's own
            # status message when it sent one: it is untrusted like every answer and is wrapped on
            # read.
            self._answers[correlation_id] = _status_text(result) or _FAILED_TASK_TEXT
            self._failed.add(correlation_id)
        return correlation_id

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        """Record a delivered answer against its correlation id. Idempotent.

        `failed` (C1l) is kept beside the answer in process memory like the answers themselves, and
        read back by `answer_failed`; the first delivery decides.

        C1c. The delivery must present the `answer_token` sent to the peer with the message
        (`metadata.answer_token`); otherwise `PeerAnswerRefusedError`. C1h: that refusal is for a
        task this adapter KNOWS (it sent it) and a delivery that does not hold its token. A task
        it never sent is a stranger's delivery, a no-op exactly as the port says and
        `PgAgentMailbox` does: nothing to refuse, nothing written. D33: it is logged (a warning
        with the correlation id, never a token), because the token table is process memory only
        and a restart makes a genuine late answer look the same. Persisting the tokens would
        need a table on this otherwise table-less adapter; not done.

        `setdefault` makes the first delivery win, exactly like `PgAgentMailbox.answer`'s
        `answered_at IS NULL` guard: a retried delivery for one correlation id must not
        change what a suspended turn resumes with.
        """
        if correlation_id in self._answers:
            return
        if correlation_id in self._cancelled:
            raise PeerAnswerRefusedError(correlation_id, "the ask was cancelled")
        expected = self._tokens.get(correlation_id)
        if expected is None:
            # An unknown id is a no-op by the port, but over A2A the tokens exist in THIS
            # process's memory only (`_tokens`): after a restart a genuine late answer lands
            # here too, and dropping it without a word hides a lost reply. Log the id (an
            # address, not a secret); never the token.
            _LOG.warning(
                "dropping a delivered answer for A2A task %s: no answer token on file for it. "
                "A2A tokens are kept in process memory only, so this is a stranger's delivery "
                "or an answer to an ask made before this process started",
                correlation_id,
            )
            return
        if answer_token is None or not secrets.compare_digest(
            expected.encode(), answer_token.encode()
        ):
            raise PeerAnswerRefusedError(correlation_id, "the answer token did not match")
        self._answers[correlation_id] = answer
        if failed:
            self._failed.add(correlation_id)

    async def answer_failed(self, correlation_id: str) -> bool:
        return correlation_id in self._failed

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        """Mark an unanswered task cancelled (local only, see `_cancelled`). True if it was
        open: known to this adapter, not answered, not already cancelled."""
        if (
            correlation_id not in self._tokens
            or correlation_id in self._answers
            or correlation_id in self._cancelled
        ):
            return False
        self._cancelled.add(correlation_id)
        return True

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        open_ids = sorted(
            corr
            for corr, turn in self._turns.items()
            if turn == turn_id and corr not in self._answers and corr not in self._cancelled
        )
        self._cancelled.update(open_ids)
        return tuple(open_ids)

    async def is_cancelled(self, correlation_id: str) -> bool:
        return correlation_id in self._cancelled

    async def redeliveries(self, correlation_id: str) -> int:
        """A2A has no redelivery lease on this side: the peer owns its queue. Always 0."""
        return 0

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
