"""Driven adapter: ContextEngine - the compaction ladder.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-04, docs/TASKS.md#t-f5-11
Status:  IMPLEMENTED (t-f5-04). L1-L4 climb; the summariser behind L3/L4 is injected.
         t-f5-11: the local estimate is CONTEXT SIZE, replaced per response and
         lowered by a successful climb - never a running total of spend.
Implements: ports/context_engine.py

SILENT-BUG AREA. Wrong here = a bigger bill, never a red test.

BUDGET: ~750 LINES. THAT NUMBER IS THE DISCIPLINE, NOT AN ASPIRATION.
    Hermes spent 11,003 lines across 8 files on this subsystem - context_compressor.py
    alone is 4,918. That gap is not bad engineering: it is the cost of chasing semantic
    deduplication, provider-native compaction, images inside history, commit fences and
    repair of broken histories.

    Treat the L1-L4 ladder as the COMPLETE day-1 scope. Do not add a rung until the bill
    or the answer quality demands it with data. Pydantic AI ships a `CompactionPart` and
    some providers compact natively; both are deliberately out of scope here for the same
    reason - one strategy that is understood beats two that interact.

THE LADDER (see domain/compaction.py for the rung semantics)
    L1 prune tool outputs to stubs                free
    L2 sliding window, protected head             free
    L3 summarise the middle with a cheap model    one call
    L4 fold the previous summary into a new one   one call

    Stop as soon as target_fraction is met. Running L3 when L1 already freed enough is
    paying for a model call that bought nothing.

WHAT EACH RUNG DOES TO A HISTORY, STATED ONCE SO THE LADDER READS AS ONE STRATEGY
    The unit is an EXCHANGE, never a message: a user turn plus every model response and
    tool return that answered it. `_split_exchanges` is what makes that unit real, and it
    is the whole implementation of CLAUDE.md #5 - see the invariant note below.

    L1  Replaces the content of old `ToolReturnPart`s with a short stub. The most recent
        exchange keeps its real results, because the model is usually still reasoning
        about them. No exchange is dropped, so pairing cannot be affected at all.
    L2  Keeps `policy.head_exchanges` at the front, keeps a tail of `policy.tail_tokens`
        (at minimum the most recent exchange, whatever it costs), and AGES everything in
        between - removed from the live history, remembered for L3.
    L3  Pays one summariser call and collapses the aged material into a single bounded
        summary message placed after the FIRST exchange. Head protection drops from
        `policy.head_exchanges` to one here, and that is not an oversight: reaching L3
        means head plus tail are over target on their own, so the only material left to
        give is inside them. Exchange one carries the task definition and is never
        summarised - summarising it is how an agent forgets what it was asked to do.
    L4  Folds the PREVIOUS checkpoint's summary into a new one together with whatever has
        aged since. It buys FRESHNESS, not room: on a first pass there is no previous
        summary and it frees nothing, and `LadderRun.reached_target` says so rather than
        pretending otherwise. That honesty is the point - the caller escalates instead of
        looping.

    A RUNG NEVER RETURNS A LARGER HISTORY THAN IT RECEIVED. L3 and L4 add a summary
    message, so each one is applied only if the result is not bigger; otherwise the
    pre-rung history stands. Without that guard a paid rung could grow the prompt it was
    called to shrink, which the ladder's own stopping rule would never notice.

THE TWO INVARIANTS, BOTH TESTED PER RUNG
    1. Tool call / tool return pairing survives every cut. Breaking it makes the provider
       reject the conversation with a 400, far from the compaction that caused it.

       HOW IT IS GUARANTEED HERE, rather than checked afterwards: a tool return arrives in
       a `ModelRequest` that carries no `UserPromptPart`, so `_split_exchanges` puts it in
       the same exchange as the `ToolCallPart` that asked for it. Every cut this file makes
       removes whole exchanges. There is no code path that can separate the two.
    2. `should_compress` handles `window_used is None` by falling back to the local token
       estimate. This adapter does not re-derive it: it delegates to the Protocol's own
       default, which is where `docs/TASKS.md#t-f5-02` put the mandatory fallback so it
       could not be forgotten one adapter at a time.

CONCURRENCY COMES FROM DBOS
    This engine does not need locks, snapshots or commit fences. `compress` returns a
    result and does not mutate; the caller runs inside a @DBOS.step() which supplies the
    durable lock and safe retry. Hermes wrote hundreds of lines for this. Do not.

    NOT MUTATING IS LOAD-BEARING FOR THAT RETRY, not a style preference. Pydantic AI's
    message parts are mutable dataclasses, so an in-place L1 looks correct everywhere
    except on a retried step, where attempt two starts from a history attempt one already
    shortened and the ladder compounds silently. Every rung here builds new objects with
    `dataclasses.replace`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolReturnPart,
    UserPromptPart,
)

from agent_core.domain.compaction import (
    CompactionCheckpoint,
    CompactionPolicy,
    CompactionResult,
    ContextState,
    LadderRun,
    Rung,
    is_within_target,
)
from agent_core.domain.turn import SessionRef, Usage
from agent_core.ports.context_engine import ContextEngine

# Four characters per token is the usual English rule of thumb. It only ever has to be
# good enough to decide WHICH rung, and it is the same estimator on both sides of every
# comparison the ladder makes, so a systematic error cancels.
CHARS_PER_TOKEN = 4

# Role markers, part separators and the message envelope cost tokens no content accounts
# for. Undercounting them makes the estimate optimistic, and an optimistic estimate is the
# one that lets the agent overflow.
MESSAGE_OVERHEAD_TOKENS = 4

# Used when the caller does not know the model's window. Deliberately NOT zero: zero means
# "unknown" to `domain.compaction.target_tokens`, which then resolves to a target of zero
# and climbs every rung on every pass - correct as a fallback, ruinous as a default.
DEFAULT_CONTEXT_WINDOW = 128_000

_SUMMARY_HEADER = "[compacted conversation summary]"

# Below this a stub costs more than the content it replaces.
_MIN_PRUNABLE_CHARS = 200


class UnsupportedHistoryError(TypeError):
    """`history` arrived in a shape this engine cannot read as a conversation.

    `ConversationStore.load_history` returns `object` and its on-disk encoding is still
    unsettled (docs/TASKS.md#t-f1-13), so this adapter accepts Pydantic AI's own message
    type - the vocabulary D7 says the port mirrors - and refuses anything else BY NAME.
    Guessing at the encoding here would put a second, drifting copy of it in the ladder.
    """


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """What the cheap model is asked for, in one object so the call site cannot drift.

    `previous_summary` is L4's entire reason to exist: the aging summary goes IN as an
    input so the new one refines it instead of repeating it. It is None for L3.
    """

    text: str
    model: str | None
    previous_summary: str | None


Summariser = Callable[[SummaryRequest], Awaitable[str]]
"""The one model call this adapter makes, injected.

Injected rather than built here for the reason `ModelGateway` exists at all: this file
owns the compaction STRATEGY, and which model serves a summary is a deployment decision.
It is also what lets the whole ladder be tested without a network.

When it is None, L3 and L4 change nothing and report it by freeing nothing. That is a
degraded engine, not a broken one - the free rungs still run and `made_progress` tells the
caller the truth.
"""


# The port's own trigger, reached unbound so the adapter INHERITS the mandatory
# None-window fallback instead of re-deriving it. Same technique, and the same reason, as
# `Core/tests/unit/test_ports_context_engine.py`.
_PORT_TRIGGER = cast(
    "Callable[[ContextEngine, ContextState, CompactionPolicy], bool]",
    ContextEngine.should_compress,
)


def _part_chars(part: object) -> int:
    """Characters a single message part will cost on the wire, near enough."""
    total = 0
    for attribute in ("content", "args", "tool_name"):
        value = getattr(part, attribute, None)
        if value is None:
            continue
        total += len(value) if isinstance(value, str) else len(str(value))
    return total


def _message_tokens(message: ModelMessage) -> int:
    chars = sum(_part_chars(part) for part in message.parts)
    return MESSAGE_OVERHEAD_TOKENS + chars // CHARS_PER_TOKEN


def estimate_tokens(messages: Sequence[ModelMessage]) -> int:
    """The local estimate. Public because `should_compress`'s fallback is fed by it and a
    caller with no provider-reported usage has to get the number from somewhere."""
    return sum(_message_tokens(message) for message in messages)


def _as_messages(history: object) -> list[ModelMessage]:
    """Narrow the store's `object` into a conversation, or refuse by name."""
    if history is None:
        return []
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise UnsupportedHistoryError(
            f"history must be a sequence of Pydantic AI ModelMessage values, got "
            f"{type(history).__name__}."
        )
    messages: list[ModelMessage] = []
    for message in history:
        if not isinstance(message, (ModelRequest, ModelResponse)):
            raise UnsupportedHistoryError(
                f"history carries a {type(message).__name__}; this engine accepts only "
                "Pydantic AI ModelMessage values."
            )
        messages.append(message)
    return messages


def _starts_exchange(message: ModelMessage) -> bool:
    """A human turn opens an exchange. Nothing else does.

    A `ModelRequest` carrying tool returns is a CONTINUATION of the exchange whose model
    response asked for them, which is precisely why cutting on this boundary can never
    separate a tool call from its return.
    """
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


@dataclass(frozen=True, slots=True)
class _Exchange:
    """One complete user turn and everything that answered it.

    `start`/`end` are indices into the ORIGINAL message list, kept so a checkpoint can say
    how far through the conversation its summary reaches.
    """

    messages: tuple[ModelMessage, ...]
    start: int
    end: int
    tokens: int

    @classmethod
    def of(cls, messages: Sequence[ModelMessage], start: int) -> _Exchange:
        return cls(
            messages=tuple(messages),
            start=start,
            end=start + len(messages),
            tokens=estimate_tokens(messages),
        )

    def with_messages(self, messages: Sequence[ModelMessage]) -> _Exchange:
        return replace(
            self, messages=tuple(messages), tokens=estimate_tokens(messages)
        )


def _split_exchanges(
    messages: Sequence[ModelMessage],
) -> tuple[tuple[ModelMessage, ...], tuple[_Exchange, ...]]:
    """Everything before the first human turn is preamble; the rest are exchanges.

    The preamble is the system prompt and is never cut - it is the cheapest part of the
    history and the most expensive to lose.
    """
    first_turn = next(
        (index for index, message in enumerate(messages) if _starts_exchange(message)),
        len(messages),
    )
    preamble = tuple(messages[:first_turn])

    exchanges: list[_Exchange] = []
    start = first_turn
    for index in range(first_turn + 1, len(messages) + 1):
        at_end = index == len(messages)
        if at_end or _starts_exchange(messages[index]):
            exchanges.append(_Exchange.of(messages[start:index], start))
            start = index
    return preamble, tuple(exchanges)


def _prune_exchange(exchange: _Exchange) -> _Exchange:
    """L1 over one exchange: every tool return becomes a stub, nothing is dropped.

    Builds new parts and new messages. Mutating the parts in place would be shorter, would
    pass every assertion about the RETURNED history, and would corrupt the caller's
    conversation on the next DBOS step retry.
    """
    rebuilt: list[ModelMessage] = []
    changed = False
    for message in exchange.messages:
        if not isinstance(message, ModelRequest):
            rebuilt.append(message)
            continue
        parts = list(message.parts)
        touched = False
        for index, part in enumerate(parts):
            if not isinstance(part, ToolReturnPart):
                continue
            content = str(part.content)
            if len(content) < _MIN_PRUNABLE_CHARS:
                continue
            parts[index] = replace(
                part,
                content=(
                    f"[pruned: {len(content)} characters of {part.tool_name} output; "
                    "the original is in the transcript]"
                ),
            )
            touched = True
        if touched:
            rebuilt.append(replace(message, parts=parts))
            changed = True
        else:
            rebuilt.append(message)
    return exchange.with_messages(rebuilt) if changed else exchange


def _render_text(exchanges: Sequence[_Exchange]) -> str:
    """What the summariser is shown. Flat, role-tagged, no provider vocabulary."""
    lines: list[str] = []
    for exchange in exchanges:
        for message in exchange.messages:
            for part in message.parts:
                content = getattr(part, "content", None)
                if content is None:
                    content = getattr(part, "args", None)
                if content is None:
                    continue
                lines.append(f"{part.part_kind}: {content}")
    return "\n".join(lines)


def _summary_message(summary: str, covers_through: int) -> ModelMessage:
    """The summary as one message the provider will accept from any vendor.

    A system-role message, not a faked user turn: an invented human turn is something the
    model can be asked to act on, and it pollutes the transcript with words nobody said.
    """
    return ModelRequest(
        parts=[
            SystemPromptPart(
                content=(
                    f"{_SUMMARY_HEADER} covering the first {covers_through} messages of "
                    f"this session:\n{summary}"
                )
            )
        ]
    )


class _Climb:
    """The working state of one climb. Rungs move exchanges between four buckets.

    Rendered history is always `preamble + head + [summary] + tail`, which is why the
    summary lands after the protected head and before the recent tail without any rung
    needing to know about message indices.
    """

    def __init__(self, messages: Sequence[ModelMessage]) -> None:
        preamble, exchanges = _split_exchanges(messages)
        self.preamble = preamble
        self.head: tuple[_Exchange, ...] = exchanges
        self.tail: tuple[_Exchange, ...] = ()
        self.aged: tuple[_Exchange, ...] = ()
        self.summary: str | None = None
        self.covers_through = 0

    # -- reading ---------------------------------------------------------------

    @property
    def live(self) -> tuple[_Exchange, ...]:
        return self.head + self.tail

    def render(self) -> list[ModelMessage]:
        messages: list[ModelMessage] = list(self.preamble)
        for exchange in self.head:
            messages.extend(exchange.messages)
        if self.summary is not None:
            messages.append(_summary_message(self.summary, self.covers_through))
        for exchange in self.tail:
            messages.extend(exchange.messages)
        return messages

    def tokens(self) -> int:
        return estimate_tokens(self.render())

    def _snapshot(
        self,
    ) -> tuple[
        tuple[_Exchange, ...], tuple[_Exchange, ...], tuple[_Exchange, ...], str | None, int
    ]:
        return (self.head, self.tail, self.aged, self.summary, self.covers_through)

    def _restore(
        self,
        state: tuple[
            tuple[_Exchange, ...],
            tuple[_Exchange, ...],
            tuple[_Exchange, ...],
            str | None,
            int,
        ],
    ) -> None:
        self.head, self.tail, self.aged, self.summary, self.covers_through = state

    # -- rungs -----------------------------------------------------------------

    def prune_tool_output(self) -> None:
        """L1. Free. Everything but the most recent exchange loses its tool payloads."""
        live = self.live
        if not live:
            return
        keep = live[-1]
        pruned = tuple(
            exchange if exchange is keep else _prune_exchange(exchange) for exchange in live
        )
        head_size = len(self.head)
        self.head = pruned[:head_size]
        self.tail = pruned[head_size:]

    def slide_window(self, policy: CompactionPolicy) -> None:
        """L2. Free. Protect the head, keep a tail by token budget, age the middle."""
        live = self.live
        if not live:
            return

        head_size = max(0, min(policy.head_exchanges, len(live)))

        # The most recent exchange is kept whatever it costs: dropping the turn the model
        # is answering does not compact a conversation, it breaks one.
        tail_start = len(live) - 1
        budget = policy.tail_tokens - live[-1].tokens
        while tail_start > head_size and budget - live[tail_start - 1].tokens >= 0:
            tail_start -= 1
            budget -= live[tail_start].tokens

        if tail_start <= head_size:
            self.head, self.tail = live, ()
            return

        self.head = live[:head_size]
        self.tail = live[tail_start:]
        self.aged = self.aged + live[head_size:tail_start]

    async def summarise_middle(
        self, policy: CompactionPolicy, summariser: Summariser | None
    ) -> None:
        """L3. One model call. Everything but the first and the last exchange condenses."""
        if summariser is None:
            return
        live = self.live
        newly_aged = live[1:-1] if len(live) > 2 else ()
        material = tuple(sorted(self.aged + newly_aged, key=lambda item: item.start))
        if not material:
            return

        before = self.tokens()
        state = self._snapshot()

        summary = await summariser(
            SummaryRequest(
                text=_render_text(material),
                model=policy.summariser_model,
                previous_summary=None,
            )
        )
        self.head = live[:1]
        self.tail = live[-1:] if len(live) > 1 else ()
        self.aged = ()
        self.summary = summary
        self.covers_through = max(item.end for item in material)

        if self.tokens() > before:
            self._restore(state)

    async def resummarise(
        self,
        policy: CompactionPolicy,
        summariser: Summariser | None,
        previous_summary: str | None,
    ) -> None:
        """L4. One model call. The aging summary goes in as an INPUT to the new one."""
        if summariser is None:
            return
        folded: list[str] = []
        if self.summary is not None:
            folded.append(self.summary)
        if self.aged:
            folded.append(_render_text(self.aged))
        if not folded and previous_summary is None:
            return

        before = self.tokens()
        state = self._snapshot()

        merged = await summariser(
            SummaryRequest(
                text="\n\n".join(folded),
                model=policy.summariser_model,
                previous_summary=previous_summary,
            )
        )
        if self.aged:
            self.covers_through = max(
                self.covers_through, max(item.end for item in self.aged)
            )
        self.aged = ()
        self.summary = merged

        if self.tokens() > before:
            self._restore(state)


def _utc_now() -> datetime:
    """The wall clock, in one place so a test can replace it.

    Non-determinism is allowed because `compress` is only ever reached from inside a DBOS
    step, never from a workflow body. CLAUDE.md non-negotiable #2.
    """
    return datetime.now(UTC)


class LadderContextEngine:
    """`ContextEngine` as the L1-L4 ladder over a Pydantic AI message history.

    `context_window` is a construction parameter because nothing on the port's `compress`
    carries it and the ladder's target is a fraction OF it. The composition root knows
    which model a deployment runs and therefore owns the number; see the constant above
    for why the fallback is not zero.
    """

    def __init__(
        self,
        summariser: Summariser | None = None,
        *,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._summariser = summariser
        self._context_window = context_window
        self._now = now
        self._estimates: dict[SessionRef, int] = {}
        self._checkpoints: dict[SessionRef, CompactionCheckpoint] = {}

    # -- lifecycle -------------------------------------------------------------

    def on_session_start(self, session: SessionRef) -> None:
        self._estimates.pop(session, None)
        self._checkpoints.pop(session, None)

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        """Keep the local estimate current. It is the ONLY input the trigger has whenever
        the provider does not report `context_window_used`, which is the common case.

        REPLACED, NEVER ACCUMULATED (docs/TASKS.md#t-f5-11). `input_tokens` is the whole
        prompt this response was billed for and `output_tokens` is what was appended to
        it, so their sum is already the size of the conversation as it now stands. Adding
        one response to the next counts every earlier turn again, once per turn that
        follows it, and produces TOTAL SPEND - a number that only ever grows. The trigger
        divides this by the window, so a running total crosses `trigger_fraction` once and
        then stays across it for the rest of the session: the ladder runs on every turn,
        rewrites the prompt prefix every turn, and re-bills the whole prompt at full price
        every turn. That is the exact failure `domain/compaction.py` opens by naming, and
        it would fail no test - only the invoice.
        """
        self._estimates[session] = usage.input_tokens + usage.output_tokens

    def estimated_tokens(self, session: SessionRef) -> int:
        return self._estimates.get(session, 0)

    def adopt_checkpoint(self, checkpoint: CompactionCheckpoint) -> None:
        """Seed L4 from `ConversationStore.latest_checkpoint`.

        The engine holds no store, and a process restart between two passes would
        otherwise leave L4 with nothing to fold - a summary frozen at whatever the last
        live process knew. The caller that has the store hands the chain back in here.
        """
        self._checkpoints[checkpoint.session] = checkpoint

    def on_session_end(self, session: SessionRef) -> None:
        """Real session boundaries ONLY - close, reset, expiry. NEVER per turn."""
        self._estimates.pop(session, None)
        self._checkpoints.pop(session, None)

    # -- the trigger -----------------------------------------------------------

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        """Delegated to the Protocol's default on purpose. See invariant 2 in the header:
        the None-window fallback is mandatory, and an adapter that reimplements it is an
        adapter that can forget it."""
        return _PORT_TRIGGER(self, state, policy)

    # -- the ladder ------------------------------------------------------------

    @staticmethod
    async def climb_ladder_async(
        policy: CompactionPolicy,
        *,
        tokens_before: int,
        context_window: int,
        apply_rung: Callable[[Rung], Awaitable[int]],
    ) -> LadderRun:
        """The async twin of `domain.compaction.climb_ladder`, and nothing more.

        WHY A TWIN EXISTS AT ALL. `climb_ladder` takes a SYNC `apply_rung`, and L3 and L4
        await a model call - D13's whole point is that the one member which reaches a
        provider is the one coroutine. There is no way to await from inside a sync
        callback, and pre-awaiting the summaries would pay for exactly the model calls the
        ladder exists to avoid.

        WHAT KEEPS IT FROM DRIFTING. The two decisions live in the domain and are called,
        not copied: `is_within_target` is the stopping rule, and `sorted(set(...))` is the
        determinism requirement - rungs climb in the ladder's order, never the profile's,
        because a `@DBOS.step()` that replayed in a different order would replay a
        different conversation. A test pins this function against the domain's.
        """
        tokens_after = tokens_before
        applied: list[Rung] = []

        for rung in sorted(set(policy.enabled_rungs)):
            if is_within_target(policy, tokens_after, context_window):
                break
            applied.append(rung)
            tokens_after = await apply_rung(rung)

        return LadderRun(
            rungs_applied=tuple(applied),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            reached_target=is_within_target(policy, tokens_after, context_window),
        )

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        """Climb the ladder and RETURN the result. Nothing here touches `history`."""
        messages = _as_messages(history)
        climb = _Climb(messages)
        previous = self._checkpoints.get(session)
        previous_summary = previous.summary if previous is not None else None

        async def apply_rung(rung: Rung) -> int:
            if rung is Rung.L1_PRUNE_TOOL_OUTPUT:
                climb.prune_tool_output()
            elif rung is Rung.L2_SLIDING_WINDOW:
                climb.slide_window(policy)
            elif rung is Rung.L3_SUMMARISE_MIDDLE:
                await climb.summarise_middle(policy, self._summariser)
            elif rung is Rung.L4_ITERATIVE_RESUMMARY:
                await climb.resummarise(policy, self._summariser, previous_summary)
            return climb.tokens()

        run = await self.climb_ladder_async(
            policy,
            tokens_before=estimate_tokens(messages),
            context_window=self._context_window,
            apply_rung=apply_rung,
        )

        # What the trigger reads has to FALL when the ladder frees room, and it has to
        # fall now: the next `update_from_response` is one whole model call away, and the
        # trigger is asked again before it. Left to the accumulator alone the engine would
        # compact, see the same number it saw before, and compact again.
        self._estimates[session] = run.tokens_after

        checkpoint = self._checkpoint_for(session, climb, run, previous)
        if checkpoint is not None:
            self._checkpoints[session] = checkpoint

        return CompactionResult(
            compacted_history=climb.render(),
            checkpoint=checkpoint,
            rungs_applied=run.rungs_applied,
            tokens_before=run.tokens_before,
            tokens_after=run.tokens_after,
        )

    def _checkpoint_for(
        self,
        session: SessionRef,
        climb: _Climb,
        run: LadderRun,
        previous: CompactionCheckpoint | None,
    ) -> CompactionCheckpoint | None:
        """A checkpoint exists only when a summary does - `CompactionResult.checkpoint` is
        None when the free rungs did all the work, and there is nothing to persist."""
        if climb.summary is None:
            return None
        supersedes = previous.checkpoint_id if previous is not None else None
        return CompactionCheckpoint(
            checkpoint_id=_checkpoint_id(session, climb.summary, supersedes),
            session=session,
            summary=climb.summary,
            covers_through_message=climb.covers_through,
            tokens_before=run.tokens_before,
            tokens_after=run.tokens_after,
            rungs_applied=run.rungs_applied,
            created_at=self._now(),
            supersedes=supersedes,
        )


def _checkpoint_id(
    session: SessionRef, summary: str, supersedes: str | None
) -> str:
    """Derived, not random.

    A `uuid4()` here would be non-determinism inside a DBOS step's replay surface: the
    same compaction replayed after a crash would produce a different id and a second row
    in a chain that is supposed to be linear. Hashing the inputs makes the replay produce
    the same checkpoint, and `supersedes` is in the hash so two passes over an identical
    history still chain instead of colliding.
    """
    digest = hashlib.sha256(
        "\x00".join([session.session_id, supersedes or "", summary]).encode("utf-8")
    )
    return f"ckpt-{digest.hexdigest()[:32]}"
