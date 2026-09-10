"""Driven adapter: ConversationStore over Postgres.

Phase:   F1 (history) / F5 (checkpoints) / F10 (reasoning)
Tasks:   docs/TASKS.md#t-f1-13, docs/TASKS.md#t-f1-18, docs/TASKS.md#t-f10-04
Implements: ports/conversation_store.py
Status:  load_history(), append_request(), append_reasoning() and load_reasoning() DONE.
         Everything else PSEUDO-CODE, out of those tasks' scope.

TABLES
    turns              (turn_id pk, session_id, tenant_id, profile_id, state,
                        profile_version, profile_snapshot, ...)
    messages           (id bigserial, session_id, seq, role, content jsonb, ...)
    checkpoints        (checkpoint_id pk, session_id, summary, covers_through_message,
                        supersedes, created_at)
    turn_reasoning     (id bigserial, turn_id, session_id, tenant_id, content,
                        content_sha256, created_at)  - reasoning_migration.py, 0015

REASONING IS STORED APART FROM THE CONVERSATION (t-f10-04)
    `domain/transcript.py` answers REASONING as `USER: False, ADMIN: True`, and
    `docs/FIELD-NOTES.md` records that MiniMax M3 returns `reasoning_content` in BOTH
    modes - so every turn produces a block the user must never read.

    That is why the block does not become a `messages` row. `messages` is what
    `load_history` returns: the model's conversation, and the raw material of the USER
    transcript. A reasoning row there would sit one forgotten WHERE clause away from the
    person it speculated about. Its own table (`turn_reasoning`) makes the rule
    structural instead of remembered - a USER read path that never names the table cannot
    leak from it - and `load_reasoning` still asks `VISIBLE_TO` rather than hard-coding
    the answer, so the grid stays the one place the cell is decided.

D20 - EVERY TURN ROW RECORDS THE INSTRUCTIONS IT RAN UNDER (t-f1-18)
    An audit six months later must answer not only what the agent did but what it had
    been told to do. `profile_version` alone does not answer it - a version number is
    only a pointer, and the file it points at may have been edited, reverted or deleted
    since. So the row stores BOTH: the monotonic version (`domain/profile.py`,
    `ProfileVersionRegistry`) and the resolved profile rendered as JSON.

    Two consequences this adapter enforces rather than assumes:

    - A profile whose `version` is still 0 never passed through a registry, so its number
      is unverifiable. Writing it would record a fact nobody can check, which is the exact
      hole D20 exists to close - `append_request` refuses instead.
    - The snapshot is rendered at append time into plain JSON values. It is a copy, not a
      reference: editing the YAML afterwards produces a NEW version for the NEXT turn and
      cannot reach a row that is already written.

load_history() PSEUDO-CODE
    1. latest checkpoint for the session
    2. messages WHERE seq > checkpoint.covers_through_message  (all of them if none)
    3. return [summary message] + [recent messages]

    That concatenation IS compaction from the model's point of view. Step 1 is F5
    (`latest_checkpoint` is still a stub below); t-f1-13 implements the F1 base case,
    which is step 2 with no checkpoint applied - every message for the session, in
    `seq` order.

INDEX YOU WILL WISH YOU HAD ON DAY ONE
    (session_id, seq) - load_history runs on every single turn. Without it, the query
    degrades quietly as conversations grow, and it presents as "the agent got slower",
    which nobody traces back to a missing index. `_LOAD_HISTORY_SQL` below is the exact
    query shape the migration's `ix_messages_session_seq` index was built for - see
    `Core/tests/integration/test_conversation_repository.py`, which forces a plan against
    this same SQL text to prove the index is actually used, not merely present.

APPEND-ONLY FOR CHECKPOINTS
    Never UPDATE a checkpoint; chain via `supersedes`. The chain is the only record of
    what the agent used to know.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration
from agent_core.domain.compaction import CompactionCheckpoint
from agent_core.domain.profile import AgentProfile
from agent_core.domain.transcript import (
    VISIBLE_TO,
    Audience,
    EntryKind,
    TranscriptEntry,
)
from agent_core.domain.turn import SessionRef, TurnId, TurnOutcome, TurnRequest

# WHERE (session_id) + ORDER BY (seq) - the exact two columns `ix_messages_session_seq`
# covers (migrations.py, migration 0002_messages). Kept as a module constant, not
# inlined, so the integration test can force a plan against these exact bytes instead of
# a hand-copied approximation that could drift from what actually runs.
_LOAD_HISTORY_SQL = """
    SELECT role, content
    FROM messages
    WHERE session_id = %s
    ORDER BY seq ASC
"""

# D20's two columns on `turns`. Expressed as a `Migration` so that the owner of
# migrations.py (docs/TASKS.md#t-f1-16) can append this object to APP_MIGRATIONS
# verbatim; because it is tracked in schema_migrations under its own id, doing so is then
# a no-op on any database that already ran it through `apply_profile_snapshot_migration`.
#
# The columns are added nullable and then SET NOT NULL, deliberately. Adding them NOT NULL
# with a default would need a default, and the only available default is a version nobody
# assigned - a backfilled lie in the one table whose value is that it does not lie. If the
# table already holds rows, SET NOT NULL fails loudly and an operator decides what those
# rows should say; it never invents an answer.
PROFILE_SNAPSHOT_MIGRATION = Migration(
    id="0009_turns_profile_snapshot",
    sql="""
    ALTER TABLE turns ADD COLUMN IF NOT EXISTS profile_version BIGINT;
    ALTER TABLE turns ADD COLUMN IF NOT EXISTS profile_snapshot JSONB;
    ALTER TABLE turns ALTER COLUMN profile_version SET NOT NULL;
    ALTER TABLE turns ALTER COLUMN profile_snapshot SET NOT NULL;
    """,
)

_INSERT_TURN_SQL = """
    INSERT INTO turns (
        turn_id, session_id, tenant_id, profile_id, state, profile_version, profile_snapshot
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

# One statement, so `seq` is read and written under the same lock rather than being
# computed in Python and raced. D19 gives one turn at a time per session
# (partition_concurrency=1, keyed by session id), but a store must not depend on the
# caller's concurrency model to stay correct.
_APPEND_USER_MESSAGE_SQL = """
    INSERT INTO messages (session_id, seq, role, content)
    SELECT %s, COALESCE(MAX(seq), 0) + 1, 'user', %s
    FROM messages
    WHERE session_id = %s
"""

# ON CONFLICT DO NOTHING against the (turn_id, content_sha256) constraint, so a DBOS
# step re-executed after a crash re-inserts nothing rather than giving an operator a
# transcript in which the agent appears to have had the same thought twice. See
# reasoning_migration.py for why the key is a content hash and not a sequence number.
_INSERT_REASONING_SQL = """
    INSERT INTO turn_reasoning (turn_id, session_id, tenant_id, content, content_sha256)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT uq_turn_reasoning_turn_content DO NOTHING
"""

# Both halves of the session identity are in the predicate. Scoping by session_id alone
# would let a guessed or reused id read another tenant's reasoning - the most sensitive
# row in the transcript - and `ports/transcript_reader.py` states the same rule for the
# projection: the tenant is in the query, never applied afterwards.
_LOAD_REASONING_SQL = """
    SELECT id, turn_id, content, created_at
    FROM turn_reasoning
    WHERE session_id = %s AND tenant_id = %s
    ORDER BY created_at ASC, id ASC
"""

_TURN_STATE_STARTED = "started"


class UnversionedProfileError(ValueError):
    """A turn was about to be persisted under a profile whose version is not verifiable.

    Either the composition root never handed this store the profile, or the profile never
    passed through `ProfileVersionRegistry` and still carries version 0. Both mean the
    turn row could only claim a version it cannot prove, and D20's whole value is that
    the number on the row can be trusted."""


def _jsonable(value: object) -> object:
    """Render a resolved domain value as something `json.dumps` accepts.

    Not `dataclasses.asdict`: the profile carries `Decimal` budgets, `Enum` members and
    `frozenset` fields, none of which survive JSON, and asdict would hand them straight
    to the encoder. Decimals become strings rather than floats because a budget that
    silently rounds in the audit trail is worse than one that is slightly awkward to read.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (frozenset, set)):
        # Sorted so that two renders of the same profile are byte-identical; a snapshot
        # that reorders between runs looks like a change that never happened.
        return sorted(
            (_jsonable(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def profile_snapshot(profile: AgentProfile) -> dict[str, object]:
    """The resolved profile as plain JSON values, including `version` and `content_hash`.

    This is what makes the stored row self-sufficient: a reader reproduces the persona,
    the budgets and the approval rules from the row itself and never has to trust that the
    file on disk today is the file that ran."""
    rendered = _jsonable(profile)
    if not isinstance(rendered, dict):  # pragma: no cover - AgentProfile is a dataclass
        raise TypeError(f"profile rendered as {type(rendered).__name__}, expected a mapping")
    return rendered


def _apply_profile_snapshot_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (PROFILE_SNAPSHOT_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PROFILE_SNAPSHOT_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PROFILE_SNAPSHOT_MIGRATION.id,)
        )


async def apply_profile_snapshot_migration(app_conninfo: str) -> None:
    """Apply `PROFILE_SNAPSHOT_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after run_migrations because that is what creates `turns` and the
    `schema_migrations` tracking table this reads. Idempotent by the same tracking, and
    forward-only: it adds columns and never drops or rewrites one.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_profile_snapshot_migration_sync, app_conninfo)


class PgConversationStore:
    """Postgres adapter for `ports.conversation_store.ConversationStore`.

    D13: every port method is async; the psycopg calls underneath are synchronous
    (psycopg has no coroutine transaction support) and are wrapped in
    `asyncio.to_thread`, per the port's own module docstring.
    """

    def __init__(
        self, conninfo: str, profiles: Mapping[str, AgentProfile] | None = None
    ) -> None:
        """`profiles` maps profile id -> the resolved, version-assigned profile in force.

        The composition root owns loading and versioning (`ProfileVersionRegistry`); this
        store only records what it is given. It is optional so that a caller exercising
        history alone need not build one, but `append_request` refuses without it - see
        `UnversionedProfileError`."""
        self._conninfo = conninfo
        self._profiles: Mapping[str, AgentProfile] = {} if profiles is None else profiles

    async def load_history(self, session: SessionRef) -> object:
        """F1 base case: every message for the session, in `seq` order.

        No checkpoint folding yet - `latest_checkpoint` is an F5 stub below, so there is
        nothing to fold. The F5 extension of this method adds step 1 of the module
        docstring's pseudo-code on top of this query unchanged.
        """
        return await asyncio.to_thread(self._load_history_sync, session)

    def _load_history_sync(self, session: SessionRef) -> list[dict[str, object]]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(_LOAD_HISTORY_SQL, (session.session_id,)).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    async def append_request(self, turn_id: TurnId, request: TurnRequest) -> None:
        """Persist the turn row and the inbound message BEFORE the model runs.

        The turn row carries `profile_version` and the resolved snapshot (D20,
        docs/TASKS.md#t-f1-18), so the record shows the instructions this turn ran under
        and keeps showing them after the YAML changes.

        The version is resolved BEFORE the thread hop so an unversioned profile raises
        here, on the caller's stack, rather than inside a worker thread where the
        traceback no longer names the turn.
        """
        profile = self._resolve_profile(request.profile_id)
        await asyncio.to_thread(self._append_request_sync, turn_id, request, profile)

    def _resolve_profile(self, profile_id: str) -> AgentProfile:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise UnversionedProfileError(
                f"no resolved profile for id {profile_id!r}: a turn cannot be persisted "
                "without the profile that served it"
            )
        if profile.version < 1:
            raise UnversionedProfileError(
                f"profile {profile_id!r} carries version {profile.version}: it never "
                "passed through ProfileVersionRegistry, so the number is unverifiable"
            )
        return profile

    def _append_request_sync(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile
    ) -> None:
        # One transaction (no autocommit): a turn row without its message, or the reverse,
        # is a half-recorded turn, and the ordering rule this method exists to serve is
        # only worth anything if what it writes is whole.
        with psycopg.connect(self._conninfo) as conn:
            conn.execute(
                _INSERT_TURN_SQL,
                (
                    turn_id,
                    request.session.session_id,
                    request.session.tenant_id,
                    request.profile_id,
                    _TURN_STATE_STARTED,
                    profile.version,
                    json.dumps(profile_snapshot(profile)),
                ),
            )
            conn.execute(
                _APPEND_USER_MESSAGE_SQL,
                (
                    request.session.session_id,
                    json.dumps(_jsonable(request.input)),
                    request.session.session_id,
                ),
            )

    async def append_reasoning(
        self, turn_id: TurnId, session: SessionRef, content: str
    ) -> None:
        """Persist one reasoning block for this turn. ADMIN-only from the moment it lands.

        Deliberately NOT part of `ports.conversation_store.ConversationStore`. Nothing in
        `application/` reads reasoning, and a use case that could is a use case that could
        leak it; the writer is the runner adapter and the reader is the transcript
        projection, both on this side of the port. Adding it to the Protocol would also
        make every fake owe an implementation of a method no use case calls.

        Idempotent under a retried DBOS step, settled in the database rather than here
        (see reasoning_migration.py). Postgres transactions are sync (D13); the blocking
        work runs in a thread.
        """
        await asyncio.to_thread(self._append_reasoning_sync, turn_id, session, content)

    def _append_reasoning_sync(
        self, turn_id: TurnId, session: SessionRef, content: str
    ) -> None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with psycopg.connect(self._conninfo) as conn:
            conn.execute(
                _INSERT_REASONING_SQL,
                (turn_id, session.session_id, session.tenant_id, content, digest),
            )

    async def load_reasoning(
        self, session: SessionRef, audience: Audience
    ) -> tuple[TranscriptEntry, ...]:
        """This session's reasoning blocks, oldest first, as `REASONING` entries.

        The audience question is asked of `VISIBLE_TO`, not answered here. An
        `if audience is Audience.USER` would be a second copy of a decision
        `domain/transcript.py` already owns cell by cell, and the second copy is the one
        that drifts - silently, towards disclosure, exactly as the derived ADMIN set did
        before t-f10-01 wrote the grid out.

        The database is not touched at all for an audience that may not see the kind, so
        no row can reach a USER caller even by way of a later bug in the mapping below.
        """
        if EntryKind.REASONING not in VISIBLE_TO[audience]:
            return ()
        return await asyncio.to_thread(self._load_reasoning_sync, session)

    def _load_reasoning_sync(self, session: SessionRef) -> tuple[TranscriptEntry, ...]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(
                _LOAD_REASONING_SQL, (session.session_id, session.tenant_id)
            ).fetchall()
        return tuple(
            TranscriptEntry(
                entry_id=str(entry_id),
                turn_id=TurnId(str(turn_id)),
                kind=EntryKind.REASONING,
                at=created_at,
                payload={"content": content},
            )
            for entry_id, turn_id, content, created_at in rows
        )

    async def append_outcome(self, turn_id: TurnId, outcome: TurnOutcome) -> None:
        """PSEUDO-CODE - out of t-f1-13's scope. Persist the outcome, including a
        SUSPENDED one, so a suspended turn survives a crash."""
        raise NotImplementedError("PgConversationStore.append_outcome: pending")

    async def save_checkpoint(self, checkpoint: CompactionCheckpoint) -> None:
        """PSEUDO-CODE - F5. Append-only: INSERT, never UPDATE. Chain via
        `checkpoint.supersedes` instead of rewriting a row."""
        raise NotImplementedError("PgConversationStore.save_checkpoint: F5")

    async def latest_checkpoint(self, session: SessionRef) -> CompactionCheckpoint | None:
        """PSEUDO-CODE - F5. Most recent checkpoint row for the session, or None."""
        raise NotImplementedError("PgConversationStore.latest_checkpoint: F5")
