"""Port: ContextEngine - when and how do I compress the conversation?

Phase:      F5
Tasks:      docs/TASKS.md#t-f5-02
Adapter:    adapters/driven/context/
Status:     PROTOCOL FROZEN (t-f5-02) - the trigger carries its mandatory fallback; the
            ladder itself is still PSEUDO-CODE, pending t-f5-03 and t-f5-04.
Per-vertical: NO (the policy is per profile, the engine is not)

SILENT-BUG AREA. A wrong strategy here shows up on the BILL, never in a test.

THE ASYNC SPLIT (D13) - ONE COROUTINE, FOUR SYNC MEMBERS
    `compress` is the only awaitable member, because it is the only one that can reach a
    model: rungs L3 and L4 each cost a summariser call, and a sync `def` doing that would
    block the event loop for every other turn in the process.

    `should_compress` stays SYNC, and this is not an exception to D13. D13's rule is "no
    I/O to await", and the trigger is pure arithmetic over a `ContextState` that has
    already been gathered - exactly the reasoning that keeps `ToolPolicy.decide` sync. It
    runs after EVERY model response, so a coroutine here would buy an await point with
    nothing behind it on the hottest path this port has. `on_session_start`,
    `update_from_response` and `on_session_end` are local accounting for the same reason.

WHY THE TRIGGER HAS A BODY WHEN EVERY OTHER PORT MEMBER IS `...`
    `docs/TASKS.md#t-f5-02` says the None-window fallback is MANDATORY. A docstring cannot
    make anything mandatory, and this is a silent-bug area: an adapter that forgets the
    fallback never compacts, and against a provider that DOES report usage it looks
    perfect. So the fallback lives on the Protocol as an inherited default rather than
    being re-derived, and forgotten, once per adapter.

    It stays inside the layer rule: pure arithmetic over domain types, no import that is
    not a domain type, no I/O. An adapter with a better estimator may still override it -
    what it cannot do is inherit a hole.

THE LIFECYCLE BELOW IS BORROWED, NOT INVENTED
    It is Hermes' context-engine base class, which is production-proven. Two details in
    it are the ones people get wrong:

    - `on_session_end` fires ONLY at real session boundaries - explicit close, reset,
      expiry. NEVER per turn. Hermes states this in its own docstring, which means
      somebody once wired it per turn.
    - `compress` RETURNS a result and does NOT mutate the live conversation. That
      separation is what lets compaction run inside a DBOS step and be discarded safely
      when the step retries.

CONCURRENCY AND DURABILITY COME FREE HERE
    Hermes runs compression over a deep copy, publishes only on an admitted commit fence,
    holds a durable lock so there is one pass per session, and lets sessions run
    concurrently. That is several hundred lines of machinery.

    We get all of it by making compaction a @DBOS.step(). The lock, the isolation and the
    crash recovery are DBOS's job. This is the single place where the stack choice pays
    the most - do not reimplement it.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.compaction import CompactionPolicy, CompactionResult, ContextState
from agent_core.domain.turn import SessionRef, Usage


class ContextEngine(Protocol):
    def on_session_start(self, session: SessionRef) -> None:
        """Reset per-session accounting. Cheap; called once."""
        ...

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        """Feed the latest usage in. Called after every model response.

        `usage.context_window_used` is OFTEN None. Maintain a local token estimate here so
        `should_compress` always has an answer.
        """
        ...

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        """The most consequential ten lines in the project. SYNC (D13) - pure arithmetic.

        TOTAL BY CONSTRUCTION. Every `ContextState` gets a real `bool`, including the two
        inputs that have no fraction to compare:

        * `window_used is None` - the COMMON case, not an edge one. The provider simply
          did not report usage. Fall back to the local estimate the engine has been
          maintaining in `update_from_response`. NEVER answer False just because the
          provider was silent: that is the bug that kills the agent in production and
          cannot be reproduced against a provider that reports usage.
        * `context_window <= 0` - the window SIZE is unknown, so the fallback has no
          divisor and there is no fraction to compute at all. The answer is the one
          `domain.compaction.target_tokens` already chose for this exact input: resolve
          towards compaction "rather than stop early against a number nobody knows". A
          trigger that disagreed with the ladder's own rule would leave the ladder to be
          climbed only when a contradicting trigger happened to fire.

        The boundary is `policy.trigger_fraction`, never a constant: it is per profile,
        and `domain/compaction.py` explains why its default is high.

        DO NOT add "compress a little every turn". Compacting often costs MORE than it
        saves: every pass rewrites the prompt prefix and invalidates the provider's cache,
        so the next request re-bills the whole prompt at full price. Hermes ships exactly
        that mode OFF BY DEFAULT for this reason. See domain/compaction.py.
        """
        fraction = state.window_used
        if fraction is None:
            if state.context_window <= 0:
                return True
            fraction = state.estimated_tokens / state.context_window
        return fraction >= policy.trigger_fraction

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        """PSEUDO-CODE - F5. The ladder, cheapest rung first, stop when the target is met.

        ASYNC (D13): the only awaitable member on this port, and the reason the other four
        stay sync. L3 and L4 each cost a summariser model call, so this is the one member
        with I/O behind its await.

        target_tokens = policy.target_fraction * context_window

        L1 PRUNE TOOL OUTPUT (free)
            Replace old tool results with a stub referencing the stored original. Usually
            the bulk of the volume. Never prune the most recent exchange's results.

        L2 SLIDING WINDOW (free)
            Protect the head (policy.head_exchanges - the task definition lives there) and
            a tail of policy.tail_tokens. Drop nothing from either.

        L3 SUMMARISE MIDDLE (one cheap model call)
            Summarise between head and tail using policy.summariser_model.

        L4 ITERATIVE RE-SUMMARY (one cheap model call)
            Feed the PREVIOUS checkpoint's summary in as an input alongside the newly aged
            exchanges. This is what keeps the summary current instead of frozen, and what
            stops it duplicating what it already said.

        INVARIANT THAT BREAKS THE CONVERSATION (CLAUDE.md #5)
            Cut ONLY at complete-exchange boundaries. A tool call separated from its
            return makes the provider reject the whole conversation with a 400 - and it
            surfaces much later, far from the compaction that caused it. Write a test that
            generates tool-bearing histories and asserts pairing after EVERY rung.

        IF NOTHING WAS FREED
            Return the result with made_progress False. The caller must NOT retry: a
            compaction that frees nothing and is retried is an infinite loop that also
            invalidates the cache on every pass. Escalate or end the turn.
        """
        ...

    def on_session_end(self, session: SessionRef) -> None:
        """Real session boundaries ONLY - close, reset, expiry. NEVER per turn."""
        ...
