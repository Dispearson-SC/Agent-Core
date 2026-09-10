"""Driven adapter: AuditSink over Postgres.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-14
Implements: ports/audit_sink.py

NON-NEGOTIABLE #6 - THE WHOLE REASON THIS FILE IS SEPARATE
    Writes go through a SEPARATE connection pool, outside the domain transaction.

    Sharing the pool means a failed turn rolls back its own evidence, and the one turn you
    most need to explain is the one that left no trace. This is not theoretical: it is the
    default behaviour if you wire it the obvious way.

TABLES (all append-only, no UPDATE, no DELETE)
    audit_tool_calls        turn_id, caller, tool, arguments jsonb, effect, rule_id, at
    audit_human_decisions   turn_id, tool_call_id, subject_id, approved, note, at
    audit_media             turn_id, media_id, sha256, direction, at
    audit_turn_costs        turn_id, input_tokens, output_tokens, cached, cost_usd, at

REDACTION
    Arguments carry credentials, tokens and personal data. Use a per-tool ALLOWLIST of
    fields to store, never a denylist of key names - a denylist misses the field somebody
    adds next month, and misses it silently.

audit_turn_costs IS THE COMPACTION DASHBOARD
    Cost per turn over a long conversation is the ONLY place a bad compaction strategy is
    visible. Rising cost after enabling compaction means the trigger is too low and the
    prompt cache is being destroyed. No test reports this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any

from agent_core.domain.media import MediaRef
from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, ToolCallId, TurnId, Usage

# A source of connections that belong to THIS sink. `psycopg_pool.ConnectionPool.connection`
# satisfies it directly, which is how composition hands over the separate audit pool; a
# plain `lambda: psycopg.connect(conninfo, autocommit=True)` satisfies it too.
#
# The sink takes a factory rather than a connection on purpose: a method that accepts a
# connection can be handed the domain transaction's one, and then non-negotiable #6 is a
# convention instead of a structure.
ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# Per-tool ALLOWLIST of argument fields that may be stored, never a denylist of key names.
# Empty by default and looked up per tool, so a tool nobody has opted in stores no
# arguments at all. A vertical supplies its own map through the constructor - no file under
# domain/, application/ or ports/ changes to add one.
ARGUMENT_ALLOWLIST: Mapping[str, frozenset[str]] = {}

_INSERT_TOOL_CALL = (
    "INSERT INTO audit_tool_calls (turn_id, caller, tool, arguments, effect, rule_id) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
_INSERT_HUMAN_DECISION = (
    "INSERT INTO audit_human_decisions (turn_id, tool_call_id, subject_id, approved, note) "
    "VALUES (%s, %s, %s, %s, %s)"
)
_INSERT_MEDIA = (
    "INSERT INTO audit_media (turn_id, media_id, sha256, direction) VALUES (%s, %s, %s, %s)"
)
_INSERT_TURN_COST = (
    "INSERT INTO audit_turn_costs (turn_id, input_tokens, output_tokens, cached, cost_usd) "
    "VALUES (%s, %s, %s, %s, %s)"
)


def _jsonb(payload: dict[str, object]) -> object:
    """Wrap a mapping for a jsonb column.

    Imported lazily so the module stays importable - and the redaction testable - without
    a compiled psycopg on the machine.
    """
    from psycopg.types.json import Jsonb

    return Jsonb(payload)


class PgAuditSink:
    """`AuditSink` over Postgres, writing on a connection source of its own.

    INSERT only. There is no method here that can rewrite a row, which is what makes the
    append-only rule real rather than aspirational.
    """

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        argument_allowlist: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self._connect = connect
        self._argument_allowlist: Mapping[str, frozenset[str]] = (
            ARGUMENT_ALLOWLIST if argument_allowlist is None else dict(argument_allowlist)
        )

    async def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        """The intent and the verdict, appended before the side effect runs.

        `rule_id` travels with the row: it is the field that answers "why was this
        allowed?" six months later, and it is the only one that cannot be reconstructed
        from anything else here.
        """
        await self._append(
            _INSERT_TOOL_CALL,
            (
                str(turn_id),
                caller.subject_id,
                tool_name,
                _jsonb(self._redact(tool_name, arguments)),
                str(decision.effect.value),
                decision.rule_id,
            ),
        )

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None,
    ) -> None:
        await self._append(
            _INSERT_HUMAN_DECISION,
            (str(turn_id), str(tool_call_id), subject_id, approved, note),
        )

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        """The sha256, never the bytes."""
        await self._append(
            _INSERT_MEDIA, (str(turn_id), str(media.media_id), media.sha256, direction)
        )

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        """The cost series that is the only place a bad compaction strategy shows up."""
        await self._append(
            _INSERT_TURN_COST,
            (
                str(turn_id),
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens > 0,
                cost_usd,
            ),
        )

    def _redact(self, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Keep the fields this tool opted in; drop everything else.

        Fail closed on an unknown tool: no entry means no argument is stored. The row is
        still written, because the caller, the tool and the verdict are the evidence - the
        arguments are only ever a bonus, and a leaked credential is not.
        """
        allowed = self._argument_allowlist.get(tool_name, frozenset())
        return {name: value for name, value in arguments.items() if name in allowed}

    async def _append(self, sql: str, params: Sequence[Any]) -> None:
        """One INSERT on this sink's own connection, off the event loop.

        D13: Postgres access is synchronous, so the blocking work runs in a thread. The
        connection comes from `self._connect` and from nowhere else, so the write can
        never join the caller's transaction.
        """
        await asyncio.to_thread(self._append_sync, sql, params)

    def _append_sync(self, sql: str, params: Sequence[Any]) -> None:
        with self._connect() as connection:
            connection.execute(sql, params)
