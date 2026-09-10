"""Port: ConversationStore - where does the history and its checkpoints live?

Phase:      F1 (history) / F5 (checkpoints)
Tasks:      docs/TASKS.md#t-f1-08
Adapter:    adapters/driven/persistence_pg/conversation_repository.py
Per-vertical: NO

THE ORDERING RULE THAT MAKES CRASHES SURVIVABLE
    The tool-call turn is persisted BEFORE any side effect runs. If the process dies
    mid-`terminal` or mid-`write_file`, the session can still be reconstructed and the
    operator can see what was in flight.

    Hermes does this explicitly in its turn loop and it is one of the few things worth
    copying verbatim. Persisting after the fact means a crash leaves a side effect in the
    world with no record that it was ever attempted.

WHY CHECKPOINTS ARE STORED AND NOT RECOMPUTED
    A compaction summary costs a model call. Recomputing it on every load pays twice for
    the same work, and worse, produces a DIFFERENT summary each time - so the agent's
    memory of the conversation changes depending on when you happen to load it.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.compaction import CompactionCheckpoint
from agent_core.domain.turn import SessionRef, TurnId, TurnOutcome, TurnRequest


class ConversationStore(Protocol):
    def load_history(self, session: SessionRef) -> object:
        """Return the provider-shaped message list for this session.

        PSEUDO-CODE - F1, extended in F5:
        1. Fetch the latest CompactionCheckpoint, if any.
        2. Fetch messages AFTER `checkpoint.covers_through_message`.
        3. Return [summary message] + [recent messages].

        That concatenation is the whole point of compaction: the agent gets the summary
        plus the recent tail instead of the entire history.
        """
        ...

    def append_request(self, turn_id: TurnId, request: TurnRequest) -> None:
        """Persist the inbound turn. Called BEFORE the model is invoked."""
        ...

    def append_outcome(self, turn_id: TurnId, outcome: TurnOutcome) -> None:
        """Persist the outcome, including a SUSPENDED one.

        A suspended turn must be durable: the whole point is that the process may die
        while a human takes three days to answer.
        """
        ...

    def save_checkpoint(self, checkpoint: CompactionCheckpoint) -> None:
        """PSEUDO-CODE - F5.

        Append-only. Never UPDATE a checkpoint: chain via `supersedes` instead, so an
        operator can reconstruct what was dropped and when. Overwriting destroys the only
        record of what the agent used to know.
        """
        ...

    def latest_checkpoint(self, session: SessionRef) -> CompactionCheckpoint | None:
        """Most recent checkpoint, or None. L4 folds this into the next summary."""
        ...
