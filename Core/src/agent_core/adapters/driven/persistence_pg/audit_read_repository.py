"""Driven adapter: AuditReader over Postgres.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-08
Implements: ports/audit_reader.py
Tests:   Core/tests/integration/test_audit_reader.py

WHY THIS IS A SEPARATE MODULE FROM audit_repository.py
    `PgAuditSink` is the append-only writer and its file says so: there is no method on it
    that can rewrite a row, and that absence is what makes non-negotiable #6 structural
    rather than a convention. Putting a SELECT beside those INSERTs would not break the
    rule, but it would put the reader inside the class every future "just fix that one
    row" patch is aimed at. Two files, two responsibilities, and the writer stays a type
    whose entire surface is appends.

    They share the connection factory type, and in production they should share the
    POOL: the audit pool is the one that can see this table, and a reader that opened a
    connection of its own against a different conninfo would be an inspection tool reading
    a different database from the one the evidence was written to.

WHAT THIS DELIBERATELY DOES NOT DO
    It does not re-redact. The arguments come back exactly as `PgAuditSink._redact` stored
    them, because two redaction paths drift and the drifting one is always the one nobody
    reads until an incident.

    It does not join `turns`. See the module docstring of `ports/audit_reader.py`: the
    audit write is outside the domain transaction on purpose, so a turn whose domain
    writes rolled back has audit rows and no `turns` row. An inner join would hide exactly
    the rows #6 exists to preserve.

    It does not re-decide. `effect`, `rule_id` and `reason` are read back as recorded;
    asking the live policy engine would answer with today's rules about yesterday's call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_core.adapters.driven.persistence_pg.audit_repository import ConnectionFactory
from agent_core.domain.policy import Effect
from agent_core.domain.turn import TurnId
from agent_core.ports.audit_reader import AuditedToolCall
from agent_core.ports.knowledge_admin import AdminIdentity

# ORDER BY id, never `at`. `id` is the BIGSERIAL the row was appended under, so it is the
# append order itself; `at` defaults to now() and ties inside a millisecond, which would
# let a denial come back ahead of the call it refused.
#
# `reason` is migration 0022's column (audit_reason_migration.py). It is NULL on every row
# written before that migration, and that NULL is honest - those rows never recorded a
# sentence. Nothing here substitutes a placeholder for it.
_SELECT_TOOL_CALLS = """
    SELECT caller, tool, arguments, effect, rule_id, reason, at
    FROM audit_tool_calls
    WHERE turn_id = %s
    ORDER BY id
"""


@dataclass(frozen=True, slots=True)
class PgAuditReadRepository:
    """`AuditReader` over `audit_tool_calls`, on the audit pool's own connections.

    SELECT only. There is no statement in this module that can write, which is the mirror
    image of what makes `PgAuditSink` append-only: an evidence reader that could also
    correct a row turns every inspection tool into a write path.
    """

    connect: ConnectionFactory

    async def tool_calls_for_turn(
        self, admin: AdminIdentity, turn_id: TurnId
    ) -> tuple[AuditedToolCall, ...]:
        """D13: the psycopg call is synchronous and runs in a thread, like every other.

        `admin` is not in the query and there is nothing narrower for it to scope: the
        table carries no tenant (see `ports/audit_reader.py`). Holding an `AdminIdentity`
        at all IS the authorisation, and the type is the wall - a `CallerIdentity` cannot
        be widened into one, so no chat caller reaches this method by any code path
        (CLAUDE.md non-negotiable #9).
        """
        return await asyncio.to_thread(self._tool_calls_sync, turn_id)

    def _tool_calls_sync(self, turn_id: TurnId) -> tuple[AuditedToolCall, ...]:
        with self.connect() as connection:
            rows: Sequence[Any] = connection.execute(
                _SELECT_TOOL_CALLS, (str(turn_id),)
            ).fetchall()
        return tuple(_row_to_call(row) for row in rows)


def _row_to_call(row: Sequence[Any]) -> AuditedToolCall:
    """One row, with the effect re-hydrated into the domain enum.

    `Effect(...)` rather than a lookup with a fallback: a value in the column that the
    enum does not know is a trail written by a version this process cannot read, and
    failing loudly here is better than rendering it as ALLOW - which is what any tolerant
    default would eventually do to a denial.
    """
    caller, tool, arguments, effect, rule_id, reason, at = row
    return AuditedToolCall(
        tool_name=str(tool),
        caller_subject_id=str(caller),
        effect=Effect(str(effect)),
        at=at if isinstance(at, datetime) else datetime.fromisoformat(str(at)),
        reason=None if reason is None else str(reason),
        rule_id=None if rule_id is None else str(rule_id),
        # Already redacted by `PgAuditSink._redact`. Not re-redacted here: two redaction
        # paths drift, and the drifting one is always the one nobody reads until an
        # incident.
        arguments=dict(arguments or {}),
    )
