"""Turn vocabulary - the nouns every other layer speaks.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-01, docs/TASKS.md#t-f11-29
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
    """Everything needed to start a turn. The three identifying fields deliberately have
    no defaults: a caller that forgets the profile or the identity should fail at
    construction, not at the policy check three layers down.

    `hop` IS THE DELEGATION DEPTH, AND ITS DEFAULT IS A STATEMENT (t-f11-47)
        How many agent-to-agent hops have already been made by the time this turn starts.
        Zero for a turn a HUMAN started; `PeerAsk.hop` for a turn a worker starts in order
        to ANSWER another agent's question.

        The count is turn-level state and it has to travel, because A -> B -> A cannot be
        caught by anything either side holds locally: to B, an ask arriving from A is the
        first ask B has seen. Only a number carried from the queue row into the turn tells
        "A started a conversation" apart from "A is being asked back by the agent it just
        asked" - `adapters/driven/peers/hop_limit.py`, THE COUNT HAS TO TRAVEL. Before
        this seat existed the gate was handed a 0 at every hop, so `max_hops` could never
        trip and a cycle between two agents that legitimately allow each other ran until
        somebody read the bill. Every other half of that gate - both `enabled` switches
        and both allowlists - was enforced, because none of them depends on a number.

        WHY IT DEFAULTS RATHER THAN BEING REQUIRED, AND WHAT THE DEFAULT MEANS
            `TurnRequest` is the shape every driving adapter builds - HTTP, the channels,
            the console, the scheduler and the peer worker - and four of those five start
            turns nobody delegated. `hop=0` is not a convenience for them: it is the true
            statement that a turn with no peer behind it is at depth zero, which is
            exactly what `authorise_hop` needs in order to allow the FIRST hop and count
            from there.

            The cost of a default is that the one adapter which must NOT take it could
            forget to pass the count, so the count is set where the row is in hand -
            `adapters/driving/peers/worker.py::peer_turn_request` builds the request from
            the `PeerAsk` - rather than being threaded through call sites that would each
            have to remember.

        It is a plain `int` and not a `NewType` because it is a count, not an identity:
        arithmetic on it is meaningful, and `hop + 1` is what an allowed decision hands
        back for the next ask to travel with.
    """

    session: SessionRef
    caller: CallerIdentity
    profile_id: str
    input: UserInput
    hop: int = 0


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """The two-shaped result. See I2.

    Exclusivity is enforced at construction: an adapter producing a third shape fails
    here, at the boundary, instead of three steps deep inside a DBOS workflow where the
    traceback no longer names the culprit.

    `messages` IS THE AGENT'S HALF OF THE CONVERSATION (t-f11-29)
        Everything this turn ADDED to the conversation that the store has not already
        written: the model's replies, its tool calls and the returns answering them. It is
        NOT the prompt - `ConversationStore.append_request` wrote that before the model ran
        (the ordering rule in ports/conversation_store.py), and a runner that returned it
        again would have the customer's own sentence stored twice and read back to the
        model twice.

        Until this seat existed, `TurnOutcome` carried a result and a pending list and no
        messages, so `StartTurn` had nothing to append but the prompt and the agent's own
        replies were never persisted by anything. A conversation with no agent turns in it
        is not a conversation: compaction has half a history to compact, the transcript
        shows half an exchange, and a model asked what it said a moment ago cannot answer.

    WHY THE ELEMENT TYPE IS `object` AND NOT THE ENCODED FORM
        I4: this module imports only the standard library and `agent_core.domain`, and
        `test_contract.py` walks the AST to keep it that way - so the element cannot be a
        Pydantic AI `ModelMessage`. Two shapes could cross instead, and the opaque one is
        the one that was chosen:

        - THE ENCODED FORM - the `(role, content json)` pair a `messages` row is made of -
          would put a second copy of the encoding decision outside `encode_message`, the
          one place `t-f1-24` settled it. Worse, whoever PRODUCES the outcome is the
          Pydantic AI adapter, so it would have to import the Postgres adapter to encode -
          coupling two driven adapters to each other and making the store's row shape a
          fact the model adapter has to know.
        - AN OPAQUE OBJECT the adapters own is exactly the arrangement this port pair
          already uses in the other direction: `ConversationStore.load_history` returns
          `object` and `AgentRunner.run` takes `history: object`, both so that no use case
          and no domain type has to name the library (D7). The runner produces the values,
          the store validates and encodes them, and `application/` carries them across
          without ever looking inside. This field is the return leg of that same trip.

        So the domain records THAT a turn added messages and never what one is made of.
        `StartTurn` moves them; only an adapter may open them.

    CLAUDE.md NON-NEGOTIABLE #5 IS WHY THEY TRAVEL AS ONE TUPLE
        A tool call and the return answering it are two messages, and a history holding one
        without the other is rejected by the provider - as a 400 on some LATER request,
        far from the turn that caused it. One field, written by one `append_outcome` in one
        transaction, makes the half-written pair unrepresentable rather than unlikely.
    """

    turn_id: TurnId
    pending: tuple[PendingRequest, ...] = ()
    result: TurnResult | None = None
    messages: tuple[object, ...] = ()

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
