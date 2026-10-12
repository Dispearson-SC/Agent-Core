"""C15 (D41): per-peer permission rules on `ask_peer`.

Subject: domain/policy.py (`PolicyRule.resource_pattern`, `resource_of`, `RuleSet.applicable`),
adapters/driven/persistence_pg/policy_repository.py (`PgToolPolicy.decide/filter_toolset`),
adapters/driven/policy_fs/loader.py (`resource:` and `effect: ask`), the runner's
`PolicyEnforcement` hook and the C14 peer catalogue.

A rule names a tool AND, optionally, a resource: for `ask_peer`, the peer id the call targets.
Effects are the existing three (`allow`, `needs_approval` - spelt `ask` in a rule file - and
`deny`) and the ordering is the existing one: DENY beats NEEDS_APPROVAL beats ALLOW, whatever the
order of the rules. A rule with no resource keeps meaning "the tool", so nothing stored before C15
changes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.policy_fs.loader import PolicyRuleFileError, parse_policy_rules
from agent_core.adapters.driven.tools.peers import build_toolset
from agent_core.domain.policy import Effect, PolicyRule, RuleSet, resource_of
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

CALLER = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))


def _rule(
    rule_id: str, effect: Effect, *, resource: str | None = None, tool: str = "ask_peer"
) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        tool_pattern=tool,
        effect=effect,
        reason=f"because {rule_id}",
        resource_pattern=resource,
    )


def _ruleset(*rules: PolicyRule) -> RuleSet:
    return RuleSet.for_caller(CALLER, rules)


def _policy() -> PgToolPolicy:
    def no_database() -> Any:
        raise AssertionError("decide and filter_toolset are pure over the snapshot")

    return PgToolPolicy(no_database)


def _decide(rules: RuleSet, target: str | None) -> Effect:
    arguments: dict[str, object] = {} if target is None else {"target": target, "question": "q"}
    return _policy().decide(rules, "ask_peer", arguments).effect


# --- the rule ---------------------------------------------------------------------------------


def test_a_resource_rule_matches_only_the_peer_it_names() -> None:
    rule = _rule("r", Effect.DENY, resource="fraud")

    assert rule.matches("ask_peer", frozenset(), "http", TenantId("t"), resource="fraud")
    assert not rule.matches("ask_peer", frozenset(), "http", TenantId("t"), resource="billing")
    assert not rule.matches("ask_peer", frozenset(), "http", TenantId("t"), resource=None), (
        "a call that names no resource is not about this peer"
    )


def test_resource_patterns_are_case_insensitive_and_a_trailing_star_is_a_prefix() -> None:
    star = _rule("r", Effect.DENY, resource="fin-*")

    assert star.matches("ask_peer", frozenset(), "http", TenantId("t"), resource="FIN-ops")
    assert not star.matches("ask_peer", frozenset(), "http", TenantId("t"), resource="x-fin-ops")
    assert _rule("e", Effect.DENY, resource="Fraud").matches(
        "ask_peer", frozenset(), "http", TenantId("t"), resource="fraud"
    )


def test_a_rule_without_a_resource_keeps_meaning_the_whole_tool() -> None:
    rule = _rule("r", Effect.ALLOW)

    for resource in ("fraud", "billing", None):
        assert rule.matches("ask_peer", frozenset(), "http", TenantId("t"), resource=resource)


def test_only_ask_peer_names_its_target_as_the_resource() -> None:
    assert resource_of("ask_peer", {"target": "fraud", "question": "q"}) == "fraud"
    assert resource_of("ASK_PEER", {"target": "fraud"}) == "fraud"
    assert resource_of("ask_peer", {"question": "q"}) is None
    assert resource_of("ask_peer", {"target": ""}) is None
    assert resource_of("ask_peer", {"target": 7}) is None
    assert resource_of("issue_refund", {"target": "fraud"}) is None


# --- the verdict ------------------------------------------------------------------------------


def test_each_peer_gets_its_own_verdict_and_the_unnamed_ones_follow_the_tool_rule() -> None:
    rules = _ruleset(
        _rule("all", Effect.ALLOW),
        _rule("ask-fraud", Effect.NEEDS_APPROVAL, resource="fraud"),
        _rule("no-payroll", Effect.DENY, resource="payroll"),
    )

    assert _decide(rules, "billing") is Effect.ALLOW
    assert _decide(rules, "fraud") is Effect.NEEDS_APPROVAL
    assert _decide(rules, "payroll") is Effect.DENY
    decision = _policy().decide(rules, "ask_peer", {"target": "fraud"})
    assert decision.rule_id == "ask-fraud" and decision.reason == "because ask-fraud"


def test_deny_beats_ask_beats_allow_whatever_the_order_of_the_rules() -> None:
    allow = _rule("allow", Effect.ALLOW, resource="fraud")
    ask = _rule("ask", Effect.NEEDS_APPROVAL, resource="fraud")
    deny = _rule("deny", Effect.DENY, resource="fraud")

    for ordered in ((allow, ask, deny), (deny, ask, allow), (ask, deny, allow)):
        assert _decide(_ruleset(*ordered), "fraud") is Effect.DENY
    for ordered in ((allow, ask), (ask, allow)):
        assert _decide(_ruleset(*ordered), "fraud") is Effect.NEEDS_APPROVAL


def test_a_rule_that_names_one_peer_does_not_grant_the_others() -> None:
    rules = _ruleset(_rule("only-billing", Effect.ALLOW, resource="billing"))

    assert _decide(rules, "billing") is Effect.ALLOW
    assert _decide(rules, "fraud") is Effect.DENY, "no rule for fraud: denied by default"
    assert _decide(rules, None) is Effect.DENY, "a call with no target matches no peer rule"


def test_a_peer_rule_does_not_leak_onto_another_tool() -> None:
    rules = _ruleset(_rule("deny-fraud", Effect.DENY, resource="fraud"), _rule("any", Effect.ALLOW))
    decision = _policy().decide(rules, "knowledge_search", {"target": "fraud"})

    assert decision.effect is Effect.DENY, "default deny: the rules are about ask_peer"
    assert decision.rule_id is None


def test_the_fake_policy_reduces_peer_rules_the_same_way() -> None:
    rules = _ruleset(
        _rule("all", Effect.ALLOW), _rule("ask-fraud", Effect.NEEDS_APPROVAL, resource="fraud")
    )
    fake = fakes.FakeToolPolicy(rules)

    assert fake.decide(rules, "ask_peer", {"target": "fraud"}).effect is Effect.NEEDS_APPROVAL
    assert fake.decide(rules, "ask_peer", {"target": "billing"}).effect is Effect.ALLOW


# --- filtering the tool itself ------------------------------------------------------------------


def test_the_tool_stays_offered_while_some_peer_may_still_be_asked() -> None:
    only_billing = _ruleset(_rule("only-billing", Effect.ALLOW, resource="billing"))
    only_denies = _ruleset(_rule("no-fraud", Effect.DENY, resource="fraud"))
    unscoped_deny = _ruleset(
        _rule("no-peers", Effect.DENY), _rule("billing-ok", Effect.ALLOW, resource="billing")
    )
    policy = _policy()

    assert policy.filter_toolset(only_billing, ("ask_peer",)) == ("ask_peer",)
    assert policy.filter_toolset(only_denies, ("ask_peer",)) == (), "nothing grants it"
    assert policy.filter_toolset(unscoped_deny, ("ask_peer",)) == (), "a tool-wide DENY wins"


# --- the catalogue and the call -----------------------------------------------------------------


def _profile(profile_id: str, *, asks: tuple[str, ...] = ()) -> AgentProfile:
    return AgentProfile.from_mapping(
        {
            "id": profile_id,
            "persona": profile_id,
            "model": "minimax/MiniMax-M3",
            "peers": {
                "enabled": True,
                "peers": [{"agent_id": a, "display_name": a.title()} for a in asks],
            },
        }
    )


def _runner(rules: RuleSet, scripted: Any, profiles: dict[str, AgentProfile]) -> Any:
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(rules),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(scripted),
        peer_profiles=profiles,
    )


def _world() -> dict[str, AgentProfile]:
    return {
        "asker": _profile("asker", asks=("billing", "fraud")),
        "billing": _profile("billing", asks=("asker",)),
        "fraud": _profile("fraud", asks=("asker",)),
    }


def _turn(runner: Any, profile: AgentProfile) -> Any:
    session = SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t"))
    request = TurnRequest(
        session=session, caller=CALLER, profile_id=profile.id, input=UserInput("hi")
    )
    return asyncio.run(runner.run(TurnId("t1"), request, profile, None))


def _asks_both(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    first = len(messages) == 1
    usage = RequestUsage(input_tokens=1, output_tokens=1)
    if first:
        return ModelResponse(
            parts=[
                ToolCallPart("ask_peer", {"target": "billing", "question": "b?"}, "c-b"),
                ToolCallPart("ask_peer", {"target": "fraud", "question": "f?"}, "c-f"),
            ],
            usage=usage,
        )
    return ModelResponse(parts=[TextPart("done")], usage=usage)


def test_a_denied_peer_is_absent_from_the_tool_schema_and_an_asked_one_stays() -> None:
    rules = _ruleset(
        _rule("all", Effect.ALLOW),
        _rule("no-fraud", Effect.DENY, resource="fraud"),
    )
    seen: list[list[str]] = []

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        (tool,) = [t for t in info.function_tools if t.name == "ask_peer"]
        seen.append(tool.parameters_json_schema["properties"]["target"]["enum"])
        return ModelResponse(
            parts=[TextPart("ok")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    world = _world()
    _turn(_runner(rules, scripted, world), world["asker"])

    assert seen == [["billing"]]

    ask_rules = _ruleset(
        _rule("all", Effect.ALLOW), _rule("ask-fraud", Effect.NEEDS_APPROVAL, resource="fraud")
    )
    seen.clear()
    _turn(_runner(ask_rules, scripted, world), world["asker"])
    assert seen == [["billing", "fraud"]], "a person can still approve it, so it stays listed"


def test_an_ask_rule_suspends_that_call_for_a_person_before_anything_is_dispatched() -> None:
    rules = _ruleset(
        _rule("all", Effect.ALLOW),
        _rule("ask-fraud", Effect.NEEDS_APPROVAL, resource="fraud"),
    )
    world = _world()

    outcome = _turn(_runner(rules, _asks_both, world), world["asker"])

    by_call = {str(p.tool_call_id): p for p in outcome.pending}
    assert by_call["c-f"].kind is PendingKind.APPROVAL, "fraud waits for a person first"
    assert by_call["c-f"].reason == "because ask-fraud"
    assert by_call["c-b"].kind is PendingKind.DELEGATION, "billing is asked as usual"


def test_a_deny_rule_refuses_that_peer_at_call_time_with_the_rules_reason() -> None:
    rules = _ruleset(_rule("all", Effect.ALLOW), _rule("no-fraud", Effect.DENY, resource="fraud"))
    world = _world()
    returned: list[str] = []

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        usage = RequestUsage(input_tokens=1, output_tokens=1)
        if len(messages) == 1:  # a model that names the hidden peer anyway
            return ModelResponse(
                parts=[ToolCallPart("ask_peer", {"target": "fraud", "question": "f?"}, "c-f")],
                usage=usage,
            )
        returned.append(str(messages[-1]))
        return ModelResponse(parts=[TextPart("done")], usage=usage)

    runner = _runner(rules, scripted, world)
    profile = world["asker"]
    # the schema enum would make the provider reject "fraud"; bypass it as a hostile model could
    outcome = _turn_without_enum(runner, profile, scripted)

    assert outcome.pending == (), "nothing was dispatched"
    assert returned and "because no-fraud" in returned[0]


def _turn_without_enum(runner: Any, profile: AgentProfile, scripted: Any) -> Any:
    runner._peer_profiles = None  # noqa: SLF001 - the static tool shape, so the call reaches the hook
    return _turn(runner, profile)


# --- the rule file ------------------------------------------------------------------------------


def _parse(text: str) -> tuple[PolicyRule, ...]:
    return parse_policy_rules(text, source="rules.yaml")


def test_a_rule_file_can_name_a_resource_and_spell_needs_approval_as_ask() -> None:
    (rule,) = _parse(
        """
rules:
  - rule_id: ask-fraud
    tool_pattern: ask_peer
    resource: fraud
    effect: ask
    reason: Fraud sees customer data; a person confirms first.
"""
    )

    assert rule.resource_pattern == "fraud"
    assert rule.effect is Effect.NEEDS_APPROVAL


def test_a_rule_file_without_a_resource_is_unchanged() -> None:
    (rule,) = _parse(
        """
rules:
  - rule_id: any-peer
    tool_pattern: ask_peer
    effect: allow
    reason: Peers are allowed.
"""
    )

    assert rule.resource_pattern is None


@pytest.mark.parametrize("bad", ["''", "7", "[fraud]"])
def test_a_resource_that_is_not_text_is_refused(bad: str) -> None:
    with pytest.raises(PolicyRuleFileError, match="resource"):
        _parse(
            f"""
rules:
  - rule_id: r
    tool_pattern: ask_peer
    resource: {bad}
    effect: deny
    reason: x
"""
        )


def test_the_stored_definition_round_trips_the_resource() -> None:
    from agent_core.adapters.driven.policy_fs.loader import _definition

    rule = _rule("r", Effect.DENY, resource="fraud")
    stored = _definition(rule)
    rebuilt = _policy()._to_rule("r", stored)  # noqa: SLF001

    assert stored["resource_pattern"] == "fraud"
    assert rebuilt == rule
    assert "resource_pattern" not in _definition(_rule("plain", Effect.ALLOW)), (
        "a rule without a resource is stored exactly as before"
    )
