"""Driven adapter: ConversationStore over Postgres.

Phase:   F1 (history) / F5 (checkpoints) / F10 (reasoning) / F11 (the agent's half)
Tasks:   docs/TASKS.md#t-f1-13, docs/TASKS.md#t-f1-18, docs/TASKS.md#t-f10-04,
         docs/TASKS.md#t-f1-22, docs/TASKS.md#t-f11-29
Implements: ports/conversation_store.py
Status:  load_history(), append_messages(), append_request(), append_reasoning(),
         load_reasoning() and append_outcome() DONE. save_checkpoint() and
         latest_checkpoint() remain PSEUDO-CODE, F5's scope.

HOW A MESSAGE IS ENCODED, AND WHY IT IS NOT OUR FORMAT (t-f1-24)
    `messages.content` holds ONE Pydantic AI `ModelMessage`, serialised by Pydantic AI's
    own `ModelMessagesTypeAdapter`. `messages.role` keeps the vocabulary it already had -
    'user' / 'assistant' - because `transcript_migration.py`'s `transcript_entries` view
    branches on that exact string to decide USER_MESSAGE versus AGENT_MESSAGE. The
    encoding's own discriminator is the `kind` INSIDE the payload, which is where the
    library keeps it and where `decode_message` reads it from; the column is not a second
    copy of that answer.

    WHAT A TRANSCRIPT READER NOW SEES IN `payload.content`, WHICH IS A REAL CONSEQUENCE
        The view projects `content` verbatim, so a transcript entry's payload carries a
        serialised `ModelMessage` rather than `{"text": ...}`. That is strictly more than
        it carried before and nothing user-visible reads a field that moved - but a
        `ModelResponse` can carry a `ThinkingPart`, and reasoning is ADMIN-only
        (t-f10-04). This paragraph used to end "nothing writes a `ModelResponse` to this
        table today"; `t-f11-29` is the anchor that made one, so the guard it asked for is
        now `without_reasoning` below.

    THE PROJECTION IS NAMED AT THE WRITE, NOT AT THE READ (t-f11-29)
        `without_reasoning` runs on every message this module stores, so a `ThinkingPart`
        never becomes part of a `messages` row at all. That is deliberately the write side
        rather than a narrowed view: `transcript_entries` is one reader, `load_history` is
        another, and anybody may add a third - a rule enforced once per reader is a rule
        that leaks through the reader somebody forgets. A USER read path cannot disclose
        what was never written to the table it reads, which is the same structural argument
        `turn_reasoning` is built on and the same one CLAUDE.md non-negotiable #8 makes
        about a tool that does not exist.

        Reasoning is not lost by this: `append_reasoning` below is its write path and
        `turn_reasoning` is where it lives, ADMIN-only from the moment it lands.

    THIS COLUMN USED TO HOLD A DIFFERENT THING AND NOTHING NOTICED
        It held `{"role": "user", "content": {"text": ...}}`, an OpenAI-wire shape no
        reader in this tree consumes. `PydanticAgentRunner._as_message_history` accepts
        `ModelMessage` values and refuses everything else by name; so do the compaction
        ladder, `_unpaired` and `_pending_tool_calls`. `ports/conversation_store.py` says
        `load_history` returns "the provider-shaped message list" and D7 says this port
        MIRRORS Pydantic AI's vocabulary rather than inventing a parallel one. The store
        was the only module speaking a third language, and the first turn of the operator
        console was the first code in the build ever to read what it had written.

    WHAT pydantic-ai 2.31.1 ACTUALLY SHIPS (checked, not assumed)
        `pydantic_ai.messages.ModelMessagesTypeAdapter` is a `TypeAdapter[list[
        ModelMessage]]` - the plural is the only one there is, so a single message is
        dumped and validated as a one-element list. `dump_python(..., mode="json")`
        yields plain JSON values psycopg can hand to a `jsonb` column, and
        `validate_python` reverses it exactly: a `ToolCallPart` and the `ToolReturnPart`
        answering it come back with the same `tool_call_id`, byte for byte, which is what
        CLAUDE.md non-negotiable #5 depends on. There is deliberately no hand-written
        codec here: a private encoder for a third-party message type breaks silently on
        upgrade, and the library already owns the round trip it is asked to guarantee.

    REASONING IS STILL NOT A MESSAGE (t-f10-04, and see below)
        A `ThinkingPart` is ADMIN-only and lives in `turn_reasoning`. Nothing writes one
        here, and `without_reasoning` is now what makes that true rather than the fact that
        nothing yet wrote a `ModelResponse`: `messages` is what `load_history` returns to
        the model and the raw material of the USER transcript.

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
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import psycopg
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    UserPromptPart,
)

from agent_core.adapters.driven.persistence_pg.migrations import Migration
from agent_core.domain.compaction import CompactionCheckpoint
from agent_core.domain.profile import AgentProfile
from agent_core.domain.transcript import (
    VISIBLE_TO,
    Audience,
    EntryKind,
    TranscriptEntry,
)
from agent_core.domain.turn import (
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    UserInput,
)

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
#
# `role` is the message's own `kind`, supplied rather than hard-coded: a response row is
# the same row shape as a request row, and a literal 'user' here is how the column came to
# disagree with what `content` actually held.
_APPEND_MESSAGE_SQL = """
    INSERT INTO messages (session_id, seq, role, content)
    SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s
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
_TURN_STATE_SUSPENDED = "suspended"
_TURN_STATE_FINISHED = "finished"

# An UPDATE, not an INSERT: the row was already created by `append_request` (step 2 of
# StartTurn always runs before step 7). Being an UPDATE is also what makes a repeated
# append_outcome for the same turn_id idempotent for free - it overwrites the one row
# rather than ever adding a second, with no ON CONFLICT clause needed.
_APPEND_OUTCOME_SQL = """
    UPDATE turns
    SET state = %s, pending = %s, result = %s, finished_at = %s
    WHERE turn_id = %s
"""

# Which conversation this turn is in - the one thing `TurnOutcome` cannot answer, because
# it names a turn and `messages` rows are keyed by session. Read from the row
# `append_request` already wrote rather than adding a seat to the port, so there stays ONE
# answer to the question.
#
# FOR UPDATE because what follows is a check-then-act (read the tail, then append). Without
# the lock two concurrent calls both find nothing stored and both append. D19 gives one turn
# at a time per session, but a store must not depend on the caller's concurrency model to
# stay correct.
_SELECT_TURN_FOR_OUTCOME_SQL = """
    SELECT session_id, tenant_id
    FROM turns
    WHERE turn_id = %s
    FOR UPDATE
"""

# The last N messages of a session, oldest first. The inner ORDER BY ... DESC LIMIT is what
# uses `ix_messages_session_seq`; the outer one only puts the window back in reading order.
#
# WHY THE REPLAY GUARD IS THE CONTENT AND NOT THE TURN'S STATE (t-f11-29)
#     `append_outcome` was idempotent for free while it only UPDATEd one row; appending is
#     not. The obvious guard - "skip unless the turn is still `started`" - is WRONG, and
#     wrong in the silent direction: a suspended turn is written once by `StartTurn` and
#     again by `ResumeTurn` when the human finally answers, with DIFFERENT messages the
#     second time. A state check would have dropped every resumed turn's tool return and
#     every answer the agent gave after it, and the only symptom would have been a
#     conversation that forgets the thing somebody waited three days for.
#
#     The question that actually separates the two cases is whether THESE messages are
#     already there. A `ModelResponse` carries its own `timestamp`, so two genuinely
#     separate replies never encode identically even when the model says the same words -
#     which is what makes the comparison a replay test rather than a repetition test.
_TAIL_MESSAGES_SQL = """
    SELECT content
    FROM (
        SELECT seq, content
        FROM messages
        WHERE session_id = %s
        ORDER BY seq DESC
        LIMIT %s
    ) AS tail
    ORDER BY seq ASC
"""


class UnversionedProfileError(ValueError):
    """A turn was about to be persisted under a profile whose version is not verifiable.

    Either the composition root never handed this store the profile, or the profile never
    passed through `ProfileVersionRegistry` and still carries version 0. Both mean the
    turn row could only claim a version it cannot prove, and D20's whole value is that
    the number on the row can be trusted."""


# What `messages.role` says for each side of a conversation. NOT Pydantic AI's own `kind`
# ('request'/'response'), and the difference is load-bearing rather than cosmetic:
# `transcript_migration.py`'s `transcript_entries` view reads this exact column -
# `CASE WHEN m.role = 'user' THEN 'user_message' ELSE 'agent_message' END` - so a row
# written under any other word becomes an AGENT message in the USER-facing transcript.
# Writing 'request' here would have relabelled every customer's own sentence as something
# the agent said, on a read path with no test between it and a person.
#
# Nothing needs the column to discriminate the ENCODING: `decode_message` reads the `kind`
# inside the payload, which is where the library keeps it.
_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"


def without_reasoning(message: ModelMessage) -> ModelMessage | None:
    """The message with every `ThinkingPart` removed, or None when nothing else was in it.

    THE NAMED PROJECTION t-f10-04 ASKED FOR AND t-f11-29 MADE URGENT
        `domain/transcript.py` answers REASONING as `USER: False, ADMIN: True`, and
        `docs/FIELD-NOTES.md` records that MiniMax M3 returns reasoning in BOTH modes - so
        every turn produces a block the user must never read. `transcript_entries` projects
        `messages.content` verbatim, so the moment a `ModelResponse` reaches this table a
        `ThinkingPart` inside it is one unnamed projection away from a USER read: CLAUDE.md
        non-negotiable #11, breached through a door nobody opened deliberately.

        This function is that door, held shut at the WRITE. It names the one part kind that
        may not be stored rather than enumerating the ones that may, because the parts that
        must survive are not a list anybody can freeze: a `ToolCallPart` and the
        `ToolReturnPart` answering it are CLAUDE.md non-negotiable #5, and a part type the
        library adds next release belongs in the conversation until somebody decides it
        does not. Dropping the known-secret kind fails towards keeping the conversation
        whole; keeping an allow-list would fail towards a provider 400 nobody can trace.

    A RESPONSE THAT WAS NOTHING BUT REASONING BECOMES NO ROW AT ALL
        None, not an empty `ModelResponse`. An assistant turn with no parts is a message
        the model said nothing in - it costs a row, reads as a gap in the transcript, and
        providers differ on whether they will even accept one back. There is nothing of the
        conversation in it to preserve.

    THE UNTOUCHED MESSAGE IS RETURNED ITSELF, not a rebuilt equal one, so the common case
    cannot be where a future field is quietly dropped.
    """
    if not isinstance(message, ModelResponse):
        return message
    kept = [part for part in message.parts if not isinstance(part, ThinkingPart)]
    if len(kept) == len(message.parts):
        return message
    if not kept:
        return None
    return replace(message, parts=kept)


def encode_message(message: ModelMessage) -> tuple[str, str]:
    """One `ModelMessage` as `(role, content json)` for a `messages` row.

    Pydantic AI's adapter does the work - see HOW A MESSAGE IS ENCODED in the module
    docstring. The plural adapter is the only one the library ships, so the message goes
    in and comes out inside a one-element list; that is the library's shape, not a
    workaround for one.

    `mode="json"` rather than the default, because the value is about to be handed to
    psycopg as `jsonb`: the default mode leaves `datetime` and `Decimal` as Python
    objects, and `json.dumps` would then refuse them.

    The role is the transcript view's discriminator and keeps the vocabulary it already
    had - see `_ROLE_USER` above for why that is not a style choice.
    """
    payload = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
    role = _ROLE_USER if isinstance(message, ModelRequest) else _ROLE_ASSISTANT
    return role, json.dumps(payload)


class UnpersistableInputError(NotImplementedError):
    """A `UserInput` carries something this store has no settled encoding for.

    Today that is `media`: inbound media has no wired path at all (see the INBOUND note in
    `adapters/driven/agent_pydantic/runner.py`), so nothing produces a `UserInput` with a
    `MediaRef` on it. The day something does, a `UserPromptPart` can carry the resolved
    content alongside the text - but resolving a `MediaRef` needs `MediaStore`, which this
    store does not hold and must not grow a second copy of.

    REFUSING RATHER THAN DROPPING. Writing the text and silently discarding the reference
    would leave a transcript in which the customer never sent the photo they are asking
    about, and nothing anywhere would report it. That is the shape of defect this whole
    anchor exists to answer.
    """


def _user_message(user_input: UserInput) -> ModelMessage:
    """The inbound turn as the one message Pydantic AI prepends for it."""
    if user_input.media:
        raise UnpersistableInputError(
            f"UserInput carries {len(user_input.media)} media reference(s) and this store "
            "has no encoding for one. Persisting the text alone would lose the reference "
            "silently; see UnpersistableInputError. docs/TASKS.md#t-f1-24."
        )
    return ModelRequest(parts=[UserPromptPart(content=user_input.text)])


def _encoded_rows(messages: Sequence[ModelMessage]) -> list[tuple[str, str]]:
    """The messages as `messages` rows, with reasoning projected out and empties dropped.

    THE ONE PLACE A MESSAGE BECOMES A ROW. `without_reasoning` runs here and nowhere else,
    so every write path in this module - `append_request`, `append_messages`,
    `append_outcome` - is covered by construction rather than by three people remembering.
    """
    rows: list[tuple[str, str]] = []
    for message in messages:
        storable = without_reasoning(message)
        if storable is None:
            continue
        rows.append(encode_message(storable))
    return rows


class UnpersistableOutcomeError(TypeError):
    """`TurnOutcome.messages` carries something this store cannot encode as a message.

    The seat is typed `tuple[object, ...]` because `domain/` may not name Pydantic AI (see
    `TurnOutcome.messages`), which leaves this adapter as the one place the real type is
    known - and therefore the one place a mismatch can be reported by name instead of as an
    AttributeError inside `ModelMessagesTypeAdapter`.

    The mirror image of `runner.py::UnsupportedHistoryError`, and for the same reason: the
    two adapters agree on one encoding because each refuses anything else at its own
    boundary, not because either guesses what the other meant.
    """


class OrphanOutcomeError(LookupError):
    """An outcome carrying messages names a turn that has no row in `turns`.

    `append_request` creates that row BEFORE the model runs, so the only ways to get here
    are a caller that skipped it or a turn id that was regenerated somewhere. Both mean the
    session these messages belong to is unknown, and the alternative to raising is dropping
    the agent's half of a conversation with nothing anywhere reporting it - which is the
    defect `t-f11-29` exists to close, reintroduced one layer down.
    """


def _outcome_messages(turn_id: TurnId, outcome: TurnOutcome) -> tuple[ModelMessage, ...]:
    """`outcome.messages` as the values this store knows how to encode, or refuse by name."""
    messages: list[ModelMessage] = []
    for message in outcome.messages:
        if not isinstance(message, (ModelRequest, ModelResponse)):
            raise UnpersistableOutcomeError(
                f"the outcome of turn {turn_id!r} carries a {type(message).__name__} among "
                "its messages; this store encodes Pydantic AI ModelMessage values through "
                "ModelMessagesTypeAdapter. The runner adapter produces them "
                "(adapters/driven/agent_pydantic/runner.py::_messages_the_turn_added) and "
                "nothing between it and here may reshape one. docs/TASKS.md#t-f11-29."
            )
        messages.append(message)
    return tuple(messages)


def decode_message(payload: object) -> ModelMessage:
    """One `messages.content` value back into the `ModelMessage` it was written from.

    The stored `role` is NOT consulted. Pydantic AI discriminates on the `kind` INSIDE the
    payload, and asking the column instead would put a second answer to one question in a
    place where the two can be edited apart.
    """
    return ModelMessagesTypeAdapter.validate_python([payload])[0]


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
    if isinstance(value, datetime):
        # ISO 8601, not `str(...)`: the two agree for a timezone-aware value but not
        # always for a naive one, and a stored terminal-state timestamp must parse back
        # unambiguously rather than merely look plausible in a log line.
        return value.isoformat()
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


# t-f1-22's own migration, following PROFILE_SNAPSHOT_MIGRATION's precedent (0009, above)
# and TURN_REASONING_MIGRATION's (0015, reasoning_migration.py): a dedicated Migration
# object next to the code that needs it, rather than growing migrations.py for every
# anchor that adds a column. Id 0017 is the next free slot after 0016
# (transcript_migration.py) - see the migration-id table in docs/TASKS.md.
#
# Both columns are nullable and stay that way: I2 in domain/turn.py makes `pending` and
# `result` mutually exclusive by construction, so a SUSPENDED row legitimately has a NULL
# `result` and a FINISHED row legitimately has an empty `pending` array. There is no
# "unassigned" value to backfill the way PROFILE_SNAPSHOT_MIGRATION's SET NOT NULL guards
# against - both shapes are valid terminal states, not missing data.
TURN_OUTCOME_MIGRATION = Migration(
    id="0017_turn_outcome",
    sql="""
    ALTER TABLE turns ADD COLUMN IF NOT EXISTS pending JSONB;
    ALTER TABLE turns ADD COLUMN IF NOT EXISTS result JSONB;
    ALTER TABLE turns ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
    """,
)


def _apply_turn_outcome_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (TURN_OUTCOME_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(TURN_OUTCOME_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (TURN_OUTCOME_MIGRATION.id,)
        )


async def apply_turn_outcome_migration(app_conninfo: str) -> None:
    """Apply `TURN_OUTCOME_MIGRATION`, once, after `migrations.run_migrations`.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_turn_outcome_migration_sync, app_conninfo)


# t-f1-24's own migration, on id 0024 - the last slot docs/TASKS.md still holds free.
# Discovery in migrations.py picks this object up from the module top level, so nothing
# has to list it.
#
# 0023 WAS ALREADY SPENT, AND THE GUARD COULD NOT SEE IT. docs/TASKS.md lists 0023 as
# unallocated, but `audit_tenant_migration.py` had taken `0023_audit_tool_calls_tenant`
# without writing it there - the same "taken outside this table" defect the table records
# for 0017. `DuplicateMigrationIdError` compares WHOLE ids, so `0023_audit_tool_calls_tenant`
# and a second `0023_...` differ as strings and coexist silently; the collision the table
# exists to prevent is on the NUMBER, and nothing checks that. Reported rather than fixed
# here: the id table is docs/TASKS.md's, and this anchor does not own it.
#
# IT DELETES RATHER THAN TRANSLATES, AND THAT IS THE SAFE DIRECTION HERE
#     Rows written before this anchor hold `{"text": ..., "media": [...]}` under role
#     'user' - the shape `_as_message_history` refuses. Every one of them was written by a
#     turn that then CRASHED on reading it back, because `append_request` writes before
#     `load_history` reads; there is no session anywhere whose conversation is worth
#     preserving, and no turn ever completed on top of one.
#
#     Translating them would mean hand-writing Pydantic AI's serialised message JSON in
#     SQL - a second, unversioned copy of a third-party format, in the one place nobody
#     would think to update when the library changes it. That is the maintenance burden
#     `encode_message` exists to avoid, and it would be taken on to rescue two crashed
#     turns.
#
#     The predicate is the encoding itself, not a date or an id range: a row whose content
#     has no `kind` is not a Pydantic AI message, and after this migration every row has
#     one. Re-running it therefore deletes nothing, which is what makes it idempotent.
MESSAGE_ENCODING_MIGRATION = Migration(
    id="0024_messages_model_message_encoding",
    sql="""
    DELETE FROM messages WHERE content->>'kind' IS NULL;
    """,
)


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

    async def load_history(self, session: SessionRef) -> list[ModelMessage]:
        """F1 base case: every message for the session, in `seq` order.

        Returns Pydantic AI `ModelMessage` values, which is what the port means by "the
        provider-shaped message list" and what every reader of a history in this tree is
        typed for. See HOW A MESSAGE IS ENCODED in the module docstring for what this used
        to return instead and why nothing caught it.

        No checkpoint folding yet - `latest_checkpoint` is an F5 stub below, so there is
        nothing to fold. The F5 extension of this method adds step 1 of the module
        docstring's pseudo-code on top of this query unchanged.
        """
        return await asyncio.to_thread(self._load_history_sync, session)

    def _load_history_sync(self, session: SessionRef) -> list[ModelMessage]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(_LOAD_HISTORY_SQL, (session.session_id,)).fetchall()
        # `role` is read past deliberately: the payload carries its own discriminator.
        return [decode_message(content) for _role, content in rows]

    async def append_messages(
        self, session: SessionRef, messages: Sequence[ModelMessage]
    ) -> None:
        """Persist model messages for a session, in the order given, as one transaction.

        DELIBERATELY NOT ON `ports.conversation_store.ConversationStore`. The port speaks
        domain types, and `ModelMessage` is Pydantic AI's - putting this on the Protocol
        would drag the library into every use case and every fake, which is the one thing
        D7 says the port exists to prevent. It is the adapter's own write path, and
        `append_request` below is expressed in terms of it so there is exactly one place
        that turns a message into a row.

        ALL OF THEM OR NONE OF THEM - CLAUDE.md non-negotiable #5. A tool call and the
        return answering it are two messages, and a crash between the two would leave a
        history the provider rejects with a 400 on some later turn, far from here. One
        transaction makes that unrepresentable rather than unlikely.

        ASYNC (D13): a database write; the sync psycopg calls run in a thread.
        """
        await asyncio.to_thread(self._append_messages_sync, session, tuple(messages))

    def _append_messages_sync(
        self, session: SessionRef, messages: tuple[ModelMessage, ...]
    ) -> None:
        if not messages:
            return
        with psycopg.connect(self._conninfo) as conn:
            self._write_messages(conn, session, messages)

    def _write_messages(
        self,
        conn: psycopg.Connection[Any],
        session: SessionRef,
        messages: tuple[ModelMessage, ...],
    ) -> None:
        """The rows, on an already-open connection, so a caller can widen the transaction.

        `append_request` needs the turn row and the first message to land together, and
        `append_outcome` needs the whole of what a turn said to land with its terminal
        state - CLAUDE.md non-negotiable #5. Both are only true if the statements run on
        the same connection.

        EVERY WRITE PATH IN THIS MODULE FUNNELS THROUGH `_encoded_rows`, which is what
        makes `without_reasoning` structural rather than remembered: there is no second
        place a `ThinkingPart` could enter the table from.
        """
        self._write_rows(conn, session, _encoded_rows(messages))

    def _write_rows(
        self,
        conn: psycopg.Connection[Any],
        session: SessionRef,
        rows: Sequence[tuple[str, str]],
    ) -> None:
        """Already-encoded `(role, content)` rows, in order, on an open connection.

        Split out from `_write_messages` because `append_outcome` has to LOOK at the
        encoded form before it writes it - see `_TAIL_MESSAGES_SQL` - and encoding twice
        would invite the comparison and the insert to disagree about what a message is.
        """
        for role, content in rows:
            conn.execute(
                _APPEND_MESSAGE_SQL,
                (session.session_id, role, content, session.session_id),
            )

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
        message = _user_message(request.input)
        await asyncio.to_thread(
            self._append_request_sync, turn_id, request, profile, message
        )

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
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        message: ModelMessage,
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
            self._write_messages(conn, request.session, (message,))

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
        """Persist the turn's terminal state AND what the agent said - t-f1-22, t-f11-29.

        `StartTurn` step 7 awaits this on EVERY turn, so a suspended turn is exactly as
        durable as a finished one: the process may die while a human takes three days to
        answer, and `outcome.pending` - not just the fact that something is pending - is
        what makes the turn resumable afterwards (see `PendingRequest.tool_call_id`'s own
        docstring on why it must round-trip verbatim).

        AND THIS IS WHERE THE AGENT'S HALF OF THE CONVERSATION IS WRITTEN (t-f11-29).
        `append_request` wrote the prompt before the model ran; `outcome.messages` is
        everything the turn added after it - the model's replies, its tool calls and the
        returns answering them. Before this seat existed nothing in production wrote a
        `ModelResponse` at all, so a stored conversation was the customer's sentences and
        nothing else: compaction had half a history to compact, the transcript showed half
        an exchange, and a model asked what it had just said could not answer.

        ALL OF THEM, WITH THE STATE, IN ONE TRANSACTION - CLAUDE.md non-negotiable #5. A
        tool call and its return are two messages, and a crash between them would leave a
        history the provider rejects with a 400 on some LATER turn, far from here. There is
        no second write to fail on its own.

        ASYNC (D13): a database write; the sync psycopg call runs in a thread.
        """
        await asyncio.to_thread(self._append_outcome_sync, turn_id, outcome)

    def _append_outcome_sync(self, turn_id: TurnId, outcome: TurnOutcome) -> None:
        rows = _encoded_rows(_outcome_messages(turn_id, outcome))
        finished_at = outcome.result.finished_at if outcome.result is not None else None
        result_json = (
            json.dumps(_jsonable(outcome.result)) if outcome.result is not None else None
        )
        # No autocommit: the state, the pending blob and every message of the turn are one
        # write or none of them. See the method docstring.
        with psycopg.connect(self._conninfo) as conn:
            turn = conn.execute(_SELECT_TURN_FOR_OUTCOME_SQL, (turn_id,)).fetchone()
            conn.execute(
                _APPEND_OUTCOME_SQL,
                (
                    _TURN_STATE_SUSPENDED if outcome.is_suspended else _TURN_STATE_FINISHED,
                    json.dumps(_jsonable(outcome.pending)),
                    result_json,
                    finished_at,
                    turn_id,
                ),
            )
            if not rows:
                return
            if turn is None:
                raise OrphanOutcomeError(
                    f"turn {turn_id!r} has no row in `turns`, so the {len(rows)} "
                    "message(s) this outcome carries belong to no conversation. "
                    "append_request writes that row before the model runs "
                    "(ports/conversation_store.py); an outcome arriving without it means "
                    "the ordering rule was skipped, and storing the agent's half of a "
                    "conversation whose session is unknown would lose it silently."
                )
            session_id, tenant_id = turn
            session = SessionRef(
                session_id=SessionId(session_id), tenant_id=TenantId(tenant_id)
            )
            if self._already_appended(conn, session, rows):
                return
            self._write_rows(conn, session, rows)

    def _already_appended(
        self,
        conn: psycopg.Connection[Any],
        session: SessionRef,
        rows: Sequence[tuple[str, str]],
    ) -> bool:
        """Whether these exact rows are already the tail of this session's conversation.

        THE REPLAY GUARD, AND IT ASKS THE CONVERSATION RATHER THAN THE TURN (t-f11-29).
        `append_outcome` is called twice for one turn on the ordinary suspended path -
        once by `StartTurn` and once by `ResumeTurn` - with different messages each time,
        so anything keyed on the turn alone drops the resumed half. Comparing the content
        distinguishes "this outcome again" from "the next outcome for this turn", which is
        the question actually being asked.

        `ModelResponse` carries its own `timestamp`, so two genuinely separate replies do
        not encode identically even when the model repeats itself word for word. The
        comparison therefore tests for a REPLAY and never for a repetition.
        """
        stored = conn.execute(
            _TAIL_MESSAGES_SQL, (session.session_id, len(rows))
        ).fetchall()
        if len(stored) != len(rows):
            return False
        return all(
            json.loads(content) == payload
            for (_role, content), (payload,) in zip(rows, stored, strict=True)
        )

    async def save_checkpoint(self, checkpoint: CompactionCheckpoint) -> None:
        """PSEUDO-CODE - F5. Append-only: INSERT, never UPDATE. Chain via
        `checkpoint.supersedes` instead of rewriting a row."""
        raise NotImplementedError("PgConversationStore.save_checkpoint: F5")

    async def latest_checkpoint(self, session: SessionRef) -> CompactionCheckpoint | None:
        """PSEUDO-CODE - F5. Most recent checkpoint row for the session, or None."""
        raise NotImplementedError("PgConversationStore.latest_checkpoint: F5")
