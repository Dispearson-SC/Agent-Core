"""C26 (D58): the root turn streams its reply text over the event feed.

Subjects: domain/reply_chunks.py (`ReplyChunker`), domain/events.py (`EventKind.DELTA`, the reply in
the snapshot), adapters/driven/agent_pydantic/reply_stream.py and the runner's `reply_events` seat,
adapters/driving/http/event_routes.py (the JSON), and composition (`AGENT_CORE_STREAM_REPLIES`).

What is defended: the first chunk is published BEFORE the turn finishes (the acceptance test);
a chunk ends at a sentence so a voice front-end can start on the first one; the chunks put back
together are exactly the reply; only a turn a person or channel started is streamed (never a
peer's, whose text is another agent's input); a streamer that fails never fails the turn; and the
final answer the turn records is unchanged (the deltas are a side channel).
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.agent_pydantic.reply_stream import ReplyStreamHandler
from agent_core.adapters.driving.http.event_routes import event_json, snapshot_json
from agent_core.application.event_feed import EventFeed, FeedKind
from agent_core.composition import Settings
from agent_core.domain.events import (
    MAX_DELTA_CHARS,
    EventKind,
    ProgressEvent,
    StoredEvent,
    bound_event,
    fold_snapshot,
    progress_event_id,
)
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.reply_chunks import MAX_CHUNK_CHARS, ReplyChunker
from agent_core.domain.turn import (
    CallerIdentity,
    PeerOwner,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes
from tests.unit.test_event_feed import Store

# --- the chunker -----------------------------------------------------------------------------


def _chunks(parts: list[str]) -> list[str]:
    chunker = ReplyChunker()
    out: list[str] = []
    for part in parts:
        out.extend(chunker.push(part))
    out.extend(chunker.flush())
    return out


def test_a_chunk_ends_at_the_end_of_a_sentence() -> None:
    assert _chunks(["Hello there. How ", "are you? Fi", "ne."]) == [
        "Hello there. ",
        "How are you? ",
        "Fine.",
    ]


def test_the_first_sentence_is_ready_as_soon_as_it_is_complete() -> None:
    chunker = ReplyChunker()

    assert chunker.push("Your order ships tod") == []
    assert chunker.push("ay.") == [], "a full stop at the very end may still be a decimal point"
    assert chunker.push(" It will ") == ["Your order ships today. "]
    assert chunker.flush() == ["It will "]


@pytest.mark.parametrize(
    "text",
    ["It costs 3.50 dollars today.", "See v2.1.3 for details.", "Call 555.0100 now."],
)
def test_a_dot_inside_a_number_or_a_token_does_not_end_a_sentence(text: str) -> None:
    assert _chunks([text]) == [text]


def test_newlines_and_other_terminators_also_end_a_chunk() -> None:
    assert _chunks(["Line one\nLine two"]) == ["Line one\n", "Line two"]
    assert _chunks(["¿Cuánto debe? Nada… Gracias."]) == ["¿Cuánto debe? ", "Nada… ", "Gracias."]
    assert _chunks(["你好。谢谢。"]) == ["你好。", "谢谢。"]


def test_a_sentence_with_no_end_is_cut_at_a_space_before_the_limit_never_inside_a_word() -> None:
    words = " ".join(f"word{n}" for n in range(200))

    chunks = _chunks([words])

    assert len(chunks) > 1 and all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert all(not c.endswith("wor") and not c.endswith("word") for c in chunks[:-1])
    assert "".join(chunks) == words


def test_one_endless_token_is_cut_hard_rather_than_held_for_ever() -> None:
    token = "x" * (MAX_CHUNK_CHARS * 2 + 10)

    chunks = _chunks([token])

    assert [len(c) for c in chunks] == [MAX_CHUNK_CHARS, MAX_CHUNK_CHARS, 10]


def test_the_chunks_put_back_together_are_exactly_the_text_however_it_arrived() -> None:
    text = "Hi. It is 3.5 now! ¿Seguro? Sí...\n\nOk then; here is a rather long clause, " * 6
    rng = random.Random(7)
    for _ in range(50):
        cuts = sorted(rng.sample(range(1, len(text)), 12))
        parts = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]
        assert "".join(_chunks(parts)) == text


def test_nothing_is_published_for_an_empty_reply() -> None:
    assert _chunks([]) == [] and _chunks(["", ""]) == []


# --- the event and the snapshot -----------------------------------------------------------------

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _delta(turn: str, run: str, segment: int, index: int, text: str, seq: int) -> StoredEvent:
    return StoredEvent(
        seq,
        ProgressEvent(
            event_id=progress_event_id(turn, EventKind.DELTA, f"{run}:{segment}:{index}"),
            kind=EventKind.DELTA,
            tenant_id="t",
            session_id="s",
            turn_id=turn,
            agent="a",
            at=T0,
            detail=f"{run}:{segment}:{index}",
            text=text,
        ),
    )


def _started(turn: str, seq: int) -> StoredEvent:
    return StoredEvent(
        seq,
        ProgressEvent(
            event_id=progress_event_id(turn, EventKind.STARTED),
            kind=EventKind.STARTED,
            tenant_id="t",
            session_id="s",
            turn_id=turn,
            agent="a",
            at=T0,
        ),
    )


def test_the_snapshot_carries_the_reply_streamed_so_far() -> None:
    snapshot = fold_snapshot(
        "r",
        (
            _started("r", 1),
            _delta("r", "run1", 0, 0, "Hello there. ", 2),
            _delta("r", "run1", 0, 1, "How are", 3),
        ),
    )

    (turn,) = snapshot.turns
    assert turn.reply_text == "Hello there. How are"
    assert turn.state.value == "running", "a delta is not a state change"
    assert snapshot.last_seq == 3 and not snapshot.settled


def test_a_new_run_or_a_new_response_replaces_the_text_instead_of_appending() -> None:
    events = [_started("r", 1), _delta("r", "run1", 0, 0, "Let me check. ", 2)]
    # The model answered again after a tool call: the reply is the LAST response's text.
    after_tool = [*events, _delta("r", "run1", 1, 0, "It is 42.", 3)]
    # The step was retried: a new run replaces the old one's text.
    retried = [*after_tool, _delta("r", "run2", 0, 0, "It is 42, sorry.", 4)]

    assert fold_snapshot("r", tuple(events)).turns[0].reply_text == "Let me check. "
    assert fold_snapshot("r", tuple(after_tool)).turns[0].reply_text == "It is 42."
    assert fold_snapshot("r", tuple(retried)).turns[0].reply_text == "It is 42, sorry."


def test_chunks_are_ordered_by_their_index_not_their_arrival() -> None:
    snapshot = fold_snapshot(
        "r",
        (
            _started("r", 1),
            _delta("r", "run1", 0, 1, "two ", 2),
            _delta("r", "run1", 0, 0, "one ", 3),
        ),
    )

    assert snapshot.turns[0].reply_text == "one two "


def test_a_delta_event_and_the_reply_are_in_the_json() -> None:
    stored = _delta("r", "run1", 0, 0, "Hello. ", 2)
    snapshot = fold_snapshot("r", (_started("r", 1), stored))

    assert event_json(stored)["kind"] == "delta" and event_json(stored)["text"] == "Hello. "
    assert snapshot_json(snapshot)["turns"][0]["reply_text"] == "Hello. "
    assert event_json(_started("r", 1))["text"] is None


def test_a_delta_keeps_its_text_up_to_its_own_bound_while_labels_are_cut() -> None:
    event = _delta("r", "run1", 0, 0, "x" * (MAX_DELTA_CHARS + 50), 1).event

    bounded = bound_event(event)

    assert bounded.text is not None and len(bounded.text) == MAX_DELTA_CHARS
    assert bounded.detail == "run1:0:0"


def test_the_first_chunk_reaches_a_feed_subscriber_before_the_turn_finishes() -> None:
    store = Store()
    store.add(_started("r", 1).event)
    feed = EventFeed(store, store, poll_seconds=0.01)

    async def scenario() -> tuple[FeedKind, str | None, bool]:
        await feed.pump()
        stream = feed.follow("t", "r")
        assert (await anext(stream)).kind is FeedKind.SNAPSHOT
        store.add(_delta("r", "run1", 0, 0, "Hello there. ", 2).event)
        await feed.pump()
        item = await asyncio.wait_for(anext(stream), 1)
        text = None if item.event is None else item.event.event.text
        return item.kind, text, feed.subscriber_count == 1

    kind, text, still_open = asyncio.run(scenario())

    assert (kind, text) == (FeedKind.EVENT, "Hello there. ")
    assert still_open, "the turn has not finished: the stream is still open"


# --- the runner ---------------------------------------------------------------------------------

CALLER = CallerIdentity(subject_id="u", channel="voice", tenant_id=TenantId("t"))
SESSION = SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t"))


class _Events:
    def __init__(self, *, fail: bool = False, on_publish: Any = None) -> None:
        self.events: list[ProgressEvent] = []
        self.fail = fail
        self.attempts = 0
        self.on_publish = on_publish

    async def publish(self, event: ProgressEvent) -> None:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("the event store is down")
        self.events.append(event)
        if self.on_publish is not None:
            self.on_publish(event)

    @property
    def deltas(self) -> list[ProgressEvent]:
        return [e for e in self.events if e.kind is EventKind.DELTA]


def _runner(model: FunctionModel, events: _Events | None) -> Any:
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        model_factory=lambda model_id, base_url: model,
        reply_events=events,
    )


PROFILE = AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "minimax/MiniMax-M3"})


def _request(*, peer: bool = False) -> TurnRequest:
    return TurnRequest(
        session=SESSION,
        caller=CALLER,
        profile_id="p",
        input=UserInput(text="hi"),
        owner=PeerOwner("root", "asker", "orch", "call-1") if peer else None,
    )


def _words(*parts: str) -> Any:
    async def stream(messages: list[Any], info: AgentInfo) -> AsyncIterator[str]:
        for part in parts:
            yield part

    def whole(messages: list[Any], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("".join(parts))])

    return FunctionModel(whole, stream_function=stream)


def test_the_first_chunk_is_published_before_the_model_has_finished() -> None:
    """The acceptance test of C26. The model's stream waits, after its first sentence, until the
    event store has been handed that sentence: if replies were only published at the end, it would
    wait for ever and the test would fail on the timeout."""
    gate = asyncio.Event()

    async def stream(messages: list[Any], info: AgentInfo) -> AsyncIterator[str]:
        yield "Your order ships today. "
        await asyncio.wait_for(gate.wait(), timeout=2)  # the sentence must have been published
        yield "It arrives tomorrow."

    events = _Events(on_publish=lambda event: gate.set())
    runner = _runner(FunctionModel(stream_function=stream), events)

    outcome = asyncio.run(runner.run(TurnId("turn-1"), _request(), PROFILE, None))

    assert outcome.result is not None
    assert outcome.result.text == "Your order ships today. It arrives tomorrow."
    assert [e.text for e in events.deltas] == ["Your order ships today. ", "It arrives tomorrow."]


def test_the_chunks_are_the_reply_and_carry_the_turn_run_and_order() -> None:
    events = _Events()
    runner = _runner(_words("Hello ", "there. How ", "are you?", " Fine."), events)

    outcome = asyncio.run(runner.run(TurnId("turn-1"), _request(), PROFILE, None))

    assert outcome.result is not None
    deltas = events.deltas
    assert "".join(d.text or "" for d in deltas) == outcome.result.text
    assert {d.turn_id for d in deltas} == {"turn-1"} and {d.agent for d in deltas} == {"p"}
    assert all(d.tenant_id == "t" and d.session_id == "s" and d.owner is None for d in deltas)
    runs = {(d.detail or "").split(":")[0] for d in deltas}
    assert len(runs) == 1 and next(iter(runs)), "one run id for the whole stream"
    assert [(d.detail or "").split(":")[2] for d in deltas] == [str(i) for i in range(len(deltas))]
    assert len({d.event_id for d in deltas}) == len(deltas)


def test_a_peers_turn_is_not_streamed() -> None:
    """A peer's text is another agent's input, not something a person is waiting to hear."""
    events = _Events()
    runner = _runner(_words("Hello there. ", "Bye."), events)

    outcome = asyncio.run(runner.run(TurnId("peer-1"), _request(peer=True), PROFILE, None))

    assert outcome.result is not None and outcome.result.text == "Hello there. Bye."
    assert events.attempts == 0


def test_without_a_publisher_the_run_is_the_ordinary_one() -> None:
    """No `reply_events`: the model is called the way it always was (no stream function needed)."""

    def plain(messages: list[Any], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("plain answer")])

    runner = _runner(FunctionModel(plain), None)

    outcome = asyncio.run(runner.run(TurnId("turn-1"), _request(), PROFILE, None))

    assert outcome.result is not None and outcome.result.text == "plain answer"


def test_a_publisher_that_fails_costs_the_chunks_not_the_turn_and_is_not_hammered() -> None:
    events = _Events(fail=True)
    runner = _runner(_words("One. ", "Two. ", "Three. ", "Four."), events)

    outcome = asyncio.run(runner.run(TurnId("turn-1"), _request(), PROFILE, None))

    assert outcome.result is not None and outcome.result.text == "One. Two. Three. Four."
    assert events.attempts == 1, "after the first failure the run stops trying"


def test_a_reply_made_of_text_only_in_a_tool_free_run_matches_the_recorded_answer() -> None:
    """The recorded final answer is the source of truth; the deltas are a copy of it."""
    events = _Events()
    runner = _runner(_words("Alpha. ", "Beta. ", "Gamma"), events)

    outcome = asyncio.run(runner.run(TurnId("turn-1"), _request(), PROFILE, None))

    assert outcome.result is not None
    assert json.dumps("".join(d.text or "" for d in events.deltas)) == json.dumps(
        outcome.result.text
    )


# --- the switch ----------------------------------------------------------------------------------

_NO_FILE = Path("/nonexistent/.env")


def test_streaming_replies_is_off_unless_the_deployment_switches_it_on() -> None:
    assert Settings.from_env({}, env_file=_NO_FILE).stream_replies is False
    on = Settings.from_env({"AGENT_CORE_STREAM_REPLIES": "true"}, env_file=_NO_FILE)
    assert on.stream_replies is True
    with pytest.raises(ValueError, match="AGENT_CORE_STREAM_REPLIES"):
        Settings.from_env({"AGENT_CORE_STREAM_REPLIES": "yes please"}, env_file=_NO_FILE)


# --- C1q: the response counter and the publish hand-off ---


def _handler(events: _Events, **options: Any) -> ReplyStreamHandler:
    return ReplyStreamHandler(
        events, turn_id=TurnId("turn-1"), session=SESSION, agent="p", **options
    )


async def _stream(*events: Any) -> AsyncIterator[Any]:
    for event in events:
        yield event


def test_the_response_counter_counts_model_responses_not_text_parts() -> None:
    """C1q: one model response holding two text parts around a tool call is ONE response; the next
    model request (its first part has index 0 again) is the next one."""
    events = _Events()
    handler = _handler(events)

    async def scenario() -> None:
        first = [
            PartStartEvent(index=0, part=TextPart("One. ")),
            PartEndEvent(index=0, part=TextPart("One. ")),
            PartStartEvent(index=1, part=ToolCallPart("lookup", {}, "c1")),
            PartStartEvent(index=2, part=TextPart("Two. ")),
            PartEndEvent(index=2, part=TextPart("Two. ")),
        ]
        await handler(None, _stream(*first))
        second = [
            PartStartEvent(index=0, part=TextPart("Three.")),
            PartEndEvent(index=0, part=TextPart("Three.")),
        ]
        await handler(None, _stream(*second))

    asyncio.run(scenario())

    assert [e.detail.split(":", 1)[1] for e in events.deltas] == ["0:0", "0:1", "1:0"]
    assert [e.text for e in events.deltas] == ["One. ", "Two. ", "Three."]


def test_publishing_never_makes_the_model_stream_wait() -> None:
    """C1q: the model's stream hands out its second sentence and only then lets the (slow) event
    store accept the first. If publishing ran inline in the stream, the stream would wait for the
    store and the store for the stream: the gate times out and the chunks are lost."""
    gate = asyncio.Event()
    published: list[str] = []

    class Slow:
        async def publish(self, event: ProgressEvent) -> None:
            await asyncio.wait_for(gate.wait(), timeout=2)
            published.append(event.text or "")

    async def stream(messages: list[Any], info: AgentInfo) -> AsyncIterator[str]:
        yield "First sentence. "
        yield "Second sentence. "
        gate.set()
        yield "Third."

    runner = _runner(FunctionModel(stream_function=stream), Slow())  # type: ignore[arg-type]

    outcome = asyncio.run(runner.run(TurnId("turn-1"), _request(), PROFILE, None))

    assert outcome.result is not None
    assert published == ["First sentence. ", "Second sentence. ", "Third."], "flushed on finish"


def test_a_stalled_event_store_costs_a_bounded_queue_not_unbounded_memory() -> None:
    """C1q: while the store accepts nothing the handler holds at most `max_pending` chunks; text
    that arrives past that is coalesced into the newest pending chunk, and everything pending is
    flushed on finish."""
    release = asyncio.Event()
    events = _Events()
    real_publish = events.publish

    async def stalled(event: ProgressEvent) -> None:
        await release.wait()
        await real_publish(event)

    events.publish = stalled  # type: ignore[method-assign]
    handler = _handler(events, max_pending=4)

    async def scenario() -> int:
        sentences = [f"Sentence {n}. " for n in range(50)]
        feed = [PartStartEvent(index=0, part=TextPart(""))]
        feed += [PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=t)) for t in sentences]
        feed += [PartEndEvent(index=0, part=TextPart("".join(sentences)))]
        task = asyncio.create_task(handler(None, _stream(*feed)))
        await asyncio.sleep(0.05)
        deepest = handler.pending
        release.set()
        await asyncio.wait_for(task, 2)
        return deepest

    deepest = asyncio.run(scenario())

    assert deepest <= 4
    assert "".join(e.text or "" for e in events.deltas) == "".join(
        f"Sentence {n}. " for n in range(50)
    ), "nothing was lost: overflow is coalesced"


# --- C1r: the publish worker never outlives the stream ---


def _stalled_events() -> _Events:
    events = _Events()

    async def stalled(event: ProgressEvent) -> None:
        await asyncio.Event().wait()  # the store never answers

    events.publish = stalled  # type: ignore[method-assign]
    return events


def _other_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


def test_a_failing_model_stream_leaves_no_publish_worker_behind() -> None:
    """C1r R3/R4: the stream raises while a chunk is waiting on a stalled store. The worker is
    cancelled and awaited before the error leaves the handler, so nothing keeps publishing the
    text of a turn that already failed."""
    handler = _handler(_stalled_events())

    async def broken() -> AsyncIterator[Any]:
        yield PartStartEvent(index=0, part=TextPart("One. Two"))
        await asyncio.sleep(0)  # let the worker start on the first sentence
        raise RuntimeError("the model broke")

    async def scenario() -> list[asyncio.Task[Any]]:
        with pytest.raises(RuntimeError, match="the model broke"):
            await handler(None, broken())
        return _other_tasks()

    assert asyncio.run(scenario()) == []


def test_a_cancelled_turn_leaves_no_publish_worker_behind() -> None:
    handler = _handler(_stalled_events())
    started = asyncio.Event()

    async def hanging() -> AsyncIterator[Any]:
        yield PartStartEvent(index=0, part=TextPart("One. Two"))
        started.set()
        await asyncio.Event().wait()

    async def scenario() -> list[asyncio.Task[Any]]:
        task = asyncio.create_task(handler(None, hanging()))
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return _other_tasks()

    assert asyncio.run(scenario()) == []


def test_a_store_that_never_answers_is_given_up_on_after_the_flush_timeout() -> None:
    handler = ReplyStreamHandler(
        _stalled_events(),
        turn_id=TurnId("turn-1"),
        session=SESSION,
        agent="p",
        flush_timeout=0.05,
    )

    async def scenario() -> list[asyncio.Task[Any]]:
        await handler(
            None,
            _stream(
                PartStartEvent(index=0, part=TextPart("One. ")),
                PartEndEvent(index=0, part=TextPart("One. ")),
            ),
        )
        return _other_tasks()

    assert asyncio.run(scenario()) == []
