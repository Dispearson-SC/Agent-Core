"""Port: TranscriptReader - what happened in this conversation?

Phase:   F10
Tasks:   docs/TASKS.md#t-f10-02
Status:  t-f10-02 FROZEN - audience and tenant seats pinned by
         tests/unit/test_ports_transcript_reader.py
Adapter: adapters/driven/persistence_pg/transcript_repository.py
Per-vertical: NO

THIS PORT STORES NOTHING NEW
    ConversationStore already has the messages. AuditSink already has the tool calls, the
    policy decisions and the human decisions. This is a PROJECTION over existing rows.

    That is why it is read-only and why it is a separate port: "what happened" is a
    different question from "record what is happening".

STORE EVERYTHING, FILTER ON READ
    `Audience` decides what comes back; storage keeps everything. An audit that stored a
    redacted copy cannot answer a question nobody anticipated, and two write paths drift -
    with the drifting one always being the one nobody reads until an incident.

WHAT THE VIEWER NEEDS, AND WHY EACH PIECE MATTERS
    - user and agent messages         the conversation itself
    - tool calls, arguments, results  ADMIN ONLY. What the agent actually did.
    - reasoning, when the model emits ADMIN ONLY. Why it did it.
    - pending requests               what it is waiting on, and since when
    - policy denials                 what it was stopped from doing - often the most
                                     interesting row on the page
    - knowledge hits                 which document produced an answer, which is how you
                                     debug a wrong price without guessing
    - peer exchanges                 what it asked another agent
    - compaction events              where history was summarised, so a gap in the
                                     transcript is explained instead of looking like loss
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.transcript import Audience, ConversationSummaryRow, TranscriptPage
from agent_core.domain.turn import SessionRef, TenantId


class TranscriptReader(Protocol):
    async def page(
        self,
        session: SessionRef,
        audience: Audience,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TranscriptPage:
        """PSEUDO-CODE - F10.

        1. Merge messages, audit rows and pending state into one timeline ordered by time.
        2. Filter kinds through VISIBLE_TO[audience].
        3. For Audience.USER, replace each hidden PENDING_REQUEST with a
           PENDING_PLACEHOLDER. The user must not learn WHICH tool is pending, but must see
           THAT something is - a conversation that silently stops while the agent waits
           three days looks broken, and the user leaves.
        4. Cursor pagination, never offset: a live conversation grows while someone reads
           it, and offsets then skip or repeat entries.

        TENANT SCOPING IS IN THE QUERY, not applied afterwards. This endpoint returns whole
        conversations; a missing tenant predicate here is a cross-tenant data breach with a
        friendly UI on top.

        The tenant arrives inside `SessionRef`, which exists so a session can never be
        named without one. It is NOT also passed as a second parameter: two sources for one
        fact can disagree, and the adapter would then have to pick which one scopes the
        query.
        """
        ...

    async def list_conversations(
        self,
        tenant: TenantId,
        audience: Audience,
        *,
        profile_id: str | None = None,
        suspended_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[tuple[ConversationSummaryRow, ...], str | None]:
        """The inbox view. `suspended_only=True` is the operational queue: every
        conversation stuck waiting on a human, oldest first. That list is the one an
        operator actually works from.

        THE LIST IS FILTERED TOO, so it takes an `Audience` exactly like `page` does.
        `ConversationSummaryRow` carries `total_cost_usd`, which is an ADMIN field by the
        same rule that makes tool arguments admin-only; and the rows a caller may see at
        all are the caller's own, not every conversation in the tenant. A list method
        without an audience seat would have to default to one, and a default is either a
        leak or an operator queue that hides its own work.

        `waiting_since` and `is_suspended` survive into the USER projection on purpose:
        the same reason `PENDING_PLACEHOLDER` exists. A user must not learn WHICH tool is
        pending, but must see THAT something is.

        NARROWING IS ENUMERATED, NEVER COMPOSED BY THE CALLER. `profile_id` and
        `suspended_only` are typed options the adapter turns into SQL itself; `cursor` is
        an opaque token the adapter minted. There is deliberately no free-form filter
        parameter - the moment one exists, the tenant predicate stops being this port's
        guarantee and becomes something every call site is trusted to remember."""
        ...
