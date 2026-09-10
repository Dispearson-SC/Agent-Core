"""Transcript vocabulary - the same rows, two audiences.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-01
Status:  TYPES DEFINED / BEHAVIOUR PENDING

WHAT THIS IS
    The read model behind a conversation viewer: user and agent messages, the tools the
    agent used, its reasoning if the model produced any, and what it is currently waiting
    on.

    NOTHING NEW IS STORED FOR THE USER-FACING PART. ConversationStore already has the
    messages; AuditSink already has the tool calls and the human decisions. This is a
    PROJECTION over data that already exists.

THE CENTRAL DESIGN DECISION - STORE EVERYTHING, FILTER ON READ
    Redaction happens in the projection, NOT in storage. Two reasons:
      1. An audit that stored a redacted copy cannot answer a question nobody anticipated.
      2. A single storage path is one place to get right; two paths drift, and the one that
         drifts is always the one nobody reads until an incident.

    So: one write path, `Audience` decides what comes back.

THE ONE THING THE USER MUST STILL SEE ABOUT HIDDEN ACTIVITY
    A user must not see WHICH tool is pending, but must see THAT something is pending.
    A conversation that silently stops while the agent waits three days for an approval
    looks broken, and the user leaves. `PENDING_PLACEHOLDER` exists for exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from agent_core.domain.turn import SessionRef, TurnId


class Audience(StrEnum):
    """USER  - the person in the conversation. Messages only, plus a pending placeholder.
    ADMIN - the operator. Everything: tools, arguments, results, reasoning, costs,
            policy decisions, peer traffic.

    There is no third audience. If one is ever needed, it is a new value here and a new
    answer in EVERY row of `VISIBILITY` - never an ad-hoc filter at a call site. The grid is
    written cell by cell precisely so that a new audience cannot inherit a default.
    """

    USER = "user"
    ADMIN = "admin"


class EntryKind(StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    PENDING_REQUEST = "pending_request"
    PENDING_PLACEHOLDER = "pending_placeholder"
    HUMAN_DECISION = "human_decision"
    PEER_EXCHANGE = "peer_exchange"
    COMPACTION = "compaction"
    KNOWLEDGE_HIT = "knowledge_hit"
    POLICY_DENIAL = "policy_denial"


# The visibility grid. THE single source of truth for who sees what: one row per
# EntryKind, one hand-written answer per Audience.
#
# WHY EVERY CELL IS WRITTEN OUT INSTEAD OF "ADMIN SEES EVERYTHING"
#     The first cut declared ADMIN as `frozenset(EntryKind)`. It read as a decision and was
#     not one: the set was DERIVED from the enum, so every kind ever added was ADMIN-visible
#     the instant it was declared, and the test that was supposed to catch an undecided kind
#     could not fail - the enum satisfied it by construction. A default wearing a decision's
#     clothes, and the default leaned towards disclosure.
#
#     Written out, adding an EntryKind leaves a hole here and `t-f10-07` fails until someone
#     answers True or False for each audience. Adding an Audience leaves a hole in EVERY
#     row and the same test fails. There is deliberately no shortcut that fills a row in.
#
# WHY ADMIN DOES NOT SEE PENDING_PLACEHOLDER
#     The placeholder is not stored. It is what the projection hands a USER INSTEAD of a
#     PENDING_REQUEST, so the user learns THAT something is pending without learning WHICH
#     tool. An operator reads the real PENDING_REQUEST. Admitting the substitute to the
#     admin view too was never decided - it was what `frozenset(EntryKind)` did on its own,
#     and it contradicts the table in docs/ARCHITECTURE.md "Store everything, filter on
#     read", which has always shown the placeholder as user-only.
VISIBILITY: dict[EntryKind, dict[Audience, bool]] = {
    #                                    USER    ADMIN
    EntryKind.USER_MESSAGE:        {Audience.USER: True,  Audience.ADMIN: True},
    EntryKind.AGENT_MESSAGE:       {Audience.USER: True,  Audience.ADMIN: True},
    EntryKind.TOOL_CALL:           {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.TOOL_RESULT:         {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.REASONING:           {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.PENDING_REQUEST:     {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.PENDING_PLACEHOLDER: {Audience.USER: True,  Audience.ADMIN: False},
    EntryKind.HUMAN_DECISION:      {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.PEER_EXCHANGE:       {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.COMPACTION:          {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.KNOWLEDGE_HIT:       {Audience.USER: False, Audience.ADMIN: True},
    EntryKind.POLICY_DENIAL:       {Audience.USER: False, Audience.ADMIN: True},
}


# The reader's lookup, derived from the grid so there is one place to change and no second
# path to drift. A kind with no row above appears in neither set - invisible to everyone,
# which is the safe failure, and `t-f10-07` fails loudly rather than letting it ship.
VISIBLE_TO: dict[Audience, frozenset[EntryKind]] = {
    audience: frozenset(
        kind for kind, row in VISIBILITY.items() if row.get(audience, False)
    )
    for audience in Audience
}


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One item in the timeline.

    `payload` is deliberately loose: a message body, tool arguments, a reasoning block, a
    pending request. The projection narrows it per kind; a stricter union here would mean a
    new type for every EntryKind and a migration for every addition.

    REASONING IS ADMIN-ONLY AND DESERVES ITS OWN RETENTION DECISION. Reasoning traces are
    bulky and contain intermediate speculation the model discarded - including guesses
    about a user that were never said out loud. Storing them forever is a liability, not an
    asset. TODO(F10): decide the retention window and write it in docs/DECISIONS.md."""

    entry_id: str
    turn_id: TurnId
    kind: EntryKind
    at: datetime
    payload: dict[str, object]
    cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    """A page of a conversation, newest-last so a viewer renders top to bottom.

    Cursor-based, not offset-based: a long conversation grows while someone reads it, and
    offsets silently skip or repeat entries when that happens."""

    session: SessionRef
    audience: Audience
    entries: tuple[TranscriptEntry, ...]
    next_cursor: str | None = None
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class ConversationSummaryRow:
    """One row in a conversation LIST - the inbox view of the viewer.

    `waiting_since` is what makes the list operationally useful: it surfaces conversations
    stuck on a human, which is the queue an operator actually needs to work."""

    session: SessionRef
    profile_id: str
    last_activity_at: datetime
    message_count: int
    is_suspended: bool
    waiting_since: datetime | None = None
    total_cost_usd: Decimal | None = None
