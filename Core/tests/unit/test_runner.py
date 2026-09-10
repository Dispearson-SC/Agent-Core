"""The F1 half of the Pydantic AI runner - t-f1-12.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-12
Covers:  adapters/driven/agent_pydantic/runner.py (PydanticAgentRunner)

WHAT THIS MODULE PINS AND WHY IT IS NOT tests/unit/test_runner_hooks.py
    `test_runner_hooks.py` pins `PolicyEnforcement` in isolation: policy, then audit, then
    the raise. This module pins the RUNNER - that the hook is actually WIRED into the run,
    that the tool loop completes, and that a turn comes back as a `TurnOutcome`. A hook
    that is perfect and never attached protects nothing, and no assertion in that file can
    see the difference.

NO MODEL, NO NETWORK, NO DATABASE
    `FunctionModel` stands in for the provider and the fakes from `tests/fakes/ports.py`
    stand in for the ports, exactly as CLAUDE.md asks: a unit test that needs Postgres
    means a port is leaking. Real-provider assertions live in
    `tests/integration/test_f0_end_to_end.py`.

THE TURN-ID SEAT
    `AgentRunner.run` takes `(turn_id, request, profile, history)` (t-f1-05): the id the
    caller generated arrives per call. The runner must NEVER invent one (CLAUDE.md
    non-negotiable #2), and it no longer can - there is no state on the runner an id could
    hide in, and no second way to call it. What is still worth asserting is that the id the
    caller passed is the id the outcome and every audit row carry, because a runner that
    quietly substituted one would be green everywhere else in the suite while filing this
    turn's denials under an id that names no turn.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

# Imported as a MODULE for the same reason tests/unit/test_runner_hooks.py does it: while
# any target in the file is still a stub, a name-level import turns "not implemented yet"
# into a collection-time ImportError instead of a red assertion inside a test that ran.
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.llm_litellm import models
from agent_core.domain.policy import Effect, PolicyDecision, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from agent_core.ports.agent_runner import AgentRunner
from tests.fakes import ports as fakes

pytestmark = [pytest.mark.phase("F1"), pytest.mark.silent]

TURN_ID = TurnId("turn-f1-1")
TOOL_NAME = "lookup_delivery_code"
DELIVERY_CODE = "DLV-UNIT-1"

# A reason with no overlap with anything else in this module, so finding it inside the
# message the model received proves it travelled the whole way rather than being
# reconstructed by the assertion.
DENY_REASON = "Looking up a delivery code needs a delivery-desk operator on this channel."


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _request(text: str = "What is the delivery code for order ord-42?") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id="s-1", tenant_id=TenantId("t-1")),  # type: ignore[arg-type]
        caller=_caller(),
        profile_id="delivery",
        input=UserInput(text=text),
    )


def _profile(**overrides: Any) -> AgentProfile:
    data: dict[str, Any] = {
        "id": "delivery",
        "persona": "You are a delivery desk assistant.",
        "model": "minimax/MiniMax-M3",
    }
    data.update(overrides)
    return AgentProfile.from_mapping(data)


def _allowing_policy() -> fakes.FakeToolPolicy:
    rule = PolicyRule(
        rule_id="r-delivery-lookup",
        tool_pattern=TOOL_NAME,
        effect=Effect.ALLOW,
        reason="the delivery desk may read a delivery code",
    )
    return fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), (rule,)))


def _denying_policy() -> fakes.FakeToolPolicy:
    rule = PolicyRule(
        rule_id="r-delivery-operator-only",
        tool_pattern=TOOL_NAME,
        effect=Effect.DENY,
        reason=DENY_REASON,
    )
    return fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), (rule,)))


class ScriptedModel:
    """A `FunctionModel` body plus the counters the assertions need.

    Records how many provider requests were made and what tool results the model saw, so a
    test can assert on what the MODEL received rather than on what the test believes was
    sent.
    """

    # `FunctionModel` names itself after the function it was given, so a callable object
    # standing in for one has to carry a name too.
    __name__ = "scripted_model"

    def __init__(self, *, call_tool: bool, final_text: str = "done") -> None:
        self.call_tool = call_tool
        self.final_text = final_text
        self.requests = 0
        self.tool_results_seen: list[str] = []
        self.tool_call_ids_sent: list[str] = []
        self.tool_call_ids_returned: list[str] = []
        self.history_seen: list[ModelMessage] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        if self.requests == 1:
            self.history_seen = list(messages)

        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if self.call_tool and not returns:
            part = ToolCallPart(TOOL_NAME, {"order_id": "ord-42"})
            self.tool_call_ids_sent.append(part.tool_call_id)
            return ModelResponse(
                parts=[part], usage=RequestUsage(input_tokens=11, output_tokens=7)
            )

        self.tool_results_seen.extend(str(part.content) for part in returns)
        self.tool_call_ids_returned.extend(part.tool_call_id for part in returns)
        return ModelResponse(
            parts=[TextPart(self.final_text)],
            usage=RequestUsage(input_tokens=13, output_tokens=5, cache_read_tokens=3),
        )


def _model_factory(scripted: ScriptedModel) -> Any:
    """A `ModelFactory` that ignores the wire id and returns the local stand-in.

    The id is still asserted separately, because dropping it here must not hide a runner
    that forgot to ask `ModelGateway.model_id_for`.
    """
    seen: list[tuple[str, str | None]] = []

    def factory(model_id: str, base_url: str | None) -> Model:
        seen.append((model_id, base_url))
        return FunctionModel(scripted)

    factory.seen = seen  # type: ignore[attr-defined]
    return factory


class DeliveryDesk:
    """The one tool. Records its calls so "the body never ran" is a fact, not a hope."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup_delivery_code(self, order_id: str) -> str:
        """Return the internal delivery code for one order id."""
        self.calls.append(order_id)
        return DELIVERY_CODE


def _runner(
    *,
    scripted: ScriptedModel,
    policy: fakes.FakeToolPolicy,
    audit: fakes.FakeAuditSink,
    desk: DeliveryDesk | None = None,
    factory: Any = None,
) -> Any:
    tools = None
    if desk is not None:
        tools = fakes.FakeToolProvider(
            FunctionToolset([desk.lookup_delivery_code]), (TOOL_NAME,)
        )
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=policy,
        audit=audit,
        tools=tools,
        model_factory=factory if factory is not None else _model_factory(scripted),
    )


# --------------------------------------------------------------------------------------
# The turn-id seat


def test_the_runner_carries_no_turn_state_at_all() -> None:
    """CLAUDE.md non-negotiable #2, now enforced by shape instead of by a guard.

    The runner used to hold an optional bound id and raise when it was absent. With the id
    on `run` there is nowhere for one to live, so the property to pin is that no such
    attribute came back: an id cached on the instance is exactly how a second turn ends up
    filed under the first turn's identity.
    """
    scripted = ScriptedModel(call_tool=False)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    assert not hasattr(runner, "for_turn"), (
        "the pre-binding is back; the id belongs to the call, not to the runner"
    )
    assert not any("turn" in name for name in vars(runner)), (
        f"the runner is holding turn state: {sorted(vars(runner))}"
    )


def test_two_turns_on_one_runner_each_carry_their_own_id() -> None:
    """The same runner, twice, and neither turn inherits the other's identity.

    This is the assertion the removed `TurnNotBoundError` guard was standing in for. A
    runner that cached the first id - or invented one - passes every other test in this
    module and files the second turn's audit rows under the first turn's name.
    """
    scripted = ScriptedModel(call_tool=False)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    first = asyncio.run(runner.run(TurnId("turn-a"), _request(), _profile(), None))
    second = asyncio.run(runner.run(TurnId("turn-b"), _request(), _profile(), None))

    assert first.turn_id == TurnId("turn-a")
    assert second.turn_id == TurnId("turn-b")


def test_the_denial_is_filed_under_the_turn_id_of_that_call() -> None:
    """The audit row follows the per-call id, not a per-runner one.

    `PolicyEnforcement` is built inside `run` from the id that call was given. One runner
    serving two turns must produce two rows under two ids; a shared or cached id would
    produce two rows under one, and no other assertion here would notice.
    """
    scripted = ScriptedModel(call_tool=True, final_text="I cannot do that.")
    audit = fakes.FakeAuditSink()
    runner = _runner(
        scripted=scripted, policy=_denying_policy(), audit=audit, desk=DeliveryDesk()
    )

    asyncio.run(runner.run(TurnId("turn-a"), _request(), _profile(), None))
    scripted.tool_results_seen.clear()
    asyncio.run(runner.run(TurnId("turn-b"), _request(), _profile(), None))

    filed = [call.payload[0] for call in audit.calls if call.kind == "tool_call"]
    assert filed == [TurnId("turn-a"), TurnId("turn-b")]


def test_the_outcome_carries_the_turn_id_the_caller_passed() -> None:
    scripted = ScriptedModel(call_tool=False, final_text="hello")
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert outcome.turn_id == TURN_ID
    assert not outcome.is_suspended
    assert outcome.result is not None
    assert outcome.result.text == "hello"


def test_two_turns_on_one_runner_share_the_agent_cache() -> None:
    """One agent per profile, not per turn.

    A fresh cache per turn would rebuild the agent - and re-resolve its toolset - on every
    single request, which is exactly what the caching note in the runner docstring exists
    to prevent. When the turn id was a BINDING this could regress by handing each turn its
    own runner; now that it is a parameter, the cache simply has to survive two calls.
    """
    scripted = ScriptedModel(call_tool=False)
    factory = _model_factory(scripted)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink(), factory=factory
    )
    profile = _profile()

    asyncio.run(runner.run(TurnId("turn-a"), _request(), profile, None))
    asyncio.run(runner.run(TurnId("turn-b"), _request(), profile, None))

    assert len(factory.seen) == 1, "the agent was rebuilt for the second turn"


# --------------------------------------------------------------------------------------
# The tool loop


def test_the_tool_loop_runs_to_completion_and_the_call_and_return_stay_paired() -> None:
    """One `run`, two provider requests, and the pair survives.

    CLAUDE.md non-negotiable #5 and the silent-bug table: a tool call separated from its
    return is accepted here and rejected by the provider much later as a 400. The id the
    model issued must be the id that comes back.
    """
    scripted = ScriptedModel(call_tool=True, final_text=f"The code is {DELIVERY_CODE}.")
    desk = DeliveryDesk()
    runner = _runner(
        scripted=scripted,
        policy=_allowing_policy(),
        audit=fakes.FakeAuditSink(),
        desk=desk,
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert scripted.requests == 2, "the runner did not complete the two-step tool loop"
    assert desk.calls == ["ord-42"], "the tool body did not run"
    assert scripted.tool_results_seen == [DELIVERY_CODE]
    assert scripted.tool_call_ids_returned == scripted.tool_call_ids_sent, (
        "the tool return carried a different id than the call the model issued"
    )
    assert outcome.result is not None
    assert DELIVERY_CODE in outcome.result.text


def test_the_advertised_tool_names_come_from_the_tool_provider() -> None:
    scripted = ScriptedModel(call_tool=True)
    desk = DeliveryDesk()
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink(), desk=desk
    )

    asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert desk.calls, "the profile's toolset never reached the agent"


# --------------------------------------------------------------------------------------
# The enforcement point, wired


def test_a_denied_tool_is_audited_before_it_is_skipped_and_never_executes() -> None:
    """The wiring proof: `PolicyEnforcement` is attached to the run the runner performs.

    `test_runner_hooks.py` proves the hook is correct in isolation. This proves it is
    ATTACHED - the one thing that file cannot see.
    """
    scripted = ScriptedModel(call_tool=True, final_text="I cannot do that.")
    desk = DeliveryDesk()
    audit = fakes.FakeAuditSink()
    runner = _runner(
        scripted=scripted, policy=_denying_policy(), audit=audit, desk=desk
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert desk.calls == [], "the tool body ran despite a DENY verdict"
    assert [call.kind for call in audit.calls] == ["tool_call"]
    recorded_turn_id, recorded_caller, tool_name, arguments, decision = audit.calls[0].payload
    assert recorded_turn_id == TURN_ID, "the denial was filed under the wrong turn"
    assert recorded_caller == _caller(), "the audit row does not name who asked"
    assert tool_name == TOOL_NAME
    assert arguments == {"order_id": "ord-42"}
    assert isinstance(decision, PolicyDecision)
    assert decision.effect is Effect.DENY
    # rule_id is what answers "why was this refused?" six months later.
    assert decision.rule_id == "r-delivery-operator-only"

    assert scripted.tool_results_seen, "the model never received a tool result at all"
    assert DENY_REASON in scripted.tool_results_seen[0]

    # A policy denial is an ORDINARY outcome, never an exception - ports/agent_runner.py.
    assert outcome.result is not None


def test_the_rule_snapshot_is_loaded_once_per_turn() -> None:
    """D13: `load_rules` is the only policy call that touches I/O, hoisted out of the
    per-call path. Loading it per tool call would put a database round trip inside every
    single tool invocation."""
    scripted = ScriptedModel(call_tool=True)
    policy = _allowing_policy()
    runner = _runner(
        scripted=scripted, policy=policy, audit=fakes.FakeAuditSink(), desk=DeliveryDesk()
    )

    asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert policy.load_rules_calls == [_caller()]


# --------------------------------------------------------------------------------------
# Translation


def test_usage_is_translated_into_the_domain_usage() -> None:
    scripted = ScriptedModel(call_tool=True)
    runner = _runner(
        scripted=scripted,
        policy=_allowing_policy(),
        audit=fakes.FakeAuditSink(),
        desk=DeliveryDesk(),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert outcome.result is not None
    usage = outcome.result.usage
    assert usage.input_tokens == 24, "input tokens were not summed across both requests"
    assert usage.output_tokens == 12
    assert usage.cached_tokens == 3
    # None is COMMON, not exceptional: the provider does not report it and ContextEngine
    # must fall back to a local estimate. domain/turn.py, Usage.
    assert usage.context_window_used is None


def test_a_history_of_model_messages_is_prepended_to_the_run() -> None:
    scripted = ScriptedModel(call_tool=False)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("earlier question")]),
        ModelResponse(parts=[TextPart("earlier answer")]),
    ]

    asyncio.run(runner.run(TURN_ID, _request(), _profile(), history))

    texts = [
        part.content
        for message in scripted.history_seen
        for part in message.parts
        if isinstance(part, (UserPromptPart, TextPart))
    ]
    assert "earlier question" in texts
    assert "earlier answer" in texts


def test_an_unrecognised_history_shape_is_refused_with_its_anchor() -> None:
    """The store's on-disk message encoding is not settled while `append_outcome` is
    pending (t-f1-13), so the runner accepts Pydantic AI's own message type and refuses
    anything else rather than guessing at a format that is still being designed."""
    scripted = ScriptedModel(call_tool=False)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    with pytest.raises(runner_module.UnsupportedHistoryError) as raised:
        asyncio.run(
            runner.run(TURN_ID, _request(), _profile(), [{"role": "user", "content": "hi"}])
        )

    assert "t-f1-13" in str(raised.value)
    assert scripted.requests == 0


# --------------------------------------------------------------------------------------
# Budget


def test_an_exhausted_iteration_budget_finishes_the_turn_instead_of_raising() -> None:
    """`StartTurn` says an exhausted budget is an ordinary outcome, never an exception.

    A runner that let `UsageLimitExceeded` escape would turn a budget ceiling into a 500
    and lose the turn, when the correct answer is a finished turn that says it stopped.
    """
    scripted = ScriptedModel(call_tool=True)
    runner = _runner(
        scripted=scripted,
        policy=_allowing_policy(),
        audit=fakes.FakeAuditSink(),
        desk=DeliveryDesk(),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(max_iterations=1), None))

    assert outcome.result is not None
    assert not outcome.is_suspended
    assert "budget" in outcome.result.text.lower()


# --------------------------------------------------------------------------------------
# The seams left for later phases


def test_resume_is_an_explicit_f3_stub_that_names_its_phase() -> None:
    """`resume` genuinely needs F3: there is no `HumanGateway`, no `DeferredToolRequests`
    output type and nothing that can suspend, so a resumable id could not round-trip. It
    says so rather than pretending."""
    scripted = ScriptedModel(call_tool=False)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    with pytest.raises(NotImplementedError) as raised:
        asyncio.run(runner.resume(TURN_ID, _profile(), None, ()))

    assert "F3" in str(raised.value)
    assert "t-f3-02" in str(raised.value)


def test_the_default_model_factory_serves_day_one_instead_of_refusing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default factory is production's ONLY path to a model, so it has to have one.

    It used to refuse whenever `ModelGateway.base_url()` was None - correct, because a
    `LiteLLMProvider` built with no endpoint silently targets api.openai.com, but it left
    day 1 with no way to call a model at all outside a test that injected its own factory.
    Day-1 library mode now has a real endpoint (adapters/driven/llm_litellm/models.py) and
    this is the wiring that reaches it."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-fake-minimax-for-tests")

    model = runner_module.litellm_model_factory("minimax/MiniMax-M3", None)

    # `Model.base_url` rather than the client's: the factory is declared as returning the
    # PORT-shaped `Model`, and reaching past that for a concrete client here would be this
    # test asserting on something the seam does not promise.
    assert "minimax" in str(model.base_url)
    assert "openai.com" not in str(model.base_url)


def test_the_default_model_factory_still_refuses_what_it_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loud refusal did not become a fallback.

    A profile naming a provider nothing can resolve must fail before a client exists, not
    reach whatever host and credential happen to be lying around."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(models.ModelEndpointUnavailableError) as raised:
        runner_module.litellm_model_factory("not-a-provider/not-a-model", None)

    assert "t-f0-04" in str(raised.value)


def test_the_runner_satisfies_the_agent_runner_port() -> None:
    """Type-checked by the project mypy run, not by this assertion.

    The annotation is the test: if `PydanticAgentRunner` drifts from the settled
    `(turn_id, request, profile, history)` / `resume` shape, `mypy` fails on this line.
    """
    scripted = ScriptedModel(call_tool=False)
    concrete = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )
    port: AgentRunner = concrete

    assert port is not None


def test_max_cost_is_handed_to_the_run_as_a_cost_limit() -> None:
    """The profile's ceiling reaches the run rather than being decoration on a YAML file."""
    scripted = ScriptedModel(call_tool=False)
    runner = _runner(
        scripted=scripted, policy=_allowing_policy(), audit=fakes.FakeAuditSink()
    )

    limits = runner.usage_limits_for(_profile(max_cost_usd="2.50", max_iterations=7))

    assert limits.request_limit == 7
    assert limits.cost_limit == Decimal("2.50")
