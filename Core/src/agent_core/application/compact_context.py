"""Use case: CompactContext - decide whether to compress, and commit the result.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-03
Status:  PSEUDO-CODE ONLY

SILENT-BUG AREA. Everything wrong here shows up on the BILL, not in a test.

WHY THIS IS A USE CASE AND NOT A HOOK
    Compaction must be a @DBOS.step(). That gives it, for free, everything Hermes built by
    hand: a durable lock so there is one pass per session, isolation via replay, and safe
    discard when the step retries.

    Making it a use case means the step is a three-line wrapper and all the strategy stays
    testable with fakes.
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

    def execute(
        self, session: SessionRef, profile: AgentProfile, state: ContextState
    ) -> CompactionResult | None:
        """PSEUDO-CODE - implement in F5. Returns None when no compaction was needed.

        STEP 1 - ASK, DO NOT GUESS
            if not self._context.should_compress(state, profile.compaction):
                return None

            The engine owns the None-handling for `context_window_used`. Do not
            second-guess it here with a token count of your own: two triggers that
            disagree is worse than one that is occasionally wrong.

        STEP 2 - LOAD, COMPRESS, COMMIT - IN THAT ORDER
            history = self._store.load_history(session)
            result  = self._context.compress(session, history, profile.compaction)

            `compress` does NOT mutate the live conversation. It returns a result and this
            method commits it. That is what makes a retried DBOS step safe.

        STEP 3 - IF NOTHING WAS FREED, STOP
            if not result.made_progress:
                log loudly and return the result

            DO NOT RETRY. A compaction that frees nothing and is retried is an infinite
            loop that ALSO invalidates the prompt cache on every pass - the most expensive
            failure mode this system has. Escalate or end the turn.

        STEP 4 - PERSIST THE CHECKPOINT
            if result.checkpoint:
                self._store.save_checkpoint(result.checkpoint)

            Append-only, chained by `supersedes`. Never UPDATE: the chain is the only
            record of what the agent used to know.

        STEP 5 - RETURN

        THE THING THAT WILL GO WRONG, STATED PLAINLY
            Compacting often costs MORE than it saves. Every pass rewrites the prompt
            prefix and invalidates the provider's cache, so the next request re-bills the
            whole prompt at full price. Hermes ships per-exchange compaction OFF BY
            DEFAULT for exactly this reason.

            The measurable signal is `AuditSink.record_turn_end`: if cost per turn RISES
            after enabling compaction, the trigger is too low. Raise it and increase the
            target. Do not add rungs.

        THE OTHER THING THAT WILL GO WRONG
            Cutting between a tool call and its return makes the provider reject the whole
            conversation with a 400, surfacing long after the compaction that caused it.
            The pairing test belongs in F5's suite and must run against every rung.
        """
        raise NotImplementedError("F5 - docs/TASKS.md#t-f5-03")
