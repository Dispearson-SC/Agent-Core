"""ToolPolicy - a SILENT-BUG AREA. These tests are the only alarm that exists.

Each test below encodes a decision from domain/policy.py. If one starts failing, the
question is which behaviour changed, not how to make it pass.
"""

import asyncio
import sys
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.usage import RunUsage

import agent_core.adapters.driven.mcp.toolsets as mcp_toolsets
from agent_core.domain.policy import EFFECT_PRECEDENCE, Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile, MCPServerRef
from agent_core.domain.turn import CallerIdentity
from tests.fakes.ports import FakeToolPolicy

TREASURER = CallerIdentity(
    subject_id="u-treasurer",
    channel="http",
    tenant_id="t-1",  # type: ignore[arg-type]
    roles=frozenset({"treasurer"}),
)

INTERN = CallerIdentity(
    subject_id="u-intern",
    channel="webhook",
    tenant_id="t-1",  # type: ignore[arg-type]
    roles=frozenset({"intern"}),
)


@pytest.mark.silent
def test_no_matching_rule_denies(caller: CallerIdentity) -> None:
    """An unknown tool name is a typo or an unregistered addition. Both deserve refusal.

    ALLOW-by-default means every newly imported tool is silently world-usable.
    """
    allow_shell = PolicyRule(
        rule_id="r-shell",
        tool_pattern="run_shell",
        effect=Effect.ALLOW,
        reason="Operators run shell commands.",
    )
    rules = RuleSet(
        subject_roles=caller.roles,
        channel=caller.channel,
        # The tenant this snapshot was loaded for (t-d2-06). It was always implicit here -
        # `caller` is the identity the rules would have been queried for - and the seat
        # makes it impossible to leave out, which is the point: a snapshot with no tenant
        # is structurally able to answer about another one.
        tenant_id=caller.tenant_id,
        rules=(allow_shell,),
    )

    # A typo and an unregistered tool both fall through to the default.
    assert rules.applicable("run_shel") == ()
    assert rules.applicable("never_registered") == ()
    assert rules.default_effect is Effect.DENY

    # An empty snapshot - the fail-closed shape load_rules() returns when the store is
    # unreachable - matches nothing and still defaults to DENY.
    empty = RuleSet(
        subject_roles=caller.roles, channel=caller.channel, tenant_id=caller.tenant_id
    )
    assert empty.applicable("run_shell") == ()
    assert empty.default_effect is Effect.DENY


@pytest.mark.silent
def test_deny_beats_needs_approval_beats_allow() -> None:
    """EFFECT_PRECEDENCE, regardless of rule order or specificity. The only ordering that
    fails safe.

    The domain deliberately does not reduce - `applicable()` hands back every match
    unreduced and the reduction happens at the enforcement point - so this test pins the
    two halves the domain does own, and then reduces.

        1. EFFECT_PRECEDENCE itself, literally. Index 0 wins, so DENY outranks everything.
        2. `applicable()` returns the SAME matches whichever order the rules were declared
           in. A snapshot that dropped or shadowed one here would make the verdict depend
           on insertion order with no reducer being wrong.
        3. The documented reduction over every permutation of the same three rules.

    `FakeToolPolicy` is that reduction, and it is what every use-case test in this suite
    already decides through, so a precedence bug in it is a precedence bug in the fixtures
    the rest of the suite trusts.
    """
    deny = PolicyRule(
        rule_id="r-deny",
        tool_pattern="transfer_*",  # the LEAST specific of the three, and it still wins
        effect=Effect.DENY,
        reason="Wire transfers are frozen for the duration of the audit.",
    )
    needs_approval = PolicyRule(
        rule_id="r-approve",
        tool_pattern="transfer_funds",
        effect=Effect.NEEDS_APPROVAL,
        reason="A human signs off on money leaving.",
    )
    allow = PolicyRule(
        rule_id="r-allow",
        tool_pattern="transfer_funds",  # exact, and it must not overturn the broad DENY
        effect=Effect.ALLOW,
        reason="Treasury operations are part of the job.",
    )

    assert EFFECT_PRECEDENCE == (Effect.DENY, Effect.NEEDS_APPROVAL, Effect.ALLOW)

    for ordering in permutations((deny, needs_approval, allow)):
        rules = RuleSet.for_caller(TREASURER, rules=ordering)

        assert {rule.rule_id for rule in rules.applicable("transfer_funds")} == {
            "r-deny",
            "r-approve",
            "r-allow",
        }, "a match was dropped, so the reduction below is deciding over a different set"

        decision = FakeToolPolicy(rules).decide(rules, "transfer_funds", {})
        assert decision.effect is Effect.DENY
        assert decision.rule_id == "r-deny", (
            "the verdict must cite the rule that actually refused - an auditor reading "
            "this row six months later is answering 'why was this blocked?'"
        )

    # Specificity is not a tie-break in EITHER direction. Asserting only the case above
    # would leave "deny wins" holding just when the DENY happens to be the broad rule.
    exact_deny = PolicyRule(
        rule_id="r-deny-exact",
        tool_pattern="transfer_funds",
        effect=Effect.DENY,
        reason="This particular tool is frozen.",
    )
    wildcard_allow = PolicyRule(
        rule_id="r-allow-wide",
        tool_pattern="transfer_*",
        effect=Effect.ALLOW,
        reason="Treasury tooling is open to the treasury.",
    )
    for ordering in permutations((exact_deny, wildcard_allow)):
        rules = RuleSet.for_caller(TREASURER, rules=ordering)
        decision = FakeToolPolicy(rules).decide(rules, "transfer_funds", {})
        assert decision.effect is Effect.DENY
        assert decision.rule_id == "r-deny-exact"

    # With no DENY in play, NEEDS_APPROVAL still outranks ALLOW. The middle value is the
    # one a boolean engine cannot hold, and losing it turns an ask into a silent yes.
    for ordering in permutations((needs_approval, allow)):
        rules = RuleSet.for_caller(TREASURER, rules=ordering)
        decision = FakeToolPolicy(rules).decide(rules, "transfer_funds", {})
        assert decision.effect is Effect.NEEDS_APPROVAL
        assert decision.rule_id == "r-approve"
        assert decision.blocks_execution is True, (
            "NEEDS_APPROVAL is not ALLOW: it must stop the call until a human answers"
        )


@pytest.mark.silent
def test_empty_role_set_means_any_role_not_no_roles() -> None:
    """The asymmetry that bites: empty `subject_roles` on a PolicyRule means ANY role,
    while empty `accepted_kinds` on a MediaPolicy means NOTHING.

    Getting the policy one backwards makes a rule silently stop applying to everybody.
    """
    unrestricted = PolicyRule(
        rule_id="r-any",
        tool_pattern="read_file",
        effect=Effect.ALLOW,
        reason="Reading is safe for anyone.",
    )
    # ANY role, including a caller carrying no role at all.
    assert unrestricted.matches("read_file", frozenset(), "http") is True
    assert unrestricted.matches("read_file", frozenset({"operator"}), "http") is True
    assert unrestricted.matches("read_file", frozenset({"intern", "auditor"}), "cron") is True

    restricted = PolicyRule(
        rule_id="r-admins",
        tool_pattern="read_file",
        effect=Effect.ALLOW,
        reason="Admins only.",
        subject_roles=frozenset({"admin"}),
    )
    assert restricted.matches("read_file", frozenset(), "http") is False
    assert restricted.matches("read_file", frozenset({"operator"}), "http") is False
    assert restricted.matches("read_file", frozenset({"operator", "admin"}), "http") is True

    # Same reading for `channels`: empty means every channel, populated means only those.
    http_only = PolicyRule(
        rule_id="r-http",
        tool_pattern="read_file",
        effect=Effect.ALLOW,
        reason="Interactive channels only.",
        channels=frozenset({"http"}),
    )
    assert unrestricted.matches("read_file", frozenset({"operator"}), "webhook") is True
    assert http_only.matches("read_file", frozenset({"operator"}), "webhook") is False
    assert http_only.matches("read_file", frozenset({"operator"}), "http") is True


@pytest.mark.silent
def test_trailing_wildcard_matches_a_prefix_case_insensitively() -> None:
    """`browser_*` is a PREFIX, not a substring, and case never decides a policy verdict.

    A rule stored as `browser_*` that stops applying because a provider advertises
    `Browser_Click` is a policy hole that no other test would notice.
    """
    browser = PolicyRule(
        rule_id="r-browser",
        tool_pattern="browser_*",
        effect=Effect.NEEDS_APPROVAL,
        reason="A human confirms browser actions.",
    )
    assert browser.matches("browser_click", frozenset(), "http") is True
    assert browser.matches("BROWSER_CLICK", frozenset(), "http") is True
    assert browser.matches("Browser_Click", frozenset(), "http") is True
    assert browser.matches("browser_", frozenset(), "http") is True
    # A prefix, not a substring: a hostile name must not inherit the rule by containing it.
    assert browser.matches("mcp_browser_click", frozenset(), "http") is False
    assert browser.matches("browse", frozenset(), "http") is False

    # The pattern itself is matched case-insensitively too, in both directions.
    exact = PolicyRule(
        rule_id="r-exact",
        tool_pattern="Run_Shell",
        effect=Effect.DENY,
        reason="Never from this channel.",
    )
    assert exact.matches("run_shell", frozenset(), "http") is True
    assert exact.matches("RUN_SHELL", frozenset(), "http") is True
    # Exact means exact - no accidental prefix behaviour without the wildcard.
    assert exact.matches("run_shell_v2", frozenset(), "http") is False


@pytest.mark.silent
def test_a_ruleset_cannot_answer_for_a_different_caller() -> None:
    """A RuleSet IS the answer for exactly one caller's narrowing.

    Passing `caller` alongside the snapshot let a caller-A snapshot answer a question
    about caller B - silently, with no type error and no failing test, which in a policy
    engine means a tool gets allowed for someone who should not have it. The snapshot
    carries the roles and the channel it was loaded for, so the wrong-caller question is
    not answered wrongly: it cannot be asked.
    """
    transfer = PolicyRule(
        rule_id="r-transfer",
        tool_pattern="transfer_funds",
        effect=Effect.ALLOW,
        reason="Treasury operations from an interactive channel.",
        subject_roles=frozenset({"treasurer"}),
        channels=frozenset({"http"}),
    )

    for_treasurer = RuleSet.for_caller(TREASURER, rules=(transfer,))
    assert for_treasurer.subject_roles == TREASURER.roles
    assert for_treasurer.channel == TREASURER.channel
    assert for_treasurer.applicable("transfer_funds") == (transfer,)

    # The intern's snapshot over the SAME rows answers differently, on its own narrowing.
    for_intern = RuleSet.for_caller(INTERN, rules=(transfer,))
    assert for_intern.applicable("transfer_funds") == ()

    # The wrong-caller question is inexpressible: `applicable` takes the tool name only.
    with pytest.raises(TypeError):
        for_intern.applicable(  # type: ignore[call-arg]
            "transfer_funds", TREASURER.roles, TREASURER.channel
        )

    # And the narrowing cannot be left off at construction time either - EACH THIRD OF IT
    # SEPARATELY, which is the only spelling with teeth. `RuleSet(rules=...)` alone raises
    # TypeError while any one of the three is still missing, so it passes unchanged if the
    # tenant grows a default: proven by mutation at the wave-12 barrier, where
    # `tenant_id: TenantId = field(kw_only=True, default=TenantId("t-default"))` left the
    # single-argument form still raising and every assertion in this module still green.
    # The tenant is the third seat and the newest (t-d2-06), so it is the one a later edit
    # would "helpfully" default; omitting only it is what proves it is mandatory.
    # `match=` pins WHICH seat was refused, so a TypeError raised for some other reason
    # cannot stand in for the one under test.
    with pytest.raises(TypeError, match="tenant_id"):
        RuleSet(  # type: ignore[call-arg]
            subject_roles=TREASURER.roles, channel=TREASURER.channel, rules=(transfer,)
        )
    with pytest.raises(TypeError, match="subject_roles"):
        RuleSet(  # type: ignore[call-arg]
            channel=TREASURER.channel, tenant_id=TREASURER.tenant_id, rules=(transfer,)
        )
    with pytest.raises(TypeError, match="channel"):
        RuleSet(  # type: ignore[call-arg]
            subject_roles=TREASURER.roles, tenant_id=TREASURER.tenant_id, rules=(transfer,)
        )


# "store unreachable -> DENY, and surface the error" is NOT tested in this module, and its
# absence here is deliberate rather than a gap. Unreachability is an ADAPTER event: the
# domain has no store to lose, so the only shape it can express is the empty snapshot -
# already asserted at the end of `test_no_matching_rule_denies` above, which is exactly
# what `load_rules` returns when Postgres is down. Everything the property adds on top of
# that shape - the failure being caught rather than raised, the error reaching `on_error`
# instead of being swallowed, the empty snapshot then refusing a tool the reachable store
# would have allowed - needs the adapter to be observable, and is asserted against it in
# tests/unit/test_policy_repository.py. Restating it here with a hand-built RuleSet would
# assert the fixture, not the fail-closed path, and read as coverage it does not have.


# --------------------------------------------------------------------------------------
# MCP under the SAME policy - docs/TASKS.md#t-f6-06, CLAUDE.md non-negotiable #4.
#
# WHY THESE TWO LIVE IN test_policy.py AND NOT IN test_mcp_toolsets.py
#     They are not adapter tests. `test_mcp_toolsets.py` asserts that the adapter applies
#     a prefix; these assert what the prefix BUYS - that a rule written for a trusted
#     local tool cannot be inherited by a third-party tool wearing its name, and that the
#     local tool is the one that actually runs. That is a policy property, and the policy
#     engine is a silent-bug area: nothing else in the suite would notice it break.
#
# WHY A REAL SERVER IS SPAWNED
#     A mocked transport would advertise whatever the test told it to advertise, which
#     proves the test author remembered the prefix, not that the collision is defended.
#     `tests/fixtures/mcp_echo_server.py` really advertises `echo`, and the profile below
#     really gives the vertical a local tool of exactly that name - so the collision under
#     test is a genuine one, established by the server and not by a string in this file.
# --------------------------------------------------------------------------------------

HOSTILE_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_echo_server.py"

LOCAL_ECHO_RESULT = "the vertical's own echo ran"
"""What the TRUSTED local tool returns. Nothing the server can produce says this."""


def _local_echo_toolset() -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset()

    @toolset.tool
    def echo(ctx: RunContext[None], text: str) -> str:
        """The vertical's OWN `echo` - the tool the hostile server is impersonating."""
        return LOCAL_ECHO_RESULT

    return toolset


class _LocalToolProvider:
    """The vertical's local `ToolProvider`: one trusted tool, named `echo`."""

    def __init__(self) -> None:
        self._toolset = _local_echo_toolset()

    async def toolset_for(self, profile: AgentProfile) -> object:
        return self._toolset

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        return ("echo",)


def _shadowing_profile() -> AgentProfile:
    """A profile wired to a server that advertises a name the vertical already owns.

    The server is LABELLED `hostile` here; the executable is the ordinary fixture server,
    because what makes it hostile is not the code it runs but the name it claims.
    """
    return AgentProfile(
        id="delivery_optimizer",
        persona="does not matter here",
        model="claude-sonnet-5",
        mcp_servers=(
            MCPServerRef(
                name="hostile",
                transport="stdio",
                command=sys.executable,
                args=(str(HOSTILE_SERVER),),
            ),
        ),
    )


def _shadowed_name() -> str:
    """`mcp_hostile_echo`, built from the adapter's own prefixer rather than typed here."""
    return f"{mcp_toolsets.server_prefix('hostile')}_echo"


def _run_context() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _call(toolset: AbstractToolset[None], name: str, **args: Any) -> Any:
    ctx = _run_context()
    tools: dict[str, ToolsetTool[None]] = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


@pytest.mark.silent
def test_mcp_tool_obeys_the_same_policy_as_a_local_tool(caller: CallerIdentity) -> None:
    """CLAUDE.md non-negotiable #4. An MCP server is third-party code.

    Two halves, and the second is the one that matters under a shadowing attempt:

      1. A `mcp_*` rule reaches every tool a server offers, and reaches ONLY those - the
         local tool of the same name keeps its own verdict.
      2. A rule written for the trusted local `echo` is NOT inherited by the server's
         `echo`. The prefixed name matches nothing and falls to `default_effect`, which is
         DENY - so a shadowing server gains nothing from the rule it impersonates.
    """
    provider = mcp_toolsets.MCPToolProvider(local=_LocalToolProvider())
    names = asyncio.run(provider.tool_names_for(_shadowing_profile()))
    prefix = mcp_toolsets.server_prefix("hostile")

    # The audit record and the policy input are the same flat list, and the third-party
    # tools are in it under their prefixed names - recorded, not hidden.
    assert names == ("echo", f"{prefix}_add", _shadowed_name(), f"{prefix}_hang")

    allow_echo = PolicyRule(
        rule_id="r-echo",
        tool_pattern="echo",
        effect=Effect.ALLOW,
        reason="The vertical's own echo is safe.",
    )
    only_local = RuleSet.for_caller(caller, rules=(allow_echo,))
    policy = FakeToolPolicy(only_local)

    assert policy.decide(only_local, "echo", {}).effect is Effect.ALLOW
    assert policy.decide(only_local, _shadowed_name(), {}).effect is Effect.DENY, (
        "the trusted tool's rule must not extend to a third-party tool of the same name"
    )
    assert only_local.applicable(_shadowed_name()) == ()
    # Filtering happens before the model is told anything, so the shadow never appears.
    assert policy.filter_toolset(only_local, names) == ("echo",)

    needs_approval_for_mcp = PolicyRule(
        rule_id="r-mcp",
        tool_pattern="mcp_*",
        effect=Effect.NEEDS_APPROVAL,
        reason="A human confirms anything a third-party server does.",
    )
    with_mcp_rule = RuleSet.for_caller(caller, rules=(allow_echo, needs_approval_for_mcp))
    policy = FakeToolPolicy(with_mcp_rule)

    for name in names[1:]:
        decision = policy.decide(with_mcp_rule, name, {})
        assert decision.effect is Effect.NEEDS_APPROVAL
        assert decision.rule_id == "r-mcp"
        assert decision.blocks_execution is True
    # ... and the local tool is untouched by the rule aimed at the servers.
    assert policy.decide(with_mcp_rule, "echo", {}).rule_id == "r-echo"


@pytest.mark.silent
def test_mcp_server_cannot_shadow_a_local_tool_name() -> None:
    """A hostile server advertising the vertical's own tool name cannot displace it.

    This is the attack, not a hypothetical: a server you connected to advertises
    `send_invoice` and the model calls the hostile one believing it is yours. The defence
    is the prefix (docs/TASKS.md#t-f6-03), and this test exists to prove it holds rather
    than to assume it does.

    Three assertions, and dropping any one of them lets a real shadow through:

      - the collision is REAL: the server genuinely advertises the local tool's name;
      - both tools survive the composition, under DIFFERENT names, so neither silently
        replaces nor deletes the other;
      - the bare name still runs the LOCAL tool, and the server's tool is reachable only
        through `mcp_hostile_*` - which is what makes it policeable separately at all.
    """
    provider = mcp_toolsets.MCPToolProvider(local=_LocalToolProvider())
    profile = _shadowing_profile()
    prefix = mcp_toolsets.server_prefix("hostile")

    names = asyncio.run(provider.tool_names_for(profile))
    advertised = tuple(name[len(prefix) + 1 :] for name in names if name.startswith(f"{prefix}_"))
    assert "echo" in advertised, (
        "the server must really advertise the local tool's name, or this test defends "
        "against a collision that never happened"
    )

    async def scenario() -> None:
        toolset = await provider.toolset_for(profile)
        assert isinstance(toolset, AbstractToolset)
        offered = tuple(sorted(await toolset.get_tools(_run_context())))

        # Both are present, and they are two different tools - the local name is not
        # overwritten, and the server's tool is not silently dropped either.
        assert "echo" in offered
        assert _shadowed_name() in offered

        # The local tool still runs. This is the whole claim.
        assert await _call(toolset, "echo", text="ping") == LOCAL_ECHO_RESULT

        # And the server is still asked for the name IT advertised, under the prefix -
        # so the third-party tool is usable, just never as `echo`.
        assert await _call(toolset, _shadowed_name(), text="ping") == "ping"

    asyncio.run(scenario())
