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

    Externally-executed is itself two cases, split by WHO executes: the user (EVIDENCE)
    or another agent (DELEGATION). See `PendingKind`.

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
    """WHAT the turn is waiting for, and therefore WHO can end the wait.

    APPROVAL   - a tool wants to run and needs a human yes/no.
    EVIDENCE   - a tool needs a human to SUPPLY something (a photo, a document).
    DELEGATION - a deferred call ANOTHER AGENT executes (`ask_peer`, t-f9-04). The answer
                 arrives as a peer's own turn completing on the same durable topic an
                 approval arrives on, which is why no new suspension machinery exists for
                 it (D17) - and exactly why it needed a kind of its own.

    All three suspend the turn identically. Only the channel message, the shape of the
    resumed result, and whether a person is asked at all differ.

    WHY DELEGATION IS A KIND AND NOT A TOOL NAME (t-f9-08)
        Pydantic AI's deferred tools split into approval-required and externally-executed
        (docs/DECISIONS.md#d9), and this enum used to stop there - so a peer ask arrived
        labelled EVIDENCE, indistinguishable from a request for the user's photo. Two
        modules then read `tool_name` to tell them apart
        (`application/start_turn.py::_notice_for`,
        `adapters/driving/workflow/turn_workflow.py::_answerable_by_a_human`), and both
        said in a comment that this is not where the decision belongs. A third mechanism
        has to be added to every such site, and the site somebody forgets fails in the
        worst direction: a question only an agent can answer is put in front of a person,
        who cannot act on it while the turn holds open for three days.

        EXTERNALLY-EXECUTED was never one case. It is two, and the axis that separates
        them is WHO executes - which is a property of the request, so it belongs on the
        request.

    THE ONE PLACE A TOOL NAME MAY STILL DECIDE THIS
        Whoever translates `DeferredToolRequests` into `PendingRequest` values (t-f7-07,
        the runner adapter) knows which tool it is translating and labels the kind there,
        once. Every reader downstream asks `kind`. That is the whole point: the tool name
        collapses from N consumers to one producer, and the producer is the only module
        that already has to know the tool.

    THE STORED-OUTCOME MIGRATION THIS MEMBER WAS FEARED TO NEED IS EMPTY
        `start_turn.py` weighed "a domain change plus a migration of every stored outcome"
        and deferred on the strength of it. The cost was never paid because there is
        nothing to pay it on: `turns.pending` is written as JSON by
        `persistence_pg/conversation_repository.py::_jsonable` and NOTHING reads it back
        into a `PendingRequest` - no production module constructs one at all yet. So no
        stored row can name a kind that did not exist, and no reader can misread one that
        now does. A historic row that recorded a peer ask as `"evidence"` still carries
        `tool_name` in the same blob, so a backfill remains possible for whoever first
        needs to ask that question of history. Nobody does today.
    """

    APPROVAL = "approval"
    EVIDENCE = "evidence"
    DELEGATION = "delegation"

    @property
    def answerable_by_a_human(self) -> bool:
        """Whether a PERSON can resolve a request of this kind.

        The single answer to the question `HumanGateway`'s callers have to ask before
        publishing anything. Reading `HUMAN_ANSWERABLE` rather than answering inline so
        the grid below stays the one place the decision is recorded.
        """
        try:
            return HUMAN_ANSWERABLE[self]
        except KeyError:
            raise KeyError(
                f"PendingKind.{self.name} has no row in HUMAN_ANSWERABLE. A new kind must "
                "be answered by hand: nothing may infer whether a person can resolve it."
            ) from None


# Who can end the wait. THE single source of truth: one row per PendingKind, one
# hand-written answer, no default and no derivation.
#
# WHY IT IS NOT `{kind: kind is not DELEGATION}` OR ANY OTHER DERIVATION
#     `t-f10-01` shipped a visibility table whose ADMIN row was `frozenset(EntryKind)`. It
#     read as a decision and was not one - every member ever added answered itself - and the
#     guard meant to catch an undecided member could not fail. Written out, a new
#     `PendingKind` leaves a hole here, `answerable_by_a_human` raises on it, and
#     `test_pending_kind.py` goes red until somebody answers True or False for it.
#
# WHY A FALSE ROW IS NOT "NOBODY IS TOLD ANYTHING"
#     A DELEGATION is never PUBLISHED to a person as a question, but the user is still told
#     THAT something is pending - CLAUDE.md non-negotiable #11 and
#     `EntryKind.PENDING_PLACEHOLDER`. This grid answers who is ASKED, not who is informed.
HUMAN_ANSWERABLE: dict[PendingKind, bool] = {
    PendingKind.APPROVAL: True,
    PendingKind.EVIDENCE: True,
    PendingKind.DELEGATION: False,
}


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

    Exclusivity is enforced at construction: an adapter producing a third shape fails
    here, at the boundary, instead of three steps deep inside a DBOS workflow where the
    traceback no longer names the culprit.
    """

    turn_id: TurnId
    pending: tuple[PendingRequest, ...] = ()
    result: TurnResult | None = None

    def __post_init__(self) -> None:
        """I2: SUSPENDED or FINISHED, never both and never neither."""
        if self.pending and self.result is not None:
            raise ValueError(
                "TurnOutcome cannot be both suspended and finished: "
                "pending and result are mutually exclusive."
            )
        if not self.pending and self.result is None:
            raise ValueError(
                "TurnOutcome must be either suspended or finished: "
                "supply exactly one of pending or result."
            )

    @property
    def is_suspended(self) -> bool:
        return bool(self.pending)
