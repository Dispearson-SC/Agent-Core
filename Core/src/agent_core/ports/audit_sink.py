"""Port: AuditSink - what happened, who asked, what did it cost?

Phase:      F1, widened in F3
Tasks:      docs/TASKS.md#t-f1-09, docs/TASKS.md#t-f3-14
Status:     FROZEN - widened once, for the refused-decision seat D25 named and left
            unowned. `Core/tests/unit/test_ports_audit_sink.py` is the regression lock,
            and its negative case is the four-member pre-widening shape.
Adapter:    adapters/driven/persistence_pg/audit_repository.py
Per-vertical: NO

NON-NEGOTIABLE (CLAUDE.md #6)
    Writes happen OUTSIDE the domain transaction. Append-only, never UPDATE.

    If the audit write shares a transaction with the domain write, a failed turn rolls
    back its own evidence - and the one turn you most need to explain is the one that
    left no trace. Use a separate connection or an autonomous transaction.

WHY THIS IS A PORT AND NOT A LOGGER
    For the fraud vertical this is EVIDENCE, with a retention policy and possibly a legal
    reader. A logger is best-effort, rotated, and grep-shaped. These are different things
    and conflating them is discovered at the worst possible moment.

WHAT MUST BE RECORDED - the "why was this allowed?" test
    Six months from now someone asks why the agent froze an account. The answer must be
    reconstructible from this table alone: the caller, the profile, the tool, the
    arguments, the policy decision AND its rule_id, who approved it, and what it cost.
    If any of those is missing, the record does not answer the question.

    The question has a second half, and it went unrecorded until t-f3-14: who tried and
    was REFUSED. A control nobody can show fired is indistinguishable from one that was
    never wired, so a refused attempt needs a row of its own - see
    `record_rejected_decision` below and docs/DECISIONS.md#d25.

WHY EVERY METHOD HERE IS ASYNC (D13)
    Every one of them is an append to Postgres on its own connection, and `record_tool_call`
    in particular runs inside `before_tool_execute` - i.e. on the critical path of every
    tool call. Blocking the event loop there would stall every other turn in the process
    for the duration of the insert.

    The separate connection required by CLAUDE.md #6 makes this stricter, not looser: the
    audit write cannot ride the domain transaction's thread, so it needs its own awaitable
    call. As in `ConversationStore`, any `@DBOS.transaction` underneath stays synchronous
    and the adapter wraps it in `asyncio.to_thread`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from agent_core.domain.media import MediaRef
from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, ToolCallId, TurnId, Usage


class AuditSink(Protocol):
    async def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        """PSEUDO-CODE - F1. Called from `before_tool_execute`, i.e. BEFORE the side effect.

        ASYNC (D13): an insert on the hot path of every tool call. Unlike
        `ToolPolicy.decide`, this one genuinely does I/O and therefore cannot be sync.

        Record the intent and the verdict, not the outcome. A tool that is about to run
        and then crashes the process must still appear here.

        REDACT before writing: arguments can carry credentials, tokens and personal data.
        Keep a redaction allowlist per tool rather than a denylist of key names - a
        denylist misses the field someone adds next month, silently.
        """
        ...

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None,
    ) -> None:
        """Who decided, what they decided, and when. This row is the one a compliance
        reader asks for first.

        ASYNC (D13): an insert, written on the resume path.

        `tool_call_id` is the domain `ToolCallId` that `PendingRequest` carries, not a
        bare string: an approval filed against an id no tool call ever had is indexed,
        queryable and wrong, and nothing downstream can tell.
        """
        ...

    async def record_rejected_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        reason: str,
    ) -> None:
        """Somebody tried to decide and was not allowed to. docs/DECISIONS.md#d25.

        ASYNC (D13): an insert, written on the same path `record_human_decision` is.

        WHY THIS IS NOT `record_human_decision(approved=False)`
            Because it is not one. That member means "a human decided", and a refusal
            filed through it reads in the trail as a decision to refuse that the human
            never made - a false record, which is worse than the silence D25 chose over
            it. The four-eyes check therefore runs BEFORE the audit row in
            `DecideApproval`, and this is the member it writes instead.

            The distinction survives only while these are two members. Collapsing them
            behind a flag re-creates the exact row D25 refused to write.

        WHY `reason` IS NOT OPTIONAL
            The row is read once, months later, by someone asking why an approval did
            not take effect. "Refused" with no grounds cannot be told apart from a
            clerical error, and the grounds are the only part that answers them.

        CARRIES NO VERDICT
            No `approved` seat: nothing was decided. `subject_id` is whoever attempted
            it, `tool_call_id` is the request they attempted it against.

        Append-only and outside the domain transaction, like every member here
        (CLAUDE.md non-negotiable #6). This is the one row a rejected attempt leaves,
        so a rollback that takes it removes the only evidence the control fired.
        """
        ...

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        """PSEUDO-CODE - F7. `direction` is 'inbound' | 'outbound'.

        ASYNC (D13): an insert.

        Records the sha256, never the bytes. That hash is how an auditor later proves the
        file attached to a case is the file that was uploaded.
        """
        ...

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        """Close the turn's cost record.

        ASYNC (D13): an insert.

        TODO(F5): this is the series that proves compaction is working. If cost per turn
        keeps climbing on a long conversation, the ladder is not firing - or it is firing
        too often and destroying the prompt cache. Neither shows up as a failing test;
        this table is the only place it is visible.
        """
        ...
