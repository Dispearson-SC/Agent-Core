"""The summariser behind L3/L4 - a SILENT-BUG AREA (CLAUDE.md). Wrong here is a BILL.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-10

WHY THIS MODULE EXISTS AT ALL, WHICH IS THE WHOLE POINT OF THE ANCHOR

    `LadderContextEngine` takes a `Summariser` OPTIONALLY. No implementation existed
    anywhere in the tree and production was wired with `None`, so rungs L3 and L4 returned
    immediately on every turn and the top half of the ladder freed nothing. Every test
    passed. Nothing logged. The only witness was the invoice - which is exactly what
    CLAUDE.md's silent-bug table says about "compaction strategy".

    So the first test below asserts the DELTA, not an absolute: the same engine, the same
    policy and the same history, run once with `None` and once with this adapter. With
    `None`, `tokens_after` is whatever L2 left. With the adapter it is strictly smaller.
    An absolute assertion ("the history is small") would have been satisfied by the free
    rungs alone and would have certified the inert ladder as working, which is the failure
    this anchor is repairing.

    The second test is CLAUDE.md non-negotiable #5. L3 and L4 are the only rungs that
    INSERT a message into the history, and the insertion point is between the protected
    head and the recent tail. A summary that landed between a `ToolCallPart` and its
    `ToolReturnPart` - or a rung that dropped one of the pair - makes the provider reject
    the entire conversation with a 400, far away from the compaction that caused it. The
    engine argues structurally that it cannot happen; nothing had ever run the paid rungs
    with a real summariser and read the pairing back.

NO NETWORK. `tests/conftest.py` says a unit test that needs a real dependency means a port
is leaking. The model call is one injected seam (`complete`), so the adapter's own
behaviour - bounding, folding, routing, degrading - is provable without one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

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
from agent_core.adapters.driven.context.summariser import (
    DEFAULT_SUMMARISER_MODEL,
    ModelSummariser,
)
from agent_core.composition import build_container
from agent_core.domain.compaction import CompactionPolicy, Rung
from agent_core.domain.turn import SessionId, SessionRef, TenantId
from agent_core.ports.model_gateway import ModelAttempt, RecoveryStrategy

SESSION = SessionRef(session_id=SessionId("s-summary"), tenant_id=TenantId("t-1"))
WINDOW = 100_000

# 0.01 of a 100_000 window is a 1_000-token target: low enough that L1 and L2 cannot
# reach it and the two PAID rungs have to run. That is the only configuration in which
# this adapter is reachable at all.
FORCES_THE_PAID_RUNGS = CompactionPolicy(target_fraction=0.01)

ALL_FOUR_RUNGS = (
    Rung.L1_PRUNE_TOOL_OUTPUT,
    Rung.L2_SLIDING_WINDOW,
    Rung.L3_SUMMARISE_MIDDLE,
    Rung.L4_ITERATIVE_RESUMMARY,
)


class _Gateway:
    """`ModelGateway`, so the routing assertion is against the port and not a constant.

    This is not a fake of the summariser - it is a fake of the port the summariser asks
    WHERE to send the call. `model_id_for` collapsing `minimax/MiniMax-M3` to the bare
    proxy alias is `LiteLLMGateway`'s day-2 behaviour, and the summariser has to go
    through it rather than hard-coding either name.
    """

    def __init__(self, proxy_base_url: str | None = None) -> None:
        self._proxy_base_url = proxy_base_url

    def model_id_for(self, profile_model: str) -> str:
        if self._proxy_base_url is None:
            return profile_model
        _, _, alias = profile_model.partition("/")
        return alias or profile_model

    def base_url(self) -> str | None:
        return self._proxy_base_url

    def classify_error(
        self, error: Exception, attempt: ModelAttempt
    ) -> RecoveryStrategy:  # pragma: no cover - unused
        raise NotImplementedError


class _Completion:
    """The one model call, recorded and answered without a network."""

    def __init__(self, answer: str = "the conversation so far, condensed") -> None:
        self.calls: list[tuple[str, str | None, list[ModelMessage]]] = []
        self._answer = answer

    async def __call__(
        self, model_id: str, base_url: str | None, messages: Sequence[ModelMessage]
    ) -> str:
        self.calls.append((model_id, base_url, list(messages)))
        return self._answer

    @property
    def prompts(self) -> list[str]:
        """Every character this adapter actually put in front of the model."""
        return [
            "\n".join(
                str(getattr(part, "content", ""))
                for message in messages
                for part in message.parts
            )
            for _model, _base, messages in self.calls
        ]


class _Failing:
    """A provider that is down. `compress` puts no handler around the summariser."""

    async def __call__(
        self, model_id: str, base_url: str | None, messages: Sequence[ModelMessage]
    ) -> str:
        raise RuntimeError("upstream 503")


def _exchange(index: int, *, user_chars: int, tool_chars: int) -> list[ModelMessage]:
    """A user turn, a tool call, its return, and an answer - one complete exchange.

    The call and the return are deliberately in the same exchange: that pairing is what
    non-negotiable #5 protects and what the second test below reads back.
    """
    call_id = f"call-{index}"
    return [
        ModelRequest(
            parts=[UserPromptPart(content=f"question {index} " + "u" * user_chars)]
        ),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="search", args={"q": index}, tool_call_id=call_id)
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


def _history(count: int, *, user_chars: int, tool_chars: int) -> list[ModelMessage]:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="you are an agent")])
    ]
    for index in range(count):
        messages.extend(_exchange(index, user_chars=user_chars, tool_chars=tool_chars))
    return messages


def _call_ids(history: Sequence[ModelMessage]) -> tuple[list[str], list[str]]:
    calls = [
        part.tool_call_id
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    returns = [
        part.tool_call_id
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    return calls, returns


def _compacted(history: object) -> list[ModelMessage]:
    """`CompactionResult.compacted_history` is typed `object`; narrow it once."""
    assert isinstance(history, list)
    return history


@pytest.mark.phase("F5")
def test_the_production_container_wires_a_real_summariser_into_the_ladder() -> None:
    """The seat this whole module exists to fill, checked where production actually
    builds it - not just that this adapter WORKS in isolation.

    docs/STATE.md's second outranking finding: `composition.py` constructed
    `LadderContextEngine()` with no summariser, so L3 and L4 froze free on every real
    turn while every test - including every other one in this file - passed, because
    they all build the engine directly with an injected `ModelSummariser` and never
    touch the wiring. `build_container()` is production's own path: no pool_factory
    override, no test double substituted for the engine.

    No port exposes the seat - `ContextEngine` says nothing about a `Summariser`, that
    being `LadderContextEngine`'s own construction detail - so this reaches the
    adapter's attribute directly rather than running a real compaction pass through a
    live model, which a unit test must not do (`tests/conftest.py`). An optional seat
    left at `None` is exactly what this assertion is here to catch if it recurs.
    """
    container = build_container()
    try:
        assert isinstance(container.context, LadderContextEngine)
        summariser = container.context._summariser  # noqa: SLF001 - see docstring
        assert isinstance(summariser, ModelSummariser), (
            "LadderContextEngine was built with no summariser (or the wrong type): "
            "rungs L3 and L4 free nothing on every real turn. See composition.py's "
            "WHAT IS WIRED BUT DEGRADED note."
        )
    finally:
        container.domain_pool.close()
        container.audit_pool.close()


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_l3_and_l4_free_tokens_only_once_a_summariser_is_present() -> None:
    """The anchor, stated as a delta so the inert ladder cannot satisfy it.

    Both runs climb all four rungs. Without a summariser the two paid rungs return
    immediately and `tokens_after` is whatever L2 left - which is what production has been
    doing on every turn. With this adapter they buy real room, and a checkpoint exists to
    prove a summary was actually produced rather than the material silently dropped.
    """
    history = _history(8, user_chars=40_000, tool_chars=4_000)

    inert = asyncio.run(
        LadderContextEngine(None, context_window=WINDOW).compress(
            SESSION, history, FORCES_THE_PAID_RUNGS
        )
    )
    summarised = asyncio.run(
        LadderContextEngine(
            ModelSummariser(_Gateway(), complete=_Completion()), context_window=WINDOW
        ).compress(SESSION, history, FORCES_THE_PAID_RUNGS)
    )

    assert inert.rungs_applied == ALL_FOUR_RUNGS
    assert summarised.rungs_applied == ALL_FOUR_RUNGS

    # The inert ladder: the two paid rungs changed nothing at all.
    assert inert.checkpoint is None

    # The repaired ladder: strictly smaller than what the free rungs alone could reach.
    assert summarised.tokens_after < inert.tokens_after
    assert summarised.checkpoint is not None
    assert summarised.checkpoint.summary


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_summary_never_splits_a_tool_call_from_its_return() -> None:
    """CLAUDE.md non-negotiable #5, on the only path that inserts a message.

    Every tool call left in the compacted history still has its return, every return still
    has its call, and no return arrives before the call that asked for it. A provider
    rejects the whole conversation with a 400 otherwise, long after the compaction that
    caused it.
    """
    history = _history(8, user_chars=40_000, tool_chars=4_000)
    engine = LadderContextEngine(
        ModelSummariser(_Gateway(), complete=_Completion()), context_window=WINDOW
    )

    result = asyncio.run(engine.compress(SESSION, history, FORCES_THE_PAID_RUNGS))
    compacted = _compacted(result.compacted_history)

    # The pairing check is only evidence if a summary was actually INSERTED. A reverted
    # L3 leaves the history untouched and would satisfy every assertion below while
    # testing nothing.
    assert Rung.L3_SUMMARISE_MIDDLE in result.rungs_applied
    assert result.checkpoint is not None
    carriers = [
        message
        for message in compacted
        if any(
            result.checkpoint.summary in str(getattr(part, "content", ""))
            for part in message.parts
        )
    ]
    assert len(carriers) == 1

    calls, returns = _call_ids(compacted)
    assert calls, "the paid rungs dropped every exchange; this would prove nothing"
    assert calls == returns

    seen: set[str] = set()
    for message in compacted:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                seen.add(part.tool_call_id)
            elif isinstance(part, ToolReturnPart):
                assert part.tool_call_id in seen, (
                    f"{part.tool_call_id} returned before it was called"
                )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_summary_is_bounded_even_when_the_model_ignores_the_instruction() -> None:
    """A prompt asking for brevity is a request, not a guarantee.

    An unbounded summary makes L3 return a history bigger than it received, the engine's
    own guard reverts the rung, and the model call is paid for nothing - the exact trap
    docs/TASKS.md warns about under F5. The bound is enforced here, not hoped for.
    """
    summariser = ModelSummariser(
        _Gateway(), max_summary_chars=800, complete=_Completion("x" * 500_000)
    )

    summary = asyncio.run(
        summariser(SummaryRequest(text="a" * 200_000, model=None, previous_summary=None))
    )

    assert 0 < len(summary) <= 800

    # And through the engine: the rung still buys room rather than being reverted.
    history = _history(8, user_chars=40_000, tool_chars=4_000)
    engine = LadderContextEngine(summariser, context_window=WINDOW)
    result = asyncio.run(engine.compress(SESSION, history, FORCES_THE_PAID_RUNGS))
    assert result.checkpoint is not None


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_material_shown_to_the_model_is_bounded_too() -> None:
    """The input side of the same trap, and the one that scales with the conversation.

    A 200-turn history rendered whole into a summarisation prompt costs more to send than
    the compaction frees. What is shown is capped; the opening and the most recent
    material survive, because those are what a summary is for.
    """
    completion = _Completion()
    summariser = ModelSummariser(_Gateway(), max_input_chars=4_000, complete=completion)

    material = "OPENING\n" + ("m" * 200_000) + "\nCLOSING"
    asyncio.run(
        summariser(SummaryRequest(text=material, model=None, previous_summary=None))
    )

    prompt = completion.prompts[0]
    assert len(prompt) < 10_000
    assert "OPENING" in prompt
    assert "CLOSING" in prompt


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_previous_summary_is_folded_in_rather_than_restated() -> None:
    """L4's entire reason to exist: the aging summary is an INPUT to the new one."""
    completion = _Completion()
    summariser = ModelSummariser(_Gateway(), complete=completion)

    asyncio.run(
        summariser(
            SummaryRequest(
                text="new material since the last pass",
                model=None,
                previous_summary="WHAT-WAS-ALREADY-KNOWN",
            )
        )
    )

    assert "WHAT-WAS-ALREADY-KNOWN" in completion.prompts[0]
    assert "new material since the last pass" in completion.prompts[0]


@pytest.mark.phase("F5")
def test_the_route_is_the_gateways_and_the_policys_model_wins() -> None:
    """Which model is decided by the port and the profile - never hard-coded here.

    Day 2 the proxy serves a bare alias and the `minimax/` prefix is stripped by
    `ModelGateway.model_id_for`; `CompactionPolicy.summariser_model` arrives on
    `SummaryRequest.model` and overrides the default when a deployment is granted
    something cheaper.
    """
    library = _Completion()
    asyncio.run(
        ModelSummariser(_Gateway(), complete=library)(
            SummaryRequest(text="material", model=None, previous_summary=None)
        )
    )
    assert library.calls[0][0] == DEFAULT_SUMMARISER_MODEL
    assert library.calls[0][1] is None

    proxied = _Completion()
    asyncio.run(
        ModelSummariser(_Gateway("http://proxy.local"), complete=proxied)(
            SummaryRequest(
                text="material", model="minimax/MiniMax-M3", previous_summary=None
            )
        )
    )
    assert proxied.calls[0][0] == "MiniMax-M3"
    assert proxied.calls[0][1] == "http://proxy.local"


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_provider_failure_degrades_instead_of_failing_the_turn() -> None:
    """`LadderContextEngine.compress` puts no handler around the summariser call.

    An exception raised here would leave the turn with no compaction AND no answer, which
    is strictly worse than the inert ladder this anchor is repairing. The adapter answers
    with a deterministic extractive fallback instead: bounded, marked as degraded, and the
    same on every replay of the DBOS step that produced it.
    """
    summariser = ModelSummariser(_Gateway(), max_summary_chars=600, complete=_Failing())
    request = SummaryRequest(
        text="OPENING\n" + "m" * 100_000, model=None, previous_summary=None
    )

    first = asyncio.run(summariser(request))
    second = asyncio.run(summariser(request))

    assert 0 < len(first) <= 600
    assert first == second

    history = _history(8, user_chars=40_000, tool_chars=4_000)
    engine = LadderContextEngine(summariser, context_window=WINDOW)
    result = asyncio.run(engine.compress(SESSION, history, FORCES_THE_PAID_RUNGS))

    assert result.tokens_after < estimate_tokens(history)
    assert result.checkpoint is not None
