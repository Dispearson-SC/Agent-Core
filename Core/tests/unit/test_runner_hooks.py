"""The security enforcement point - t-f1-12.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-12
Covers:  adapters/driven/agent_pydantic/runner.py

SILENT-BUG AREA (CLAUDE.md): a policy hole never fails a test on its own, and neither
does an audit row written on the wrong side of a raise. Both are asserted here directly,
because nothing else in the suite will notice when they stop being true.

WHAT F1 IS DONE-WHEN, RESTATED AS THREE ASSERTIONS (docs/TASKS.md, F1 header)
    1. ORDER. `AuditSink.record_tool_call` is written BEFORE `SkipToolExecution` leaves
       `before_tool_execute`. If the write happens after the raise, any exception on the
       way out erases the evidence and the audit table then says the denial never
       happened - the one row a compliance reader came for. CLAUDE.md non-negotiable #6.
    2. The tool body NEVER runs. A denial that audits perfectly and still freezes the
       account has enforced nothing.
    3. The refusal REACHES THE MODEL as the tool result, so the agent can adapt instead of
       retrying blindly. `PolicyDecision.reason` is written for that reader.

WHY THE ORDER IS PROVED WITH THE t-f0-01 FAKE AND NOT WITH A MOCK
    `FakeAuditSink` records calls in a LIST, in order. The test appends its own marker to
    that same list at the moment it catches the raise, so both events land on one timeline
    and the assertion is a sequence comparison rather than two independent "was it
    called?" checks - which is exactly the pair of checks that cannot see an ordering bug.

NO MODEL, NO NETWORK, NO DATABASE. `FunctionModel` is a local function standing in for the
provider, so assertions 2 and 3 are made against a real Pydantic AI run - the hook wired
the way production wires it - and still belong in tests/unit/.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import SkipToolExecution
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

# `runner` and `ports` are imported as MODULES, not `from ... import Name`: while a target
# is still a stub these names do not exist, and a name-level import would turn "not
# implemented yet" into a collection-time ImportError instead of a red assertion inside a
# test body. Same reason tests/unit/test_fakes.py imports the fakes module, not its
# classes.
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.policy import Effect, PolicyDecision, PolicyRule, RuleSet
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId
from tests.fakes import ports as fakes

pytestmark = [pytest.mark.phase("F1"), pytest.mark.silent]

TOOL_NAME = "freeze_account"

# A reason with no overlap with anything else in this module, so finding it inside the
# message the model received proves it travelled the whole way rather than being
# reconstructed by the assertion.
DENY_REASON = "Freezing an account requires a fraud-desk operator on this channel."

DENY_RULE = PolicyRule(
    rule_id="r-freeze-operator-only",
    tool_pattern=TOOL_NAME,
    effect=Effect.DENY,
    reason=DENY_REASON,
)


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"agent"}),
    )


def _denying_setup() -> tuple[fakes.FakeToolPolicy, RuleSet, fakes.FakeAuditSink]:
    caller = _caller()
    rules = RuleSet.for_caller(caller, (DENY_RULE,))
    return fakes.FakeToolPolicy(rules), rules, fakes.FakeAuditSink()


def _hooks(
    policy: fakes.FakeToolPolicy, rules: RuleSet, audit: fakes.FakeAuditSink
) -> Any:
    return runner_module.PolicyEnforcement(
        policy=policy,
        rules=rules,
        audit=audit,
        turn_id=TurnId("turn-1"),
        caller=_caller(),
    )


def test_deny_audits_before_it_raises() -> None:
    """ASSERTION 1. One timeline, two events, and the audit row comes first."""
    policy, rules, audit = _denying_setup()
    hooks = _hooks(policy, rules, audit)

    async def _drive() -> SkipToolExecution:
        with pytest.raises(SkipToolExecution) as raised:
            await hooks.before_tool_execute(
                # The hook takes its verdict from `call` and its identity from the
                # constructor; it must not need the run context to decide, so None is
                # passed deliberately. A hook that starts reading `ctx` to reach a
                # policy input has moved the caller back out of the frozen snapshot.
                cast(Any, None),
                call=ToolCallPart(TOOL_NAME, {"account_id": "a-1"}),
                tool_def=ToolDefinition(name=TOOL_NAME),
                args={"account_id": "a-1"},
            )
        # Appended to the SAME list the fake records into, so the raise takes its place
        # on the audit timeline instead of being a separate, unordered observation.
        audit.calls.append(fakes.AuditCall("skip_raised", (raised.value.result,)))
        return raised.value

    asyncio.run(_drive())

    assert [call.kind for call in audit.calls] == ["tool_call", "skip_raised"]

    _turn_id, _caller_recorded, tool_name, arguments, decision = audit.calls[0].payload
    assert tool_name == TOOL_NAME
    assert arguments == {"account_id": "a-1"}
    # rule_id is what answers "why was this refused?" six months later. A decision
    # recorded without it is unauditable - see the AuditSink port docstring.
    assert isinstance(decision, PolicyDecision)
    assert decision.effect is Effect.DENY
    assert decision.rule_id == DENY_RULE.rule_id


def test_deny_never_reaches_the_tool_body_and_refusal_reaches_the_model() -> None:
    """ASSERTIONS 2 and 3, against a real Pydantic AI run with the hook attached."""
    policy, rules, audit = _denying_setup()
    hooks = _hooks(policy, rules, audit)

    executed: list[str] = []
    tool_results_seen_by_model: list[str] = []

    def freeze_account(account_id: str) -> str:
        """Freeze an account. MUST NOT RUN in this test."""
        executed.append(account_id)
        return "frozen"

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(TOOL_NAME, {"account_id": "a-1"})])
        # Second request: whatever the tool result was, it is now in the history the
        # model reads. Echo it so the assertion inspects what the MODEL saw.
        tool_results_seen_by_model.extend(str(part.content) for part in returns)
        return ModelResponse(parts=[TextPart("acknowledged")])

    agent = Agent(
        FunctionModel(model_fn),
        tools=[freeze_account],
        capabilities=[hooks],
    )

    asyncio.run(agent.run("freeze account a-1"))

    assert executed == [], "the tool body ran despite a DENY verdict"
    assert tool_results_seen_by_model, "the model never received a tool result at all"
    assert DENY_REASON in tool_results_seen_by_model[0]
    assert [call.kind for call in audit.calls] == ["tool_call"]
