"""Compaction - a SILENT-BUG AREA. Wrong here shows up on the BILL, never as a red test.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-01, docs/TASKS.md#t-f5-05, docs/TASKS.md#t-f5-06
Status:  LADDER DECISION TESTS (t-f5-01); PAIRING PER RUNG (t-f5-05) AND THE BOUNDED-RESEND
         PROXY (t-f5-06) IMPLEMENTED. The four stubs at the end belong to other anchors.

The pairing test is the one that prevents a class of provider 400s that surface hours
after the compaction that caused them.

The ladder tests below (t-f5-01) guard the other half of the bill. Every rung climbed
rewrites more of the prompt prefix and invalidates more of the provider cache, so a
ladder that keeps climbing after the target is already met pays twice for nothing. That
overshoot frees MORE tokens, so no assertion about headroom would ever catch it - which
is exactly why the assertions here are about WHICH rungs ran, not about how much they
freed.

WHAT THE TWO NEW TESTS DO NOT PROVE, STATED ONCE SO NOBODY READS THEM AS MORE
    * `t-f5-05` proves that no rung SEPARATES a tool call from its return. It does not
      prove the provider accepts the result: a summary message in the middle of a
      conversation, a stub the model cannot act on, a vendor that rejects a system-role
      message after the first turn - none of those are pairing, and none of them are
      here. Only real traffic answers them.
    * `t-f5-06` is a PROXY and is filed as one. It measures tokens resent per turn and how
      often the prefix is rewritten, against a fake model over a simulated conversation. A
      fake has no bill and a simulated conversation has NO PROVIDER CACHE TO BREAK - and
      breaking that cache is the exact mechanism the phase is worried about. Cost per turn
      is `docs/TASKS.md#t-f5-09`, it is measured against a real invoice, and no assertion
      in this file may stand in for it.
    * Neither test says anything about summary QUALITY. The summariser here is a fake that
      returns a fixed string; whether a real one preserves what the agent needed is a
      question no unit test can ask.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agent_core.adapters.driven.context.engine import (
    LadderContextEngine,
    SummaryRequest,
    estimate_tokens,
)
from agent_core.domain.compaction import (
    CompactionPolicy,
    ContextState,
    Rung,
    climb_ladder,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId


class _Ladder:
    """Records which rungs were actually climbed and how many tokens each one freed.

    Standing in for `adapters/driven/context/engine.py` (t-f5-04): the domain decides
    WHETHER to climb, the adapter decides WHAT a rung does to a history. The recording is
    the whole point - `attempted` is the evidence that a rung the ladder did not need was
    never paid for.
    """

    def __init__(self, tokens: int, frees: Mapping[Rung, int]) -> None:
        self.tokens = tokens
        self._frees = frees
        self.attempted: list[Rung] = []

    def __call__(self, rung: Rung) -> int:
        self.attempted.append(rung)
        self.tokens -= self._frees.get(rung, 0)
        return self.tokens


WINDOW = 100_000
# CompactionPolicy.target_fraction defaults to 0.40, so the target is 40_000 tokens.

SESSION = SessionRef(session_id=SessionId("s-compaction"), tenant_id=TenantId("t-1"))


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_reaching_target_on_the_first_rung_stops_the_ladder_immediately() -> None:
    """L1 is free and often enough on its own. Climbing past it would spend a model call
    and rewrite more of the prefix for headroom that was already bought."""
    policy = CompactionPolicy()
    ladder = _Ladder(90_000, {Rung.L1_PRUNE_TOOL_OUTPUT: 60_000})

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [Rung.L1_PRUNE_TOOL_OUTPUT]
    assert run.rungs_applied == (Rung.L1_PRUNE_TOOL_OUTPUT,)
    assert run.reached_target is True
    assert run.tokens_after == 30_000


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_rung_is_climbed_only_after_the_previous_one_missed_target() -> None:
    """The two halves of the ladder rule in one run: L2 is reached because L1 fell short,
    and L3/L4 - the two rungs that cost a model call - are never reached because L2 did
    not fall short."""
    policy = CompactionPolicy()
    ladder = _Ladder(
        90_000,
        {Rung.L1_PRUNE_TOOL_OUTPUT: 20_000, Rung.L2_SLIDING_WINDOW: 40_000},
    )

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L2_SLIDING_WINDOW]
    assert run.rungs_applied == (Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L2_SLIDING_WINDOW)
    assert run.reached_target is True
    assert run.tokens_after == 30_000


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_history_already_within_target_climbs_no_rung_at_all() -> None:
    """Zero rungs is a legitimate outcome and the cheapest one there is. A pass that
    rewrites the prefix to free tokens nobody needed is pure loss - it costs a full
    re-billed prompt on the next request and buys nothing."""
    policy = CompactionPolicy()
    ladder = _Ladder(10_000, {Rung.L1_PRUNE_TOOL_OUTPUT: 5_000})

    run = climb_ladder(
        policy, tokens_before=10_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == []
    assert run.rungs_applied == ()
    assert run.reached_target is True
    assert run.tokens_after == 10_000


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_history_sitting_exactly_on_the_target_climbs_no_rung() -> None:
    """The boundary itself, which no other case in this file stands on.

    `is_within_target` is `tokens <= target`, and every other fixture here is comfortably
    on one side or the other - so flipping that `<=` to `<` left the whole module green
    when it was tried at the wave-12 barrier. A history sitting exactly on the target is
    the only input that tells the two apart, and it is not a hypothetical: the target is
    where the previous rung was aiming, so landing on it is the ordinary outcome of a
    ladder that worked.

    Getting it wrong costs a rung - and at L3 a rung is a model call - for a history that
    was already exactly as small as the policy asked for.
    """
    policy = CompactionPolicy()
    exactly_on_target = int(WINDOW * policy.target_fraction)
    ladder = _Ladder(exactly_on_target, {Rung.L1_PRUNE_TOOL_OUTPUT: 5_000})

    run = climb_ladder(
        policy,
        tokens_before=exactly_on_target,
        context_window=WINDOW,
        apply_rung=ladder,
    )

    assert ladder.attempted == [], (
        "the ladder spent a rung on a history that already met the target exactly; "
        "`is_within_target` must be inclusive"
    )
    assert run.rungs_applied == ()
    assert run.reached_target is True
    assert run.tokens_after == exactly_on_target


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_ladder_that_frees_nothing_reports_no_progress() -> None:
    """`made_progress` False is the caller's stop signal, and it is a strict comparison.

    Everywhere else in this file `made_progress` is asserted True, so widening it to
    `tokens_after <= tokens_before` - which makes a ladder that freed NOTHING report
    progress - left the module green when it was tried. That widening is the most
    expensive failure this system has: `CompactionResult.made_progress` documents that a
    caller must not retry on False, and a False that never arrives is an infinite loop
    which re-invalidates the prompt cache on every pass.
    """
    policy = CompactionPolicy()
    ladder = _Ladder(90_000, {})  # every rung runs and every rung frees zero

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == list(Rung)
    assert run.tokens_after == run.tokens_before == 90_000
    assert run.reached_target is False
    assert run.made_progress is False, (
        "a ladder that freed nothing reported progress. The caller retries on that, and "
        "each retry rewrites the prefix and re-bills the whole prompt."
    )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_ladder_ends_when_the_rungs_run_out_without_reaching_target() -> None:
    """`reached_target is False` is what the caller escalates on. It must NOT be
    confused with no progress: tokens did fall, the ladder simply has nothing cheaper
    left to try."""
    policy = CompactionPolicy()
    ladder = _Ladder(90_000, dict.fromkeys(Rung, 5_000))

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L3_SUMMARISE_MIDDLE,
        Rung.L4_ITERATIVE_RESUMMARY,
    ]
    assert run.reached_target is False
    assert run.tokens_after == 70_000
    assert run.made_progress is True


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_rung_the_profile_disabled_is_never_climbed() -> None:
    """A profile that switches off the summarising rungs must not have them climbed
    behind its back - that is a model call the operator declined to pay for."""
    policy = CompactionPolicy(
        enabled_rungs=(Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L3_SUMMARISE_MIDDLE)
    )
    ladder = _Ladder(
        90_000,
        {Rung.L1_PRUNE_TOOL_OUTPUT: 1_000, Rung.L3_SUMMARISE_MIDDLE: 60_000},
    )

    run = climb_ladder(
        policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder
    )

    assert ladder.attempted == [Rung.L1_PRUNE_TOOL_OUTPUT, Rung.L3_SUMMARISE_MIDDLE]
    assert run.reached_target is True


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_rungs_are_climbed_cheapest_first_whatever_order_the_profile_declared() -> None:
    """The ladder's order is a property of the ladder, not of how a YAML file happened to
    list it. A profile listing L4 first must not buy a summary before trying free pruning
    - and because compaction runs inside a DBOS step, a declaration-ordered climb would
    also replay differently after a crash."""
    # L2 is declared TWICE on purpose. The rule is `sorted(set(...))`, and a fixture with
    # no duplicate cannot tell that apart from a bare `sorted(...)` - a profile that lists
    # a rung twice would then be charged for it twice, and at L3/L4 that is a second model
    # call for a summary the first one already produced.
    policy = CompactionPolicy(
        enabled_rungs=(
            Rung.L4_ITERATIVE_RESUMMARY,
            Rung.L2_SLIDING_WINDOW,
            Rung.L1_PRUNE_TOOL_OUTPUT,
            Rung.L2_SLIDING_WINDOW,
        )
    )
    ladder = _Ladder(90_000, dict.fromkeys(Rung, 1_000))

    climb_ladder(policy, tokens_before=90_000, context_window=WINDOW, apply_rung=ladder)

    assert ladder.attempted == [
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L4_ITERATIVE_RESUMMARY,
    ]


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_adapters_async_twin_climbs_in_the_same_order_as_the_domain_ladder() -> None:
    """The same determinism rule, asserted against the ladder production actually runs.

    `domain.climb_ladder` takes a SYNC `apply_rung`, so `LadderContextEngine` carries an
    async twin of the loop (D13: L3 and L4 await a model). `compress` climbs through the
    twin, never through the domain function - so every assertion above this line is about
    a loop the running system does not use.

    THIS WAS A REAL HOLE, not a hypothetical one. At the wave-12 barrier, deleting
    `sorted(set(...))` from `climb_ladder_async` was the ONE mutation in the compaction
    subsystem that no test anywhere in the tree caught: this module's fixtures reach the
    engine only through already-sorted `enabled_rungs`, and
    `test_context_engine.py::test_the_async_climb_agrees_with_the_domain_ladder` pins the
    twin against the domain over the DEFAULT policy, whose rungs are already in order. Two
    ladders in a silent-bug area, and only one of them was pinned - which is exactly the
    divergence `docs/STATE.md` lists as an open, unowned risk.

    It is CLAUDE.md non-negotiable #7 as well as money: compaction runs inside a
    `@DBOS.step()`, and an order taken from however a profile happened to list its rungs
    would replay a different conversation after a crash.
    """
    declared = (
        Rung.L4_ITERATIVE_RESUMMARY,
        Rung.L2_SLIDING_WINDOW,
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,  # duplicated, as above: `set` is half the rule
    )
    policy = CompactionPolicy(target_fraction=0.01, enabled_rungs=declared)
    attempted: list[Rung] = []

    async def apply_rung(rung: Rung) -> int:
        attempted.append(rung)
        return 90_000 - 1_000 * len(attempted)

    run = asyncio.run(
        LadderContextEngine.climb_ladder_async(
            policy, tokens_before=90_000, context_window=WINDOW, apply_rung=apply_rung
        )
    )

    expected = [
        Rung.L1_PRUNE_TOOL_OUTPUT,
        Rung.L2_SLIDING_WINDOW,
        Rung.L4_ITERATIVE_RESUMMARY,
    ]
    assert attempted == expected, (
        f"the engine's ladder climbed {[step.name for step in attempted]}. It must climb "
        f"the LADDER's order, deduplicated, whatever order the profile declared "
        f"({[step.name for step in declared]}) - both because a paid rung must never run "
        f"before a free one, and because a @DBOS.step() replaying in a different order "
        f"replays a different conversation."
    )
    assert run.rungs_applied == tuple(expected)


# ---------------------------------------------------------------------------
# t-f5-05 - pairing, per rung
# ---------------------------------------------------------------------------


class _FakeSummariser:
    """The cheap model behind L3 and L4, faked.

    Written against the ADAPTER'S `Summariser` alias rather than waiting for the real
    adapter (`docs/TASKS.md#t-f5-10`, landing in this same wave): the pairing invariant is
    a property of where the ladder CUTS, and it must be provable without any particular
    summariser existing. A fake that never lands leaves L3 and L4 untested; a fake that
    lands here tests them today.

    The returned text is a FIXED WIDTH on purpose. Every paid rung in the engine is
    reverted when it would return a larger history than it received, so a summary that
    grew between L3 and L4 would silently un-apply L4 and the test would assert pairing
    over a history L4 never touched.
    """

    def __init__(self) -> None:
        self.requests: list[SummaryRequest] = []

    async def __call__(self, request: SummaryRequest) -> str:
        self.requests.append(request)
        return f"summary-{len(self.requests):04d}"


def _exchange(
    index: int, *, user_chars: int, call_chars: int, tool_chars: int
) -> list[ModelMessage]:
    """One complete exchange: a user turn, a tool call, its return, and an answer.

    The call and its return are deliberately in DIFFERENT messages. That is the shape
    CLAUDE.md #5 is about - a cut between them makes the provider reject the whole
    conversation with a 400, far from the compaction that caused it.

    `call_chars` sizes the tool ARGUMENTS, and the pairing fixture below makes them the
    largest message in the exchange. That is not decoration. A window that counts tokens
    over MESSAGES walks backwards until its budget runs out, and it runs out in front of
    the biggest item it meets - so an oversized argument is what forces such a window to
    stop between a return and the call that asked for it, which is the orphan this test
    exists to catch. With a small argument the same broken window happens to stop on an
    exchange boundary and the test passes while the bug is present.
    """
    call_id = f"call-{index}"
    return [
        ModelRequest(parts=[UserPromptPart(content="u" * user_chars)]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="search",
                    args={"q": "q" * call_chars},
                    tool_call_id=call_id,
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search", content="o" * tool_chars, tool_call_id=call_id
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content=f"answer {index}")]),
    ]


def _history(
    count: int, *, user_chars: int, call_chars: int, tool_chars: int
) -> list[ModelMessage]:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="you are an agent")])
    ]
    for index in range(count):
        messages.extend(
            _exchange(
                index,
                user_chars=user_chars,
                call_chars=call_chars,
                tool_chars=tool_chars,
            )
        )
    return messages


def _as_history(value: object) -> list[ModelMessage]:
    """Narrow `CompactionResult.compacted_history`, which the port types as `object`."""
    assert isinstance(value, list), f"compacted_history is {type(value).__name__}"
    messages: list[ModelMessage] = []
    for message in value:
        assert isinstance(message, (ModelRequest, ModelResponse))
        messages.append(message)
    return messages


def _call_ids(history: Sequence[ModelMessage]) -> set[str]:
    return {
        part.tool_call_id
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }


def _return_ids(history: Sequence[ModelMessage]) -> set[str]:
    return {
        part.tool_call_id
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


def _ordered_call_ids(history: Sequence[ModelMessage]) -> list[str]:
    """Tool call ids in the order they appear, so two histories can be compared as
    SEQUENCES rather than as sets."""
    return [
        part.tool_call_id
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]


def _out_of_order_pairs(history: Sequence[ModelMessage]) -> set[str]:
    """Ids whose return does not come strictly AFTER the call that asked for it.

    Set membership is not the whole invariant. A return that precedes its call is the same
    provider 400 as a return with no call at all, and a rung that reorders messages - a
    summary spliced at the wrong index, a tail concatenated before a head - produces
    exactly that while leaving both ids present.
    """
    first_call: dict[str, int] = {}
    first_return: dict[str, int] = {}
    for position, message in enumerate(history):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                first_call.setdefault(part.tool_call_id, position)
            elif isinstance(part, ToolReturnPart):
                first_return.setdefault(part.tool_call_id, position)
    return {
        call_id
        for call_id, call_at in first_call.items()
        if call_id in first_return and first_return[call_id] <= call_at
    }


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_pairing_check_itself_fails_on_a_history_with_an_orphan() -> None:
    """The integrity check on every assertion below it.

    A pairing test over a ladder that cuts whole exchanges can only ever pass, so on its
    own it is indistinguishable from a test that cannot fail - the exact defect
    `docs/STATE.md` records against the transcript visibility guard. This pins that the
    two id sets really do disagree when a call loses its return and when a return loses
    its call.
    """
    paired = _history(2, user_chars=10, call_chars=10, tool_chars=10)

    assert _call_ids(paired) == _return_ids(paired)
    assert len(_call_ids(paired)) == 2

    # Drop the tool RETURN of the first exchange: index 0 is the system preamble, so the
    # first exchange occupies 1..4 and its return is at index 3.
    call_without_return = [*paired[:3], *paired[4:]]
    assert _call_ids(call_without_return) - _return_ids(call_without_return) == {"call-0"}

    # Drop the tool CALL instead: the mirror image, and the one a "keep the last N
    # messages" window produces.
    return_without_call = [*paired[:2], *paired[3:]]
    assert _return_ids(return_without_call) - _call_ids(return_without_call) == {"call-0"}

    # Both ids present, wrong way round: the set check alone calls this paired.
    assert _out_of_order_pairs(paired) == set()
    swapped = [*paired[:2], paired[3], paired[2], *paired[4:]]
    assert _call_ids(swapped) == _return_ids(swapped)
    assert _out_of_order_pairs(swapped) == {"call-0"}


@pytest.mark.silent
@pytest.mark.phase("F5")
@pytest.mark.parametrize("rung", list(Rung), ids=[rung.name for rung in Rung])
def test_every_rung_leaves_tool_calls_paired_with_their_returns(rung: Rung) -> None:
    """CLAUDE.md non-negotiable #5, asserted once per rung of the ladder.

    THE LADDER IS CLIMBED AS A PREFIX, NOT A SINGLE RUNG. L4 folds material that L2 and L3
    aged, so in isolation it has nothing to fold, frees nothing, and would prove nothing
    at all - a green assertion over a history the rung never touched. Each parametrisation
    therefore enables L1 through the named rung and drives the whole prefix, so a failure
    names the CHEAPEST rung that breaks pairing.

    Three guards keep the assertion from being vacuous, and each one has already been the
    defect somewhere in this repository:

    * the named rung must appear in `rungs_applied` - otherwise a rung that silently did
      not run would pass;
    * the paid rungs must have CALLED the summariser - `docs/STATE.md` records L3 and L4
      inert in production for ten waves because the collaborator behind them was `None`,
      and every test still passed;
    * the compacted history must still contain a tool pair - pairing over a history with
      no tools left is true and worthless.

    L1 is the one rung that cannot fail this by construction - it rewrites tool results in
    place and drops nothing - and it is asserted anyway, because "cannot" is a property of
    today's implementation and this is the file that would notice if it changed.

    It does NOT prove the compacted history is one a provider will accept. See the module
    header.
    """
    policy = CompactionPolicy(
        target_fraction=0.01,  # unreachable, so every enabled rung is climbed
        enabled_rungs=tuple(step for step in Rung if step <= rung),
    )
    summariser = _FakeSummariser()
    engine = LadderContextEngine(summariser, context_window=WINDOW)

    history = _history(10, user_chars=200, call_chars=12_000, tool_chars=2_000)
    original_pairs = _call_ids(history)
    assert len(original_pairs) == 10

    result = asyncio.run(engine.compress(SESSION, history, policy))
    compacted = _as_history(result.compacted_history)

    assert rung in result.rungs_applied, (
        f"{rung.name} never ran, so this test asserted pairing over a history it never "
        f"touched. Rungs applied: {[step.name for step in result.rungs_applied]}."
    )

    paid_rungs = [step for step in result.rungs_applied if step >= Rung.L3_SUMMARISE_MIDDLE]
    assert len(summariser.requests) == len(paid_rungs), (
        f"{rung.name}: the summariser was called {len(summariser.requests)} times for "
        f"{len(paid_rungs)} paid rungs. A paid rung that never reached its collaborator "
        f"is inert - docs/TASKS.md#t-f5-10."
    )

    calls = _call_ids(compacted)
    returns = _return_ids(compacted)

    assert calls - returns == set(), (
        f"{rung.name} left tool calls with no return: {sorted(calls - returns)}. "
        "The provider rejects the whole conversation with a 400. CLAUDE.md #5."
    )
    assert returns - calls == set(), (
        f"{rung.name} left tool returns with no call: {sorted(returns - calls)}. "
        "The provider rejects the whole conversation with a 400. CLAUDE.md #5."
    )
    assert _out_of_order_pairs(compacted) == set(), (
        f"{rung.name} left a tool return ahead of its call: "
        f"{sorted(_out_of_order_pairs(compacted))}. Both ids are present, so the set "
        "check above passes and the provider still rejects the conversation."
    )
    assert calls, (
        f"{rung.name} left no tool pair at all, so pairing held vacuously. Give the "
        "fixture a history whose head and tail both carry a tool call."
    )
    assert calls <= original_pairs

    # Two properties the pairing assertions above are structurally blind to, and both were
    # PROVEN blind by mutation at the wave-12 barrier: a rung that drops whole exchanges,
    # or reorders them, leaves every surviving pair intact and every set comparison happy.
    #
    #   1. THE NEWEST EXCHANGE SURVIVES. `slide_window` says so in its own comment -
    #      "dropping the turn the model is answering does not compact a conversation, it
    #      breaks one" - and nothing here noticed when `self.tail = live[tail_start:]`
    #      became `live[tail_start:-1]`, or when L3's `self.tail = live[-1:]` became `()`.
    #      Both mutations produce a shorter, perfectly paired history that answers the
    #      wrong question, and the ladder's own stopping rule likes them BETTER.
    #   2. ORDER IS PRESERVED. `_out_of_order_pairs` compares a call against its OWN
    #      return, so swapping two whole exchanges passes it - and a conversation whose
    #      turns arrive out of sequence is not one the model can answer, whatever the
    #      pairing says.
    newest = _ordered_call_ids(history)[-1]
    assert newest in calls, (
        f"{rung.name} dropped the most recent exchange ({newest}). That is the turn the "
        f"model is about to answer: cutting it is not compaction, and it satisfies every "
        f"pairing assertion above while making the history smaller."
    )
    assert _ordered_call_ids(compacted) == [
        call_id for call_id in _ordered_call_ids(history) if call_id in calls
    ], (
        f"{rung.name} reordered the conversation. Every surviving pair is still intact, "
        f"so the pairing assertions above cannot see it, and the model is handed turns "
        f"that answer questions it has not been asked yet."
    )


# ---------------------------------------------------------------------------
# t-f5-06 - the bounded-resend proxy. NOT the cost criterion; that is t-f5-09.
# ---------------------------------------------------------------------------

TURNS = 200


def _simulated_turns(
    engine: LadderContextEngine, policy: CompactionPolicy, *, turns: int
) -> tuple[list[int], int, list[ModelMessage]]:
    """Drive `turns` exchanges through the engine the way the runner does.

    The `ContextState` built here is the one `runner.py::_history_processor` builds: the
    estimate comes from the MESSAGES about to be sent, and `window_used` stays None
    because no provider LiteLLM fronts reports it. That is the common case, and it is the
    path the port's mandatory fallback exists for.

    Returns the tokens resent on each turn, how many passes actually rewrote the prefix,
    and the history the last turn was sent with. `made_progress` False ends the asking,
    exactly as the runner does: a compaction that freed nothing and is retried is an
    infinite loop that ALSO re-invalidates the prompt cache on every pass.
    """
    history: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="you are an agent")])
    ]
    resent: list[int] = []
    rewrites = 0

    for index in range(turns):
        history.extend(
            _exchange(index, user_chars=1_200, call_chars=40, tool_chars=2_400)
        )
        state = ContextState(
            session=SESSION,
            window_used=None,
            estimated_tokens=estimate_tokens(history),
            context_window=WINDOW,
            message_count=len(history),
            passes_so_far=rewrites,
        )
        if engine.should_compress(state, policy):
            result = asyncio.run(engine.compress(SESSION, history, policy))
            if result.made_progress:
                history = _as_history(result.compacted_history)
                rewrites += 1
        resent.append(estimate_tokens(history))

    return resent, rewrites, history


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_tokens_resent_stay_bounded_over_two_hundred_turns() -> None:
    """Two hundred turns, and the prompt never grows past what the trigger permits.

    THE BOUND IS DERIVED, NOT CHOSEN. Compaction fires when the estimate crosses
    `trigger_fraction` of the window, and the largest history that can ever be sent is
    therefore one that had not crossed it yet plus the exchange just appended. A ladder
    that stopped working shows up here as linear growth: turn 200 of an uncompacted
    conversation is roughly two hundred times turn one, and it fails this assertion by a
    factor, not by a margin.

    WHAT IT DOES NOT PROVE: that this is CHEAPER. Tokens resent is not money - a provider
    prompt cache makes an unchanged prefix nearly free, and every compaction pass destroys
    that cache. A simulated conversation has no cache to destroy, so this test cannot see
    the mechanism the phase is actually worried about. `docs/TASKS.md#t-f5-09` measures
    the bill; nothing here substitutes for it.
    """
    policy = CompactionPolicy()
    engine = LadderContextEngine(_FakeSummariser(), context_window=WINDOW)

    resent, rewrites, final = _simulated_turns(engine, policy, turns=TURNS)

    one_exchange = estimate_tokens(
        _exchange(0, user_chars=1_200, call_chars=40, tool_chars=2_400)
    )
    bound = int(WINDOW * policy.trigger_fraction) + one_exchange

    assert len(resent) == TURNS
    assert max(resent) <= bound, (
        f"the largest prompt resent was {max(resent)} tokens against a derived bound of "
        f"{bound} (trigger {policy.trigger_fraction} of a {WINDOW}-token window, plus the "
        f"exchange that crossed it). The history is growing unbounded."
    )
    # Integrity: the run must have crossed the trigger at all, or the bound above held
    # because nothing ever happened.
    assert rewrites >= 1
    assert max(resent) > int(WINDOW * policy.target_fraction)
    # And the last turn survived. Dropping the exchange the model is answering does not
    # compact a conversation, it breaks one - and it would also satisfy the bound above.
    assert f"call-{TURNS - 1}" in _call_ids(final)
    assert _call_ids(final) == _return_ids(final)


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_prefix_is_rewritten_no_more_often_than_the_trigger_allows() -> None:
    """The other half of `t-f5-06`, and the one the phase's warning is really about.

    Every pass rewrites the prompt prefix and invalidates the provider's cache, so the
    next request re-bills the whole prompt at full price. Compacting per turn therefore
    costs MORE than it saves - Hermes ships that mode off by default for exactly this
    reason - and it is invisible to every other assertion in this suite, because a
    per-turn ladder produces a SMALLER history and looks better on headroom.

    THE CEILING IS ARITHMETIC FROM THE POLICY, NOT A TUNED NUMBER. A pass may only fire
    once the estimate has climbed to `trigger_fraction` of the window, and it must bring
    it to `target_fraction`, so each pass absorbs at least `(trigger - target) * window`
    tokens of growth. Two hundred exchanges add a known amount, and the quotient plus one
    for the partially-filled last interval is the most passes the trigger can permit.

    It does NOT prove the passes were cheap, or that this is the right trigger. It proves
    the ladder is not being climbed per turn behind our backs.
    """
    policy = CompactionPolicy()
    engine = LadderContextEngine(_FakeSummariser(), context_window=WINDOW)

    _, rewrites, _final = _simulated_turns(engine, policy, turns=TURNS)

    one_exchange = estimate_tokens(
        _exchange(0, user_chars=1_200, call_chars=40, tool_chars=2_400)
    )
    absorbed_per_pass = int(
        WINDOW * (policy.trigger_fraction - policy.target_fraction)
    )
    ceiling = math.ceil(TURNS * one_exchange / absorbed_per_pass) + 1

    assert 1 <= rewrites <= ceiling, (
        f"the prefix was rewritten {rewrites} times over {TURNS} turns; the trigger "
        f"permits at most {ceiling} (each pass absorbs {absorbed_per_pass} tokens of "
        f"growth and the run added {TURNS * one_exchange}). A count near {TURNS} means "
        "the ladder is running per turn and the provider cache is destroyed every turn."
    )


# ---------------------------------------------------------------------------
# Stubs belonging to other anchors. Left as filed; not this task's to close.
# ---------------------------------------------------------------------------


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_should_compress_falls_back_when_window_used_is_none() -> None:
    """`context_window_used` is None on providers that do not report usage - COMMON, not
    exceptional. Without the local estimate fallback the agent never compacts and dies of
    overflow in production, never in tests."""


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_head_and_tail_are_never_summarised() -> None:
    """The head carries the task definition. Summarising it is how an agent forgets what
    it was asked to do."""


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_l4_folds_the_previous_summary_instead_of_appending() -> None:
    """This is what keeps the summary current rather than frozen, and stops it repeating
    what it already said."""


@pytest.mark.silent
@pytest.mark.skip(reason="F5")
def test_no_progress_does_not_retry() -> None:
    """A compaction that frees nothing and is retried is an infinite loop that ALSO
    invalidates the prompt cache on every pass - the most expensive failure this system
    has."""
