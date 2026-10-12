"""Driven adapter: an `AuditSink` that also tells the event feed about each tool call (C21, D52).

Phase:   Agent-comms v2, C21
Tests:   Core/tests/unit/test_progress_events.py

`tool_called` happens inside the model step, in the runner's policy hook, where the only things in
hand are the turn id, the caller and the call. That hook already reports every call to the audit
sink BEFORE the call runs, so this decorator is the one place that sees all of them without the
runner learning about events.

  * The audit row is written first and is the system of record; the event is a courtesy copy.
  * A publisher that fails is logged and ignored: it must never fail the audited call.
  * The event carries the tool's NAME and the policy verdict (`allow`, `deny`, `needs_approval`),
    never the arguments.
  * A recorded model step is not re-run on replay, so a tool call is published once per execution
    of the step (at least once across a crash; the audit row has the same property).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from agent_core.domain.events import EventKind, ProgressEvent, progress_event_id
from agent_core.domain.media import MediaRef
from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, ToolCallId, TurnId, Usage
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.event_publisher import EventPublisher

__all__ = ["EventingAuditSink"]

_LOG = logging.getLogger(__name__)


class EventingAuditSink:
    def __init__(
        self,
        inner: AuditSink,
        events: EventPublisher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inner = inner
        self._events = events
        self._clock = clock

    async def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        await self._inner.record_tool_call(turn_id, caller, tool_name, arguments, decision)
        try:
            await self._events.publish(
                ProgressEvent(
                    # A repeated call is a new event; a random discriminator keeps them apart.
                    event_id=progress_event_id(
                        str(turn_id), EventKind.TOOL_CALLED, uuid.uuid4().hex
                    ),
                    kind=EventKind.TOOL_CALLED,
                    tenant_id=str(caller.tenant_id),
                    session_id="",
                    turn_id=str(turn_id),
                    agent="",
                    at=self._clock(),
                    tool=tool_name,
                    detail=decision.effect.value,
                )
            )
        except Exception:
            _LOG.warning(
                "could not publish the tool_called event of turn %r", turn_id, exc_info=True
            )

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None,
    ) -> None:
        await self._inner.record_human_decision(turn_id, tool_call_id, subject_id, approved, note)

    async def record_rejected_decision(
        self, turn_id: TurnId, tool_call_id: ToolCallId, subject_id: str, reason: str
    ) -> None:
        await self._inner.record_rejected_decision(turn_id, tool_call_id, subject_id, reason)

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        await self._inner.record_media(turn_id, media, direction)

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        await self._inner.record_turn_end(turn_id, usage, cost_usd)

    def __getattr__(self, name: str) -> Any:
        """Anything else the concrete sink offers (the composition root reads a few)."""
        return getattr(self._inner, name)
