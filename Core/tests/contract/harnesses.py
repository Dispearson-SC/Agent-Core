"""Adapters under the `AgentMailbox` conformance suite (`mailbox_conformance.py`)."""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
import psycopg

from agent_core.adapters.driven.peers.mailbox import PgAgentMailbox
from agent_core.adapters.driven.peers.mailbox_a2a import A2AAgentMailbox
from agent_core.domain.peers import AgentId
from agent_core.domain.turn import SessionRef, TurnId
from tests.contract.mailbox_conformance import ASKER, LEASE, POLICY, SESSION, TARGET
from tests.fakes.controlled_mailbox import ControlledMailbox
from tests.fakes.memory_mailbox import InMemoryAgentMailbox


class PgHarness:
    """`PgAgentMailbox` on a migrated database. The caller hands in an EMPTY `peer_messages`."""

    supports_queue = True
    supports_continuation = True

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self.mailbox = PgAgentMailbox(conninfo)

    async def ask(
        self,
        question: str,
        *,
        turn_id: str = "turn-1",
        request_key: str = "tc-1",
        max_concurrent: int | None = None,
        target: AgentId = TARGET,
        session: SessionRef = SESSION,
        asker: AgentId = ASKER,
        continue_session: str | None = None,
        root_turn_id: str | None = None,
    ) -> str:
        policy = (
            POLICY
            if max_concurrent is None
            else dataclasses.replace(POLICY, max_concurrent_asks=max_concurrent)
        )
        return await self.mailbox.ask(
            policy,
            target,
            question,
            from_session=session,
            turn_id=TurnId(turn_id),
            hop=0,
            asker=asker,
            request_key=request_key,
            continue_session=continue_session,
            root_turn_id=root_turn_id,
        )

    def callee_token(self, correlation_id: str) -> str | None:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(
                "SELECT answer_token FROM peer_messages WHERE correlation_id = %s",
                (correlation_id,),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    async def claim(self, target: AgentId = TARGET) -> Any:
        return await self.mailbox.claim_next(target)

    async def age(self, correlation_id: str, by: timedelta) -> None:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            conn.execute(
                "UPDATE peer_messages SET delivered_at = delivered_at - %s "
                "WHERE correlation_id = %s",
                (by, correlation_id),
            )

    async def reclaim(
        self, exists: Any, retire: Any = None, failed: Any = None
    ) -> tuple[str, ...]:
        return await self.mailbox.reclaim_stale(
            TARGET, lease=LEASE, exists=exists, retire=retire, failed=failed
        )

    def redeliveries(self, correlation_id: str) -> int:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(
                "SELECT redeliveries FROM peer_messages WHERE correlation_id = %s",
                (correlation_id,),
            ).fetchone()
        assert row is not None
        return int(row[0])


class A2AHarness:
    """`A2AAgentMailbox` against an in-process peer (`httpx.MockTransport`).

    The peer behaves like a compliant A2A server: it accepts every task as `submitted`, keeps the
    secret it was sent with the FIRST message of a task, and de-duplicates on `messageId` (the
    id the adapter derives from the request key), returning the task it already made.
    """

    supports_queue = False
    supports_continuation = False

    def __init__(self) -> None:
        self._messages: dict[str, dict[str, Any]] = {}  # task id -> first message
        self._by_message_id: dict[str, str] = {}
        self.mailbox = A2AAgentMailbox(httpx.AsyncClient(transport=httpx.MockTransport(self._peer)))

    def _peer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        message = body["params"]["message"]
        message_id = message.get("messageId")
        task = self._by_message_id.get(message_id) if message_id else None
        if task is None:
            task = f"task-{uuid.uuid4()}"
            self._messages[task] = message
            if message_id:
                self._by_message_id[message_id] = task
        result = {"id": task, "status": {"state": "submitted"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    async def ask(
        self,
        question: str,
        *,
        turn_id: str = "turn-1",
        request_key: str = "tc-1",
        max_concurrent: int | None = None,
        target: AgentId = TARGET,
        session: SessionRef = SESSION,
        asker: AgentId = ASKER,
        continue_session: str | None = None,
        root_turn_id: str | None = None,
    ) -> str:
        policy = (
            POLICY
            if max_concurrent is None
            else dataclasses.replace(POLICY, max_concurrent_asks=max_concurrent)
        )
        return await self.mailbox.ask(
            policy,
            target,
            question,
            from_session=session,
            turn_id=TurnId(turn_id),
            hop=0,
            asker=asker,
            request_key=request_key,
            continue_session=continue_session,
            root_turn_id=root_turn_id,
        )

    def callee_token(self, correlation_id: str) -> str | None:
        message = self._messages.get(correlation_id)
        return None if message is None else str(message["metadata"]["answer_token"])


class MemoryHarness:
    """`InMemoryAgentMailbox`, optionally behind a `ControlledMailbox` with no fault planned.

    The wrapped variant proves the test kit is transparent: whatever a restart test does through
    it is what the bare mailbox would have done.
    """

    supports_queue = True
    supports_continuation = True

    def __init__(self, *, controlled: bool = False) -> None:
        self.inner = InMemoryAgentMailbox()
        self.mailbox: Any = ControlledMailbox(self.inner) if controlled else self.inner

    async def ask(
        self,
        question: str,
        *,
        turn_id: str = "turn-1",
        request_key: str = "tc-1",
        max_concurrent: int | None = None,
        target: AgentId = TARGET,
        session: SessionRef = SESSION,
        asker: AgentId = ASKER,
        continue_session: str | None = None,
        root_turn_id: str | None = None,
    ) -> str:
        policy = (
            POLICY
            if max_concurrent is None
            else dataclasses.replace(POLICY, max_concurrent_asks=max_concurrent)
        )
        return await self.mailbox.ask(
            policy,
            target,
            question,
            from_session=session,
            turn_id=TurnId(turn_id),
            hop=0,
            asker=asker,
            request_key=request_key,
            continue_session=continue_session,
            root_turn_id=root_turn_id,
        )

    def callee_token(self, correlation_id: str) -> str | None:
        return self.inner.token_of(correlation_id)

    async def claim(self, target: AgentId = TARGET) -> Any:
        return await self.mailbox.claim_next(target)

    async def age(self, correlation_id: str, by: timedelta) -> None:
        self.inner.age(correlation_id, by)

    async def reclaim(
        self, exists: Any, retire: Any = None, failed: Any = None
    ) -> tuple[str, ...]:
        return await self.mailbox.reclaim_stale(
            TARGET, lease=LEASE, exists=exists, retire=retire, failed=failed
        )

    def redeliveries(self, correlation_id: str) -> int:
        return int(self.inner.rows[correlation_id].redeliveries)


def database_name_for(suffix: str) -> str:
    return re.sub(r"\W", "_", f"agent_core_conformance_{suffix}")
