"""Driven adapter: ConversationStore over Postgres.

Phase:   F1 (history) / F5 (checkpoints)
Tasks:   docs/TASKS.md#t-f1-13
Implements: ports/conversation_store.py

TABLES
    turns              (turn_id pk, session_id, tenant_id, profile_id, caller, state, ...)
    messages           (id bigserial, session_id, seq, role, content jsonb, ...)
    checkpoints        (checkpoint_id pk, session_id, summary, covers_through_message,
                        supersedes, created_at)

load_history() PSEUDO-CODE
    1. latest checkpoint for the session
    2. messages WHERE seq > checkpoint.covers_through_message  (all of them if none)
    3. return [summary message] + [recent messages]

    That concatenation IS compaction from the model's point of view.

INDEX YOU WILL WISH YOU HAD ON DAY ONE
    (session_id, seq) - load_history runs on every single turn. Without it, the query
    degrades quietly as conversations grow, and it presents as "the agent got slower",
    which nobody traces back to a missing index.

APPEND-ONLY FOR CHECKPOINTS
    Never UPDATE a checkpoint; chain via `supersedes`. The chain is the only record of
    what the agent used to know.
"""
