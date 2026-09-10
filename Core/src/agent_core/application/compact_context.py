"""Use case: CompactContext - decide whether to compress, and commit the result.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-03
Status:  IMPLEMENTED (t-f5-03). The ladder itself is still pseudo-code, pending t-f5-04:
         this class owns WHETHER a pass runs and what is committed, never what a rung does.

SILENT-BUG AREA. Everything wrong here shows up on the BILL, not in a test.

WHY THIS IS A USE CASE AND NOT A HOOK
    Compaction must be a @DBOS.step(). That gives it, for free, everything Hermes built by
    hand: a durable lock so there is one pass per session, isolation via replay, and safe
    discard when the step retries.

    Making it a use case means the step is a three-line wrapper and all the strategy stays
    testable with fakes.

WHY `execute` IS ASYNC (D13) WHEN THE STUB'S PSEUDO-CODE READ SYNC
    Three of the four calls below are I/O: `load_history` and `save_checkpoint` are
    database round trips, and `compress` reaches a summariser model on rungs L3 and L4.
    `ports/context_engine.py` states that `compress` is the ONE awaitable member on its
    port for exactly that reason. A sync `def` here would either block the event loop for
    the length of a model call - CLAUDE.md #3's failure mode, one turn at a time - or force
    the caller to bridge, which is the cost D13 exists to avoid. The trigger stays sync
    because it is arithmetic, and this method does not await it.
"""

from __future__ import annotations

from agent_core.domain.compaction import CompactionResult, ContextState
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import SessionRef
from agent_core.ports.context_engine import ContextEngine
from agent_core.ports.conversation_store import ConversationStore


class CompactContext:
    def __init__(self, *, context: ContextEngine, store: ConversationStore) -> None:
        self._context = context
        self._store = store

    async def execute(
        self, session: SessionRef, profile: AgentProfile, state: ContextState
    ) -> CompactionResult | None:
        """Run at most ONE compaction pass. Returns None when no compaction was needed.

        The return value is deliberately three-valued, because the caller acts differently
        on each: `None` means the trigger did not fire, a result with `made_progress` True
        means the history shrank and the checkpoint is durable, and a result with
        `made_progress` False means the ladder is exhausted and the caller must escalate
        or end the turn rather than ask again.
        """
        # STEP 1 - ASK, DO NOT GUESS. The engine owns the None-window fallback for
        # `context_window_used`; a token count of our own here would be a second trigger,
        # and two triggers that can disagree are worse than one that is occasionally wrong.
        if not self._context.should_compress(state, profile.compaction):
            return None

        # STEP 2 - LOAD, COMPRESS, COMMIT - IN THAT ORDER. `compress` does NOT mutate the
        # live conversation: it returns a result and this method commits it. That is what
        # makes a retried DBOS step safe to discard.
        history = await self._store.load_history(session)
        result = await self._context.compress(session, history, profile.compaction)

        # STEP 3 - IF NOTHING WAS FREED, STOP. There is no retry here and there must never
        # be one: a compaction that frees nothing and is repeated is an infinite loop that
        # ALSO rewrites the prompt prefix on every pass, so the provider's cache is
        # invalidated and the next request re-bills the whole prompt at full price. The
        # result is handed back unchanged for the caller to escalate on.
        #
        # No checkpoint is written on this path either, even when the ladder produced one.
        # A summary covering messages the last checkpoint already covered, that bought no
        # tokens, makes the `supersedes` chain unreadable as the record of what the agent
        # used to know - and that chain is the only such record there is.
        if not result.made_progress:
            return result

        # STEP 4 - PERSIST THE CHECKPOINT. Append-only, chained by `supersedes`; never an
        # UPDATE. `checkpoint` is None when only the free rungs (L1/L2) ran, and a pass
        # that shrank the history without paying for a summary has nothing to persist.
        if result.checkpoint is not None:
            await self._store.save_checkpoint(result.checkpoint)

        # STEP 5 - RETURN.
        return result
