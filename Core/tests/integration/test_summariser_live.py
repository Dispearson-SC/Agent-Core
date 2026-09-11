"""Integration: `ModelSummariser`'s default `Completion` reaches the real provider.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-10

WHY THIS FILE EXISTS
    `tests/conftest.py` forbids a network in `tests/unit/`, and every test in
    `tests/unit/test_summariser.py` injects its own `complete`, so the default
    `Completion` this adapter builds - `ModelSummariser._model_request`, which reaches
    MiniMax through `agent_core.adapters.driven.llm_litellm.models.model_for` and
    `pydantic_ai.direct.model_request` - is exercised by NOTHING. That is the same gap
    the injected model factory left at `t-f0-04`: the code path production actually uses
    on every L3/L4 pass is the one no test drives. docs/STATE.md names this outright.

WHAT THIS PROVES
    A history built the same way `tests/unit/test_summariser.py` builds one - with a
    complete tool call/return exchange inside it - runs through the REAL
    `LadderContextEngine.compress`, with a `ModelSummariser` built with NO injected
    `complete`, so the request actually leaves the process for MiniMax through LiteLLM:

      a. the live model answers, and the resulting summary/history is strictly SHORTER
         than what went in - a live provider that is reached but never actually
         summarises (e.g. echoes the prompt back) would fail this;
      b. CLAUDE.md non-negotiable #5: no tool call is separated from its return in the
         compacted history - every call still has its return, in order, none dropped.

COST
    One provider call. `CompactionPolicy(target_fraction=0.01)` is the same forcing
    policy `tests/unit/test_summariser.py` uses so the paid rungs actually run, and both
    `max_summary_chars` and `max_input_chars` are set small so the material sent and the
    tokens billed stay tiny.

SKIP GUARD
    Skips cleanly - mirrors `tests/integration/test_f0_end_to_end.py` - when no MiniMax
    credential is available. CI without a key must not fail.

THE CREDENTIAL
    Same resolution as `test_f0_end_to_end.py`: `MINIMAX_API_KEY` (LiteLLM's own name)
    wins if already set, otherwise the gitignored `.env`'s `MINIMAX_API` is read and
    mapped onto `MINIMAX_API_KEY` for the duration of the test only, via `monkeypatch`.
    The value is never asserted on, logged, or written anywhere.

WHY THE FILLER TEXT IS A REPEATED SENTENCE, NOT REPEATED CHARACTERS
    Verified against the live model while writing this file: `"u" * 8_000` as filler
    made M3's own `ThinkingPart` balloon - apparently trying to make sense of a wall of
    one repeated character - and consume the whole `max_tokens` budget, leaving nothing
    for the actual summary and tripping `ModelSummariser`'s empty-answer guard into
    `_extractive_fallback` nondeterministically, run to run. A repeated natural-language
    sentence does not trigger this and was reliable across more than five live runs. A
    real conversation never looks like raw character filler either, so this is also the
    more representative fixture.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

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
    estimate_tokens,
)
from agent_core.adapters.driven.context.summariser import ModelSummariser
from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway
from agent_core.domain.compaction import CompactionPolicy, Rung
from agent_core.domain.turn import SessionId, SessionRef, TenantId

pytestmark = [pytest.mark.phase("F5")]

SESSION = SessionRef(session_id=SessionId("s-summary-live"), tenant_id=TenantId("t-1"))
WINDOW = 100_000

# Same forcing policy as tests/unit/test_summariser.py: low enough that L1/L2 alone
# cannot reach the target, so the two PAID rungs - the ones this file exists to reach a
# real provider through - have to run.
FORCES_THE_PAID_RUNGS = CompactionPolicy(target_fraction=0.01)


def _repo_root() -> Path:
    """`Core/tests/integration/this_file.py` -> the repository root, three parents up."""
    return Path(__file__).resolve().parents[3]


def _minimax_api_key() -> str | None:
    """The provider credential, or None. Never asserted on, formatted, or written."""
    from_environment = os.environ.get("MINIMAX_API_KEY")
    if from_environment:
        return from_environment

    env_file = _repo_root() / ".env"
    if not env_file.exists():
        return None

    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "MINIMAX_API":
            return value.strip().strip('"').strip("'") or None
    return None


_needs_model = pytest.mark.skipif(
    _minimax_api_key() is None,
    reason="no MiniMax credential: set MINIMAX_API_KEY, or MINIMAX_API in the repo .env",
)

# See "WHY THE FILLER TEXT IS A REPEATED SENTENCE, NOT REPEATED CHARACTERS" above - a
# repeated natural sentence, not repeated single characters.
_FILLER_SENTENCE = (
    "The customer reported that order 4821 arrived with a cracked screen protector "
    "and asked for a replacement to be shipped within two business days. "
)


def _exchange(index: int, *, reps: int) -> list[ModelMessage]:
    """A user turn, a tool call, its return, and an answer - one complete exchange.

    Mirrors `tests/unit/test_summariser.py::_exchange`: the call and return live in the
    same exchange, which is exactly what non-negotiable #5 protects.
    """
    call_id = f"call-{index}"
    body = _FILLER_SENTENCE * reps
    return [
        ModelRequest(parts=[UserPromptPart(content=f"turn {index}: " + body)]),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="search", args={"q": index}, tool_call_id=call_id)
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search", content=body[:1_000], tool_call_id=call_id
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content=f"answer {index}")]),
    ]


def _history(count: int, *, reps: int) -> list[ModelMessage]:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="you are an agent")])
    ]
    for index in range(count):
        messages.extend(_exchange(index, reps=reps))
    return messages


def _call_ids(history: list[ModelMessage]) -> tuple[list[str], list[str]]:
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


@_needs_model
def test_the_live_summariser_reaches_minimax_and_shrinks_without_splitting_a_tool_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """t-f5-10's live-provider gap: nothing exercised `ModelSummariser`'s real route."""
    key = _minimax_api_key()
    assert key is not None  # the skipif above already guards this
    monkeypatch.setenv("MINIMAX_API_KEY", key)

    history = _history(4, reps=120)
    engine = LadderContextEngine(
        ModelSummariser(LiteLLMGateway(), max_summary_chars=1_500, max_input_chars=6_000),
        context_window=WINDOW,
    )

    result = asyncio.run(engine.compress(SESSION, history, FORCES_THE_PAID_RUNGS))
    compacted = _compacted(result.compacted_history)

    # The paid rung actually ran and produced a real (non-degraded) summary - a
    # provider that is unreachable would fall back to `_extractive_fallback`, which
    # this assertion rules out.
    assert Rung.L3_SUMMARISE_MIDDLE in result.rungs_applied
    assert result.checkpoint is not None
    assert result.checkpoint.summary
    assert not result.checkpoint.summary.startswith(
        "[summariser unavailable"
    ), "the live call fell back to the extractive path instead of reaching the provider"

    # (a) shorter than its input.
    assert result.tokens_after < estimate_tokens(history)

    # (b) CLAUDE.md non-negotiable #5: the summary never split a tool call from its
    # return. A reverted L3 would leave every pairing untouched trivially, so this is
    # only evidence together with the `rungs_applied` assertion above.
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
