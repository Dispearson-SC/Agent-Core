"""The runner's half of an intent plan (D97): a planned call through the model-call pipeline.

Subject: `PydanticAgentRunner.call_tool`, `.compose_reply`, `.template_reply` and
adapters/driven/agent_pydantic/intent_plan.py.

THE RULES UNDER TEST. A planned call is a real Pydantic AI run of the profile's own agent whose
model is a script: the tool runs with the plan's arguments only if the profile's toolset offers it,
it is declared replay-safe, its arguments validate and the caller's policy allows it; the policy
hook writes the same audit row it writes for a model's call; no provider model is ever called. A
DENY, an approval requirement, an error result, invalid arguments, a write tool and a tool outside
the grant each end the call with its fallback code and never run the body. The compose reply is one
model call offered no tools, told to use only the results; a template reply calls no model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.agent_pydantic.intent_plan import (
    COMPOSE_INSTRUCTIONS,
    ComposeIncompleteError,
    ComposeSettings,
)
from agent_core.composition import Settings, compose_settings_for
from agent_core.domain.intents import (
    PLAN_ARGUMENT_INVALID,
    PLAN_NEEDS_APPROVAL,
    PLAN_POLICY_DENIED,
    PLAN_TOOL_ERROR,
    PLAN_TOOL_NOT_GRANTED,
    PLAN_TOOL_NOT_READ_ONLY,
    PlannedCall,
    PlanToolResult,
)
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.tool_replay import safe_tool_metadata
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

TURN = TurnId("turn-1")


def _caller() -> CallerIdentity:
    return CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=_caller(),
        profile_id="shop",
        input=UserInput(text="what is low on stock?"),
    )


def _profile() -> AgentProfile:
    return AgentProfile(id="shop", persona="You run the shop.", model="m", max_iterations=4)


class Backend:
    """The tool bodies, recording every call that reached them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def toolset(self) -> FunctionToolset[Any]:
        def stock_levels(limit: int = 5) -> dict[str, Any]:
            self.calls.append(("stock_levels", {"limit": limit}))
            return {"items": [{"sku": "A", "units": 2}], "limit": limit}

        def broken_read() -> dict[str, Any]:
            self.calls.append(("broken_read", {}))
            return {"error": "the backend said no", "status": 503}

        def reorder(sku: str) -> str:
            self.calls.append(("reorder", {"sku": sku}))
            return "ordered"

        return FunctionToolset(
            [
                Tool(stock_levels, metadata=safe_tool_metadata()),
                Tool(broken_read, metadata=safe_tool_metadata()),
                Tool(reorder),  # unclassified: not replay-safe, a write
            ]
        )


def _never_called(model_id: str, base_url: str | None) -> Model:
    def refuse(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("a planned call must never reach the provider's model")

    return FunctionModel(refuse)


def _policy(effect: Effect = Effect.ALLOW) -> fakes.FakeToolPolicy:
    rule = PolicyRule(rule_id="r", tool_pattern="*", effect=effect, reason="the shop rule")
    return fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), (rule,)))


def _runner(
    backend: Backend,
    *,
    effect: Effect = Effect.ALLOW,
    model_factory: Any = _never_called,
    audit: fakes.FakeAuditSink | None = None,
) -> Any:
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(),
        policy=_policy(effect),
        audit=audit or fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(
            backend.toolset(), ("stock_levels", "broken_read", "reorder")
        ),
        model_factory=model_factory,
    )


def _call(runner: Any, tool: str, arguments: dict[str, Any] | None = None) -> PlanToolResult:
    planned = PlannedCall(step_id=tool, tool=tool, arguments=arguments or {})
    result: PlanToolResult = asyncio.run(
        runner.call_tool(TURN, _request(), _profile(), planned)
    )
    return result


def test_a_planned_call_runs_the_tool_once_audited_like_a_models_call() -> None:
    backend = Backend()
    audit = fakes.FakeAuditSink()

    result = _call(_runner(backend, audit=audit), "stock_levels", {"limit": 3})

    assert result.ok and result.step_id == "stock_levels"
    assert '"units": 2' in result.content and '"limit": 3' in result.content
    assert backend.calls == [("stock_levels", {"limit": 3})]
    assert [(c.kind, c.payload[2], c.payload[3]) for c in audit.calls] == [
        ("tool_call", "stock_levels", {"limit": 3}),
    ]
    assert audit.calls[0].payload[4].effect is Effect.ALLOW


@pytest.mark.parametrize(
    ("effect", "failure"),
    [(Effect.DENY, PLAN_POLICY_DENIED), (Effect.NEEDS_APPROVAL, PLAN_NEEDS_APPROVAL)],
)
def test_a_denied_or_approval_bound_call_never_runs_and_is_still_audited(
    effect: Effect, failure: str
) -> None:
    backend = Backend()
    audit = fakes.FakeAuditSink()

    result = _call(_runner(backend, effect=effect, audit=audit), "stock_levels")

    assert result.failure == failure
    assert backend.calls == []
    assert [c.payload[4].effect for c in audit.calls] == [effect]


@pytest.mark.parametrize(
    ("tool", "arguments", "failure"),
    [
        ("not_granted", {}, PLAN_TOOL_NOT_GRANTED),
        ("reorder", {"sku": "A"}, PLAN_TOOL_NOT_READ_ONLY),
        ("stock_levels", {"limit": "many"}, PLAN_ARGUMENT_INVALID),
        ("broken_read", {}, PLAN_TOOL_ERROR),
    ],
)
def test_a_call_outside_the_grant_a_write_bad_arguments_or_an_error_result_fails_the_plan(
    tool: str, arguments: dict[str, Any], failure: str
) -> None:
    backend = Backend()

    result = _call(_runner(backend), tool, arguments)

    assert result.failure == failure
    assert all(name != "reorder" for name, _ in backend.calls), "a write never runs"
    if failure != PLAN_TOOL_ERROR:
        assert backend.calls == []


def test_a_compose_reply_is_one_tool_less_model_call_told_to_use_only_the_results() -> None:
    seen: dict[str, Any] = {}

    def compose(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["tools"] = [t.name for t in info.function_tools]
        seen["instructions"] = info.instructions
        seen["prompt"] = [
            p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)
        ]
        seen["requests"] = seen.get("requests", 0) + 1
        return ModelResponse(parts=[TextPart("Product A has 2 units left.")])

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(compose)

    runner = _runner(Backend(), model_factory=factory)
    results = (PlanToolResult("stock_levels", "stock_levels", content='{"units": 2}'),)

    outcome = asyncio.run(runner.compose_reply(TURN, _request(), _profile(), results))

    assert outcome.result.text == "Product A has 2 units left."
    assert seen["tools"] == [] and seen["requests"] == 1
    assert COMPOSE_INSTRUCTIONS in (seen["instructions"] or "")
    prompt = seen["prompt"][0]
    assert prompt.startswith("what is low on stock?") and '{"units": 2}' in prompt
    assert all(
        not any(isinstance(p, UserPromptPart) for p in getattr(m, "parts", ()))
        for m in outcome.messages
    ), "the prompt with the results is not stored as the person's words"
    assert [type(m).__name__ for m in outcome.messages] == ["ModelResponse"]


def test_a_template_reply_is_the_text_as_the_agents_message_without_a_model() -> None:
    runner = _runner(Backend())

    outcome = runner.template_reply(TURN, "Low: A (2)")

    assert outcome.result.text == "Low: A (2)"
    (message,) = outcome.messages
    assert isinstance(message, ModelResponse) and message.parts[0].content == "Low: A (2)"


def test_a_planned_call_fits_a_profile_allowed_a_single_iteration() -> None:
    """The scripted run takes two requests (the call, then the end) but makes ONE tool call: its
    limit is one call, not the profile's iteration count, which `prepare` already charged."""
    backend = Backend()
    planned = PlannedCall(step_id="s", tool="stock_levels", arguments={})
    one = AgentProfile(id="shop", persona="p", model="m", max_iterations=1)

    result = asyncio.run(_runner(backend).call_tool(TURN, _request(), one, planned))

    assert result.ok
    assert backend.calls == [("stock_levels", {"limit": 5})]


# --- the compose call's bounds (stress phase5: 16-20k reasoning tokens, 47-112 s per reply) ---


def _settings_seen(runner_kwargs: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def compose(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.update(info.model_settings or {})
        return ModelResponse(parts=[TextPart("ok")])

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(compose)

    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(),
        policy=_policy(),
        audit=fakes.FakeAuditSink(),
        model_factory=factory,
        **runner_kwargs,
    )
    results = (PlanToolResult("stock_levels", "stock_levels", content='{"units": 2}'),)
    asyncio.run(runner.compose_reply(TURN, _request(), _profile(), results))
    return seen


def test_a_compose_reply_is_bounded_by_default_in_output_tokens_and_reasoning_effort() -> None:
    seen = _settings_seen({})

    assert seen["max_tokens"] == 4096
    assert seen["openai_reasoning_effort"] == "low"


def test_a_deployment_sets_the_compose_bounds_and_an_unset_effort_is_not_sent() -> None:
    seen = _settings_seen(
        {"compose_settings": ComposeSettings(max_tokens=900, reasoning_effort=None)}
    )

    assert seen["max_tokens"] == 900
    assert "openai_reasoning_effort" not in seen


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(parts=[TextPart("Product A has")], finish_reason="length"),
        ModelResponse(parts=[TextPart("   ")]),
    ],
    ids=["cut_at_the_token_bound", "empty"],
)
def test_a_cut_or_empty_compose_raises_so_the_plan_falls_back(response: ModelResponse) -> None:
    def compose(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return response

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(compose)

    runner = _runner(Backend(), model_factory=factory)
    results = (PlanToolResult("stock_levels", "stock_levels", content='{"units": 2}'),)

    with pytest.raises(ComposeIncompleteError):
        asyncio.run(runner.compose_reply(TURN, _request(), _profile(), results))


def test_the_compose_instructions_ask_for_a_brief_answer_that_copies_numbers() -> None:
    """Measured on the live provider (L18): the earlier wording ("do not invent, estimate or round
    any number...") cost 6-7k reasoning tokens on a 11k-token briefing; this one about 1k."""
    assert "briefly" in COMPOSE_INSTRUCTIONS
    assert "exactly as they appear" in COMPOSE_INSTRUCTIONS


# --- the deployment's compose bounds (`AGENT_CORE_COMPOSE_*`) --------------------------------


def test_the_compose_bounds_default_to_4096_tokens_and_low_effort() -> None:
    settings = Settings.from_env({}, env_file=Path("/nonexistent/.env"))

    assert compose_settings_for(settings) == ComposeSettings(4096, "low")


def test_the_compose_bounds_are_read_strictly_and_an_empty_effort_sends_none() -> None:
    settings = Settings.from_env(
        {"AGENT_CORE_COMPOSE_MAX_TOKENS": "1200", "AGENT_CORE_COMPOSE_REASONING_EFFORT": ""},
        env_file=Path("/nonexistent/.env"),
    )
    assert compose_settings_for(settings) == ComposeSettings(1200, None)
    for bad in (
        {"AGENT_CORE_COMPOSE_MAX_TOKENS": "0"},
        {"AGENT_CORE_COMPOSE_MAX_TOKENS": "lots"},
        {"AGENT_CORE_COMPOSE_REASONING_EFFORT": "extreme"},
    ):
        with pytest.raises(ValueError):
            Settings.from_env(bad, env_file=Path("/nonexistent/.env"))
