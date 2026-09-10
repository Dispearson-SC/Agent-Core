"""Turn vocabulary - the nouns every other layer speaks.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-01
Status:  TYPES DEFINED / BEHAVIOUR PENDING

WHAT THIS FILE IS
    The lifecycle of one agent turn expressed as immutable data. A turn starts from a
    `TurnRequest` and ends in exactly one of two shapes:

        TurnOutcome(pending=(...), result=None)   -> SUSPENDED
        TurnOutcome(pending=(),    result=<...>)  -> FINISHED

    There is no third shape. Anything resembling one is an adapter bug, not a missing
    state here.

WHY SUSPENSION IS A FIRST-CLASS OUTCOME
    Pydantic AI's deferred tools END the run and return `DeferredToolRequests` instead of
    blocking a thread, and they cover BOTH approval-required and externally-executed
    tools. `PendingRequest.kind` is the only thing separating them - which is why one
    port (`HumanGateway`) serves approvals and evidence requests alike.
    See docs/DECISIONS.md#d9.

INVARIANTS (assert in tests)
    I1. Every dataclass here is frozen. A record of what happened must not be editable
        by the code reading it.
    I2. `pending` and `result` are mutually exclusive. Never both, never neither.
    I3. `TurnId` is generated INSIDE a DBOS step, never in a workflow body. A fresh id on
        replay forks the conversation. CLAUDE.md non-negotiable #2.
    I4. This module imports only the standard library and `agent_core.domain`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import NewType

from agent_core.domain.media import MediaRef

TurnId = NewType("TurnId", str)
SessionId = NewType("SessionId", str)
TenantId = NewType("TenantId", str)
ToolCallId = NewType("ToolCallId", str)


@dataclass(frozen=True, slots=True)
class SessionRef:
    """Identifies one conversation.

    `tenant_id` is present from day 1 even though multi-tenancy lands on day 2:
    retrofitting a tenant column into a live audit table costs far more than carrying an
    unused field."""

    session_id: SessionId
    tenant_id: TenantId


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """WHO asked. This is the input to `ToolPolicy`, so it must carry everything a rule
    could reasonably branch on. Adding a field later means revisiting every stored rule,
    so err on the side of including it now."""

    subject_id: str
    channel: str
    tenant_id: TenantId
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class UserInput:
    """What the human sent. `media` holds references, never bytes."""

    text: str
    media: tuple[MediaRef, ...] = ()


class PendingKind(StrEnum):
    """APPROVAL - a tool wants to run and needs a human yes/no.
    EVIDENCE - a tool needs a human to SUPPLY something (a photo, a document).

    Both suspend the turn identically. Only the channel message and the shape of the
    resumed result differ."""

    APPROVAL = "approval"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """One thing the turn is waiting on.

    `tool_call_id` MUST be the id Pydantic AI issued for the deferred call. Resuming with
    a different id silently drops the result and the agent loops asking again - a silent
    bug with no exception. Round-trip it verbatim; never regenerate it."""

    kind: PendingKind
    tool_call_id: ToolCallId
    tool_name: str
    arguments: dict[str, object]
    reason: str


@dataclass(frozen=True, slots=True)
class Usage:
    """Token and money accounting for one turn. Fed to `ContextEngine.update_from_response`
    and to `AuditSink`.

    `context_window_used` is the fraction of the model's window occupied as of the last
    response, or None when the provider does not report it. None is COMMON, not
    exceptional: `ContextEngine` must fall back to a local estimate or the agent never
    compacts and dies of context overflow in production. docs/ARCHITECTURE.md#5."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    context_window_used: float | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    """A finished turn."""

    text: str
    media: tuple[MediaRef, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Everything needed to start a turn. Deliberately has no defaults: a caller that
    forgets the profile or the identity should fail at construction, not at the policy
    check three layers down."""

    session: SessionRef
    caller: CallerIdentity
    profile_id: str
    input: UserInput


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """The two-shaped result. See I2.

    TODO(F1): add `__post_init__` asserting exclusivity, and a test constructing both
    invalid shapes expecting ValueError. Cheap, and it catches adapter bugs at the
    boundary instead of three steps deep inside a DBOS workflow.
    """

    turn_id: TurnId
    pending: tuple[PendingRequest, ...] = ()
    result: TurnResult | None = None

    @property
    def is_suspended(self) -> bool:
        return bool(self.pending)
