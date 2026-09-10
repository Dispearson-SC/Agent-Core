"""Compaction vocabulary - the ladder, the trigger, and the checkpoint.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-01
Status:  TYPES DEFINED / BEHAVIOUR PENDING

READ THIS BEFORE TOUCHING ANY COMPACTION CODE
    Hermes ships a per-exchange micro-compaction mode and it is OFF BY DEFAULT. The
    reason, verbatim from its source:

        "every pass rewrites the prompt prefix and breaks the provider prompt cache"

    COMPACTING OFTEN COSTS MORE THAN IT SAVES. Rewriting old messages invalidates the
    cached prefix, so the next request re-bills the whole prompt at full price. This is
    the classic first-implementation mistake: enable per turn, watch the bill rise, fail
    to connect the two. It fails NO test. CLAUDE.md lists it as a silent-bug area.

    Design rule that follows: compact RARELY and in LARGE steps. High trigger, aggressive
    target, never per turn.

DIVISION OF LABOUR
    Pydantic AI supplies the MECHANICS (a history processor wrapped as a `ProcessHistory`
    capability, which works with any provider). This file and `ContextEngine` supply the
    STRATEGY. The strategy is where money is won or lost. docs/ARCHITECTURE.md#5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from agent_core.domain.turn import SessionRef


class Rung(IntEnum):
    """The ladder, cheapest first. Climb a rung only when the previous one was not enough.

    L1_PRUNE_TOOL_OUTPUT  Replace old tool results with a stub referencing the original
                          on disk. Usually the bulk of the volume and the least
                          information lost. NO model call.
    L2_SLIDING_WINDOW     Keep the head (system prompt + first exchanges, which define
                          the task) and a tail of the last N tokens. NO model call.
    L3_SUMMARISE_MIDDLE   Condense the middle with a cheap model. Head and tail intact.
    L4_ITERATIVE_RESUMMARY  Fold the aging summary into a new one together with newly
                          aged exchanges. THIS is what stops the summary from freezing:
                          the old summary is an INPUT to the new one, so information is
                          refined rather than duplicated.
    """

    L1_PRUNE_TOOL_OUTPUT = 1
    L2_SLIDING_WINDOW = 2
    L3_SUMMARISE_MIDDLE = 3
    L4_ITERATIVE_RESUMMARY = 4


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Per-profile compaction strategy. Lives on `AgentProfile`.

    `trigger_fraction` defaults high on purpose. See the header: a low trigger is the
    expensive mistake, and it looks like it is working right up until the invoice.

    `target_fraction` is aggressive so each pass buys many turns of headroom. Compacting
    from 0.75 down to 0.72 is the worst of both worlds - full cache invalidation for
    almost no room.

    `head_exchanges` protects the opening of the conversation, where the task is defined.
    Summarising the task definition is how an agent forgets what it was asked to do."""

    trigger_fraction: float = 0.75
    target_fraction: float = 0.40
    head_exchanges: int = 2
    tail_tokens: int = 8_000
    enabled_rungs: tuple[Rung, ...] = (
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L3_SUMMARISE_MIDDLE,
        Rung.L4_ITERATIVE_RESUMMARY,
    )
    summariser_model: str | None = None  # None -> use the profile's own model


@dataclass(frozen=True, slots=True)
class ContextState:
    """What `should_compress` decides from.

    `window_used` mirrors `Usage.context_window_used` and IS OFTEN None. `estimated_tokens`
    is the mandatory local fallback: with a provider that does not report usage, a
    None-only trigger never fires and the agent dies of context overflow. That failure
    appears in production, never in tests. docs/ARCHITECTURE.md#5."""

    session: SessionRef
    window_used: float | None
    estimated_tokens: int
    context_window: int
    message_count: int
    passes_so_far: int = 0


@dataclass(frozen=True, slots=True)
class CompactionCheckpoint:
    """A persisted summary. Stored by `ConversationStore`, NOT recomputed on load -
    recomputing pays twice for the same work.

    `supersedes` chains checkpoints so L4 can find the previous summary to fold in, and
    so an operator can reconstruct what was dropped and when."""

    checkpoint_id: str
    session: SessionRef
    summary: str
    covers_through_message: int
    tokens_before: int
    tokens_after: int
    rungs_applied: tuple[Rung, ...]
    created_at: datetime | None = None
    supersedes: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """What `ContextEngine.compress` returns. It does NOT mutate the live conversation -
    the caller commits it. That separation is what lets compaction run inside a DBOS step
    and be discarded safely if the step is retried.

    `checkpoint` is None when only free rungs (L1/L2) ran and no summary was produced."""

    compacted_history: object  # provider-shaped message list; adapters narrow this
    checkpoint: CompactionCheckpoint | None
    rungs_applied: tuple[Rung, ...]
    tokens_before: int
    tokens_after: int

    @property
    def made_progress(self) -> bool:
        """False means the ladder ran and freed nothing.

        TODO(F5): the caller MUST NOT retry on False. A compaction that frees nothing and
        is retried is an infinite loop that also invalidates the prompt cache on every
        pass - the most expensive possible failure mode. Escalate or fail the turn.
        """
        return self.tokens_after < self.tokens_before
