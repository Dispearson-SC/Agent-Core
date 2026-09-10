"""Port: AuditSink - what happened, who asked, what did it cost?

Phase:      F1
Tasks:      docs/TASKS.md#t-f1-09
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
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from agent_core.domain.media import MediaRef
from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, TurnId, Usage


class AuditSink(Protocol):
    def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        """PSEUDO-CODE - F1. Called from `before_tool_execute`, i.e. BEFORE the side effect.

        Record the intent and the verdict, not the outcome. A tool that is about to run
        and then crashes the process must still appear here.

        REDACT before writing: arguments can carry credentials, tokens and personal data.
        Keep a redaction allowlist per tool rather than a denylist of key names - a
        denylist misses the field someone adds next month, silently.
        """
        ...

    def record_human_decision(
        self, turn_id: TurnId, tool_call_id: str, subject_id: str, approved: bool, note: str | None
    ) -> None:
        """Who decided, what they decided, and when. This row is the one a compliance
        reader asks for first."""
        ...

    def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        """PSEUDO-CODE - F7. `direction` is 'inbound' | 'outbound'.

        Records the sha256, never the bytes. That hash is how an auditor later proves the
        file attached to a case is the file that was uploaded.
        """
        ...

    def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        """Close the turn's cost record.

        TODO(F5): this is the series that proves compaction is working. If cost per turn
        keeps climbing on a long conversation, the ladder is not firing - or it is firing
        too often and destroying the prompt cache. Neither shows up as a failing test;
        this table is the only place it is visible.
        """
        ...
