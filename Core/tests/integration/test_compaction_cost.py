"""Integration: t-f5-09 - measure what compaction actually costs, against a real bill.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-09

WHAT THIS FILE IS, AND WHAT IT IS NOT
    This is a MEASUREMENT HARNESS, not an assertion about cost. `docs/TASKS.md` and
    `domain/compaction.py` both warn that compacting can cost MORE than it saves, because
    every pass rewrites the prompt prefix and breaks the provider's cache - Hermes ships
    per-exchange compaction OFF BY DEFAULT for exactly this reason. Whether the ladder
    saves money AT ITS CURRENT TRIGGER, over a real conversation, against a real provider,
    is an empirical question this file exists to answer - and disproving the hope that it
    always helps is a GOOD outcome, not a failed one. The numbers this harness produces go
    into `docs/FIELD-NOTES.md` by hand, with their date, model and traffic; this test
    asserts only that the harness ran end to end and that the numbers it produced are
    internally consistent - never a specific price, which moves out from under a fixed
    assertion the day a provider repriced anything.

WHY A FAKE MODEL COULD NEVER PROVE THIS
    `Core/tests/unit/test_compaction.py` (`t-f5-06`) already keeps the part a fake model
    CAN prove: tokens resent per turn stay bounded and the prompt prefix is rewritten no
    more than the trigger allows, over a simulated conversation. A fake has no bill, and a
    simulated conversation has no provider cache to break - which is the exact mechanism
    this phase is worried about. Only a real provider call has a real bill.

    `t-f5-10` filled the `Summariser` seat with `ModelSummariser` and wired it into
    production (`composition.py`), so L3/L4 are no longer inert - measuring the ladder
    before that landed would have measured a ladder whose expensive rungs never ran and
    called the result "cost", which is worse than not measuring at all.

TWO TENANTS, ONE SCRIPT, TWO CONDITIONS
    The same script of user turns runs twice against the real proxy - once per virtual
    key, so the two conditions are separable by construction (docs/STATE.md: per-team
    spend separation already verified) and neither condition can pollute the other's bill:

      * tenant A (`LITELLM_KEY_TENANT_A`) drives the conversation through the REAL
        `LadderContextEngine` (t-f5-01..t-f5-04, t-f5-10) - the trigger decides when to
        compact, using the SAME `ContextState` shape `runner.py::_history_capability`
        builds in production (`window_used=None`, a fresh local estimate, the profile's
        `context_window`), and the ladder's own `ModelSummariser`-shaped calls are routed
        through the SAME proxy and key, because those calls are part of the ladder's real
        cost, not a side effect to discount.
      * tenant B (`LITELLM_KEY_TENANT_B`) drives the identical script with no engine at
        all: the history simply grows, turn after turn, uncompacted.

    "Tokens billed" comes from each response's own `usage` block - what the provider
    actually reported LiteLLM billed for, not a `CHARS_PER_TOKEN` guess. It is summed
    including every summariser call the ladder made, because a comparison that hid the
    ladder's own calls would be measuring only half of what it costs.

WHY RAW HTTP, NOT `LiteLLMGateway` / `ModelGateway`
    `tests/integration/test_proxy_mode.py` (`t-d2-01`) already found and reported the
    gap: `pydantic_ai.LiteLLMProvider` hard-codes a placeholder bearer token with no way
    to carry a tenant's virtual key, so nothing built through `models.model_for` can
    authenticate against the real proxy today. This file hits the exact same wall the
    same way that one did: a plain HTTP call carrying exactly the two things a working
    client would need - the proxy's base URL and the team's granted bare model alias
    (`MiniMax-M3`, verified against `GET /team/info` in `test_proxy_mode.py`) - plus one
    tenant's key. `LadderContextEngine` itself IS the real production ladder; only the
    wire transport beneath the injected `Summariser` is a workaround for a gap this task
    does not own and must not fix.

CONVERSATION SHAPE, AND WHY IT IS NOT REPEATED-CHARACTER FILLER
    `docs/FIELD-NOTES.md` records that `"u" * 8_000` filler makes MiniMax-M3's own
    `ThinkingPart` consume the entire `max_tokens` budget and return an empty `TextPart`,
    intermittently. `_TURNS` below is fourteen DISTINCT natural-language paragraphs - an
    engineer being onboarded onto a data pipeline, escalating in concrete detail exactly
    the way a real conversation does - never a repeated character and never padding.

    `_CONTEXT_WINDOW` is deliberately small (3_000) so this real, bounded conversation
    actually crosses `CompactionPolicy`'s default `trigger_fraction` (0.75) instead of
    needing hundreds of real turns to do it for real. The window is a construction
    parameter of `LadderContextEngine` in production too (composition.py), so shrinking
    it changes nothing about which code path runs - only how much traffic is needed to
    exercise it.

COST
    Fourteen real turns per tenant (28 total), plus one real call per ladder pass the
    engine actually makes (typically one or two, L3 and/or L4). `max_tokens` is bounded on
    every call. MiniMax-M3 is the cheapest chat model either tenant team is granted
    (docs/STATE.md's infrastructure table).

SKIP GUARD
    Skips cleanly - mirroring `test_proxy_mode.py` and `test_summariser_live.py` - when
    the proxy or either tenant credential is unavailable. CI without them must not fail.

THE CREDENTIALS
    Never printed, logged, formatted into an assertion message, or written anywhere. Read
    from the process environment first, then the repository's gitignored `.env`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from agent_core.adapters.driven.context.engine import (
    LadderContextEngine,
    Summariser,
    SummaryRequest,
    estimate_tokens,
)
from agent_core.domain.compaction import CompactionPolicy, ContextState
from agent_core.domain.turn import SessionId, SessionRef, TenantId

pytestmark = [pytest.mark.phase("F5")]

_MODEL = "MiniMax-M3"  # bare alias - what the live proxy's teams are actually granted
_CONTEXT_WINDOW = 3_000  # small on purpose: see CONVERSATION SHAPE above
_POLICY = CompactionPolicy()  # production defaults: trigger_fraction=0.75, target=0.40
_ANSWER_MAX_TOKENS = 300
_SUMMARY_MAX_TOKENS = 300
_SPEND_POLL_TIMEOUT_SECONDS = 60.0
_SPEND_POLL_INTERVAL_SECONDS = 10.0

_SYSTEM_PROMPT = (
    "You are a senior engineer onboarding a teammate onto a data pipeline project. "
    "Answer each message in two or three sentences, staying concrete."
)

# Fourteen DISTINCT natural-language turns - an onboarding conversation escalating in
# detail. See "CONVERSATION SHAPE" above for why this is not repeated-character filler.
_TURNS: tuple[str, ...] = (
    "We're standing up a new ingestion pipeline for the order-events topic. Before you "
    "touch any code, can you summarise what you understand the goal to be, so we start "
    "from the same page?",
    "Close, but one correction: the pipeline reads from Kafka, not from the REST API - "
    "the REST API is only how downstream dashboards read the aggregated result. Does "
    "that change how you'd structure the consumer?",
    "Good. Now, the tricky part: order-events carries three schema versions in the wild "
    "at once, because older producers haven't been migrated yet. What's your plan for "
    "handling a v1 record and a v3 record arriving back to back?",
    "That schema-registry approach works. One more constraint: late-arriving events can "
    "show up up to six hours after their timestamp, because of retries on the producer "
    "side. How does that interact with the hourly aggregation window you described?",
    "Right, a watermark handles that. Let's talk about failure modes now. If the "
    "aggregation job crashes halfway through an hourly window, what should happen on "
    "restart - reprocess the whole window, or resume from where it left off?",
    "Idempotent upserts keyed by order id and window, got it - that avoids double "
    "counting on restart. Now, what about a poison message: one malformed record that "
    "the deserializer can't parse at all. Should that stop the whole batch?",
    "Agreed, a dead-letter topic is the right call. Let's move to observability. What "
    "three metrics would tell you, at 3am, whether this pipeline is healthy without you "
    "having to read a single log line?",
    "Consumer lag, dead-letter rate, and window-completion latency - I like all three. "
    "Now think about cost: this topic does roughly two million events a day. Roughly "
    "how would you size the consumer group so it keeps up without over-provisioning?",
    "That sizing reasoning is sound. Here's a curveball: finance wants the same pipeline "
    "to also produce a daily reconciliation report comparing pipeline totals against the "
    "billing system's totals. Would you bolt that onto this pipeline or build it "
    "separately?",
    "A separate reconciliation job reading the pipeline's own output is exactly right - "
    "it can't corrupt the hot path. Now, security: this topic contains customer email "
    "addresses in the shipping-address field. What has to happen before this pipeline "
    "can go anywhere near a staging environment with real data?",
    "Right, field-level masking before it lands in any non-production store. Let's talk "
    "rollout. Given everything we've covered - schema versions, late events, dead "
    "letters, sizing, reconciliation, and masking - what would you tackle in week one "
    "versus defer to week two?",
    "That sequencing looks right to me. One thing you didn't mention: who gets paged "
    "when the dead-letter rate spikes at 3am, and what's the very first thing they "
    "should check before waking anyone else up?",
    "Good instinct - checking the schema registry for a new, unregistered producer "
    "version first, before assuming it's a code bug. Last question before we write this "
    "up: if you had to explain this whole pipeline to the finance stakeholder in three "
    "sentences, with no jargon, what would you say?",
    "That's a clean explanation - concrete enough that finance could actually repeat it "
    "back. Write up everything we've covered today as a one-page design note, and we'll "
    "review it together tomorrow morning before you start building.",
)


def _repo_root() -> Path:
    """`Core/tests/integration/this_file.py` -> the repository root, three parents up."""
    return Path(__file__).resolve().parents[3]


def _env_or_dotenv(name: str) -> str | None:
    """`name` from the process environment, falling back to the repo's gitignored `.env`.

    Mirrors `test_proxy_mode.py::_env_or_dotenv`. Never printed, logged, or asserted on.
    """
    from_environment = os.environ.get(name)
    if from_environment:
        return from_environment

    env_file = _repo_root() / ".env"
    if not env_file.exists():
        return None

    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def _proxy_url() -> str | None:
    url = _env_or_dotenv("LITELLM_PROXY_URL")
    return url.rstrip("/") if url else None


def _get(base_url: str, path: str, key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result: dict[str, Any] = json.loads(response.read().decode())
        return result


def _chat_completion(
    base_url: str, key: str, model: str, messages: list[dict[str, str]], *, max_tokens: int
) -> dict[str, Any]:
    payload = json.dumps(
        {"model": model, "messages": messages, "max_tokens": max_tokens}
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result: dict[str, Any] = json.loads(response.read().decode())
        return result


def _extract_answer(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return "(no answer)"
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) and content else "(empty answer)"


def _usage_total_tokens(response: dict[str, Any]) -> int:
    """The REAL, RECORDED number this harness measures - what the provider's own `usage`
    block reports, as returned through the proxy. Never a `CHARS_PER_TOKEN` estimate."""
    usage = response.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = usage.get("total_tokens")
    return int(total) if total is not None else prompt + completion


def _team_id_for(base_url: str, key: str) -> str:
    info = _get(base_url, "/key/info", key)["info"]
    team_id = info.get("team_id")
    assert team_id, "the tenant key carries no team_id; per-team spend cannot be checked"
    return str(team_id)


def _team_spend(base_url: str, key: str, team_id: str) -> float:
    info = _get(base_url, f"/team/info?team_id={team_id}", key)
    spend: float = info["team_info"]["spend"]
    return spend


def _poll_spend_moved(base_url: str, key: str, team_id: str, baseline: float) -> float:
    """Best-effort. LiteLLM posts spend asynchronously (`test_proxy_mode.py` observed
    25-65s); this harness does not fail if it has not posted by the deadline - the token
    counts recorded per call are the authoritative numbers either way."""
    deadline = time.monotonic() + _SPEND_POLL_TIMEOUT_SECONDS
    spend = baseline
    while spend <= baseline and time.monotonic() < deadline:
        time.sleep(_SPEND_POLL_INTERVAL_SECONDS)
        spend = _team_spend(base_url, key, team_id)
    return spend


def _proxy_reachable(base_url: str | None, key: str | None) -> bool:
    if base_url is None or key is None:
        return False
    try:
        _get(base_url, "/key/info", key)
    except (urllib.error.URLError, OSError):
        return False
    return True


_PROXY_URL = _proxy_url()
_TENANT_A_KEY = _env_or_dotenv("LITELLM_KEY_TENANT_A")
_TENANT_B_KEY = _env_or_dotenv("LITELLM_KEY_TENANT_B")

_needs_proxy = pytest.mark.skipif(
    _TENANT_B_KEY is None or not _proxy_reachable(_PROXY_URL, _TENANT_A_KEY),
    reason=(
        "no reachable LiteLLM proxy with both tenants: set LITELLM_PROXY_URL, "
        "LITELLM_KEY_TENANT_A and LITELLM_KEY_TENANT_B, or add them to the repo .env"
    ),
)


def _to_wire_messages(history: list[ModelMessage]) -> list[dict[str, str]]:
    """Pydantic AI's message vocabulary -> the plain OpenAI-shaped wire format the proxy's
    raw `/chat/completions` endpoint accepts. No tool calls: this harness measures the
    ladder's TEXT-only cost, which is what t-f5-06/t-f5-10 already pin the pairing
    correctness of - this file's only job is the bill."""
    wire: list[dict[str, str]] = []
    for message in history:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    wire.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    wire.append({"role": "user", "content": part.content})
        elif isinstance(message, ModelResponse):
            texts = [part.content for part in message.parts if isinstance(part, TextPart)]
            if texts:
                wire.append({"role": "assistant", "content": "\n".join(texts)})
    return wire


def _proxy_summariser(
    base_url: str, key: str, model: str, call_tokens: list[int]
) -> Summariser:
    """A `Summariser` that reaches the SAME real proxy and tenant key the ladder's cost is
    being charged to. `call_tokens` collects each call's real billed tokens so they land
    in the ladder condition's total - the ladder's own calls are part of what it costs."""

    async def summarise(request: SummaryRequest) -> str:
        prompt = (
            "Summarise the following excerpt from a longer conversation in at most six "
            "sentences, keeping concrete facts, decisions and numbers:\n\n" + request.text
        )
        if request.previous_summary:
            prompt = (
                "Fold the previous summary and the new excerpt below into ONE refreshed "
                f"summary of at most six sentences.\n\nPrevious summary:\n"
                f"{request.previous_summary}\n\nNew excerpt:\n{request.text}"
            )
        response = await asyncio.to_thread(
            _chat_completion,
            base_url,
            key,
            model,
            [{"role": "user", "content": prompt}],
            max_tokens=_SUMMARY_MAX_TOKENS,
        )
        call_tokens.append(_usage_total_tokens(response))
        return _extract_answer(response)

    return summarise


@dataclass(frozen=True, slots=True)
class ConditionResult:
    label: str
    turns: int
    total_tokens_billed: int
    per_turn_tokens: tuple[int, ...]
    rungs_applied: tuple[str, ...]
    spend_before_usd: float | None = None
    spend_after_usd: float | None = None


@dataclass(frozen=True, slots=True)
class BillMeasurement:
    recorded_at: str
    model: str
    context_window: int
    with_ladder: ConditionResult
    without_ladder: ConditionResult

    @property
    def ladder_saved_money(self) -> bool:
        """Derived, never asserted in a fixed direction - see the module docstring: this
        harness may disprove the hope that compaction always pays for itself."""
        return self.with_ladder.total_tokens_billed < self.without_ladder.total_tokens_billed


async def _run_condition(
    *,
    base_url: str,
    key: str,
    model: str,
    turns: tuple[str, ...],
    use_ladder: bool,
    context_window: int,
    policy: CompactionPolicy,
    session: SessionRef,
    label: str,
) -> ConditionResult:
    history: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(content=_SYSTEM_PROMPT)])]
    per_turn_tokens: list[int] = []
    rungs_seen: list[str] = []
    summariser_tokens: list[int] = []

    engine: LadderContextEngine | None = None
    if use_ladder:
        engine = LadderContextEngine(
            _proxy_summariser(base_url, key, model, summariser_tokens),
            context_window=context_window,
        )
        engine.on_session_start(session)

    for turn_text in turns:
        history.append(ModelRequest(parts=[UserPromptPart(content=turn_text)]))

        if engine is not None:
            # The SAME shape `runner.py::_history_capability` builds in production:
            # `window_used=None` so the port's own mandatory fallback decides, and a
            # fresh local estimate over the CURRENT history - never a cached total.
            state = ContextState(
                session=session,
                window_used=None,
                estimated_tokens=estimate_tokens(history),
                context_window=context_window,
                message_count=len(history),
            )
            if engine.should_compress(state, policy):
                result = await engine.compress(session, history, policy)
                if result.made_progress:
                    assert isinstance(result.compacted_history, list)
                    history = result.compacted_history
                    rungs_seen.extend(rung.name for rung in result.rungs_applied)

        wire = _to_wire_messages(history)
        response = await asyncio.to_thread(
            _chat_completion, base_url, key, model, wire, max_tokens=_ANSWER_MAX_TOKENS
        )
        per_turn_tokens.append(_usage_total_tokens(response))
        history.append(ModelResponse(parts=[TextPart(content=_extract_answer(response))]))

    if engine is not None:
        engine.on_session_end(session)

    total = sum(per_turn_tokens) + sum(summariser_tokens)
    return ConditionResult(
        label=label,
        turns=len(turns),
        total_tokens_billed=total,
        per_turn_tokens=tuple(per_turn_tokens),
        rungs_applied=tuple(rungs_seen),
    )


def run_bill_measurement() -> BillMeasurement:
    """The harness. Runs the identical script once per tenant key - real conversation,
    real proxy, real bill - and returns both conditions' numbers, never asserting on them
    itself. See the module docstring for the full design and why each choice was made."""
    base_url = _PROXY_URL
    key_a = _TENANT_A_KEY
    key_b = _TENANT_B_KEY
    assert base_url is not None and key_a is not None and key_b is not None, (
        "the skip guard should have prevented this"
    )

    team_a = _team_id_for(base_url, key_a)
    team_b = _team_id_for(base_url, key_b)
    spend_a_before = _team_spend(base_url, key_a, team_a)
    spend_b_before = _team_spend(base_url, key_b, team_b)

    session_a = SessionRef(session_id=SessionId("s-f5-09-with-ladder"), tenant_id=TenantId("t-a"))
    session_b = SessionRef(
        session_id=SessionId("s-f5-09-without-ladder"), tenant_id=TenantId("t-b")
    )

    with_ladder = asyncio.run(
        _run_condition(
            base_url=base_url,
            key=key_a,
            model=_MODEL,
            turns=_TURNS,
            use_ladder=True,
            context_window=_CONTEXT_WINDOW,
            policy=_POLICY,
            session=session_a,
            label="with_ladder",
        )
    )
    without_ladder = asyncio.run(
        _run_condition(
            base_url=base_url,
            key=key_b,
            model=_MODEL,
            turns=_TURNS,
            use_ladder=False,
            context_window=_CONTEXT_WINDOW,
            policy=_POLICY,
            session=session_b,
            label="without_ladder",
        )
    )

    spend_a_after = _poll_spend_moved(base_url, key_a, team_a, spend_a_before)
    spend_b_after = _poll_spend_moved(base_url, key_b, team_b, spend_b_before)
    with_ladder = replace(
        with_ladder, spend_before_usd=spend_a_before, spend_after_usd=spend_a_after
    )
    without_ladder = replace(
        without_ladder, spend_before_usd=spend_b_before, spend_after_usd=spend_b_after
    )

    return BillMeasurement(
        recorded_at=datetime.now(UTC).date().isoformat(),
        model=_MODEL,
        context_window=_CONTEXT_WINDOW,
        with_ladder=with_ladder,
        without_ladder=without_ladder,
    )


@_needs_proxy
def test_compaction_cost_is_measured_against_a_real_bill() -> None:
    """t-f5-09 closes MEASURED, not DONE. This asserts the harness ran to completion and
    that its numbers are real and internally consistent - never a specific price, and
    never a direction for `ladder_saved_money`. Copy the printed numbers into
    `docs/FIELD-NOTES.md` by hand, with today's date."""
    measurement = run_bill_measurement()

    # The harness completed both conditions over the full script.
    assert measurement.with_ladder.turns == len(_TURNS)
    assert measurement.without_ladder.turns == len(_TURNS)
    assert len(measurement.with_ladder.per_turn_tokens) == len(_TURNS)
    assert len(measurement.without_ladder.per_turn_tokens) == len(_TURNS)

    # Real, recorded numbers - every turn was actually billed for something.
    assert all(tokens > 0 for tokens in measurement.with_ladder.per_turn_tokens)
    assert all(tokens > 0 for tokens in measurement.without_ladder.per_turn_tokens)
    assert measurement.with_ladder.total_tokens_billed > 0
    assert measurement.without_ladder.total_tokens_billed > 0

    # The measurement is meaningless if the ladder never actually fired - t-f5-10's own
    # warning, restated as a guard: measuring an inert ladder measures the wrong thing.
    assert measurement.with_ladder.rungs_applied, (
        "the ladder never triggered over this script against context_window="
        f"{_CONTEXT_WINDOW}; lengthen _TURNS or shrink _CONTEXT_WINDOW before trusting "
        "any comparison here"
    )

    # ladder_saved_money is a DERIVED fact of the two totals just recorded above, not an
    # independent claim - this only checks the property agrees with its own inputs.
    assert measurement.ladder_saved_money == (
        measurement.with_ladder.total_tokens_billed
        < measurement.without_ladder.total_tokens_billed
    )

    with_per_turn = measurement.with_ladder.total_tokens_billed / measurement.with_ladder.turns
    without_per_turn = (
        measurement.without_ladder.total_tokens_billed / measurement.without_ladder.turns
    )
    print(
        "\nt-f5-09 measurement "
        f"({measurement.recorded_at}, model={measurement.model}, "
        f"context_window={measurement.context_window}):\n"
        f"  with ladder:    total={measurement.with_ladder.total_tokens_billed} tokens "
        f"over {measurement.with_ladder.turns} turns ({with_per_turn:.1f}/turn), "
        f"rungs_applied={measurement.with_ladder.rungs_applied}, "
        f"per_turn={measurement.with_ladder.per_turn_tokens}, "
        f"spend {measurement.with_ladder.spend_before_usd} -> "
        f"{measurement.with_ladder.spend_after_usd}\n"
        f"  without ladder: total={measurement.without_ladder.total_tokens_billed} tokens "
        f"over {measurement.without_ladder.turns} turns ({without_per_turn:.1f}/turn), "
        f"per_turn={measurement.without_ladder.per_turn_tokens}, "
        f"spend {measurement.without_ladder.spend_before_usd} -> "
        f"{measurement.without_ladder.spend_after_usd}\n"
        f"  ladder_saved_money={measurement.ladder_saved_money}"
    )
