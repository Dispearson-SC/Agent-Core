"""The operator console, driven over fakes - no terminal, no database, no model.

Phase:   F0
Tasks:   docs/TASKS.md#t-f0-07

WHY THE CONSOLE TAKES ITS INPUT AND OUTPUT AS SEAMS
    A REPL that can only be exercised by a human typing at it is a REPL nobody tests, and
    this one is the surface an operator will judge the whole system from. So `read_line`
    and `write_line` are injected: a test feeds it a script of commands and reads back
    every line it printed.

WHAT IS REAL HERE AND WHAT IS FAKE
    The POLICY ENGINE and the PROFILE are real - `ToolPolicy.decide` over a real `RuleSet`
    of real `PolicyRule`s, and real `AgentProfile` objects carrying a real `ApprovalRule`.
    That is deliberate: the console's whole claim is that a refusal it shows you is the
    refusal production would produce, so the parts that decide a refusal must not be
    stubbed out here.

    `StartTurn` is the real use case too, wired to the shared fakes. Only the model runner
    and the audit READ are scripted - the runner because there is no model in a unit test,
    and the read because the audit trail it projects is written by a database this test
    deliberately does not have.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from decimal import Decimal

import pytest

from agent_core.adapters.driving.cli.console import Console, ObservedToolCall
from agent_core.application.start_turn import StartTurn
from agent_core.domain.compaction import (
    CompactionPolicy,
    CompactionResult,
    ContextState,
)
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import (
    AgentProfile,
    ApprovalRule,
    MCPServerRef,
    ProfileVersionRegistry,
)
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnResult,
    Usage,
)
from agent_core.ports.skill_registry import SkillMeta
from tests.fakes.ports import (
    FakeAgentRunner,
    FakeAuditSink,
    FakeConversationStore,
    FakeToolPolicy,
    FakeToolProvider,
)

pytestmark = pytest.mark.phase("F0")


# The one credential-shaped string in this file. `:profile` renders an MCP server, and an
# MCP URL is exactly where a token ends up in real configuration - so the console must be
# able to name the server without reprinting what authenticates to it.
_SECRET = "s3cr3t-mcp-token"


class _StubContextEngine:
    """`ContextEngine` is one of `StartTurn`'s eight seats and there is no shared fake yet.

    Only `update_from_response` is on the first-turn path, so that is the only member with
    anything to record; the rest exist because the port has them.
    """

    def __init__(self) -> None:
        self.updates: list[tuple[SessionRef, Usage]] = []

    def on_session_start(self, session: SessionRef) -> None:
        return None

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        self.updates.append((session, usage))

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        return False

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        raise AssertionError("the first-turn path must not compact")

    def on_session_end(self, session: SessionRef) -> None:
        return None


class _StubSkillRegistry:
    """`StartTurn` holds a `SkillRegistry` and does not call it yet (step 4 is F6)."""

    async def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        return ()

    async def read(self, name: str) -> str:
        raise AssertionError("the first-turn path must not read a skill body")


class _ScriptedToolCallLog:
    """The audit READ the console renders a turn's tool calls from.

    Scripted rather than faked-over-a-sink: in production this projects rows
    `PgAuditSink` already wrote, and what this test is about is whether the console
    RENDERS a decision and its `rule_id`, not whether Postgres round-trips one.
    """

    def __init__(self, calls: Sequence[ObservedToolCall] = ()) -> None:
        self.calls = tuple(calls)
        self.asked: list[TurnId] = []

    async def for_turn(self, turn_id: TurnId) -> tuple[ObservedToolCall, ...]:
        self.asked.append(turn_id)
        return self.calls


def _script(*lines: str) -> Callable[[str], str | None]:
    """A canned session. Returns `None` once exhausted, which is what EOF looks like."""
    remaining = list(lines)

    def read_line(prompt: str) -> str | None:
        return remaining.pop(0) if remaining else None

    return read_line


def _delivery_profile() -> AgentProfile:
    return AgentProfile(
        id="delivery_optimizer",
        persona="You optimize delivery routing and pricing.",
        model="claude-sonnet-5",
        toolsets=("delivery",),
        mcp_servers=(
            MCPServerRef(
                name="maps",
                transport="http",
                url=f"https://maps.invalid/mcp?token={_SECRET}",
            ),
        ),
        skill_namespaces=("delivery",),
        max_iterations=15,
        max_cost_usd=Decimal("0.25"),
        approval_rules=(
            ApprovalRule(
                tool_name="pricing_apply",
                reason="Price change above 15% needs a human.",
                condition="abs(pct_change) > 15",
            ),
        ),
    )


def _fraud_profile() -> AgentProfile:
    return AgentProfile(
        id="fraud_analyst",
        persona="You review flagged transactions for fraud signals.",
        model="claude-sonnet-5",
        toolsets=("fraud",),
        skill_namespaces=("fraud",),
        max_iterations=20,
        max_cost_usd=Decimal("0.50"),
        approval_rules=(
            ApprovalRule(
                tool_name="freeze_account",
                reason="Freezing an account always needs a human.",
            ),
        ),
    )


def _profiles() -> dict[str, AgentProfile]:
    """Version-assigned exactly as `composition.load_profiles` does it."""
    registry = ProfileVersionRegistry()
    return {
        profile.id: profile
        for profile in (
            registry.assign(_delivery_profile()),
            registry.assign(_fraud_profile()),
        )
    }


def _rules(caller: CallerIdentity) -> RuleSet:
    return RuleSet.for_caller(
        caller,
        rules=(
            PolicyRule(
                rule_id="delivery-read",
                tool_pattern="route_*",
                effect=Effect.ALLOW,
                reason="Route lookups are read-only.",
            ),
            PolicyRule(
                rule_id="pricing-four-eyes",
                tool_pattern="pricing_apply",
                effect=Effect.NEEDS_APPROVAL,
                reason="A price change is decided by a person.",
            ),
            PolicyRule(
                rule_id="no-third-party",
                tool_pattern="mcp_*",
                effect=Effect.DENY,
                reason="This caller may not reach a third-party server.",
            ),
        ),
    )


@pytest.fixture
def cli_caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="operator",
        channel="cli",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


@pytest.fixture
def cli_session() -> SessionRef:
    return SessionRef(session_id=SessionId("console-1"), tenant_id=TenantId("t-1"))


def _console(
    *,
    caller: CallerIdentity,
    session: SessionRef,
    script: Callable[[str], str | None],
    printed: list[str],
    outcome: TurnOutcome | None = None,
    tool_calls: _ScriptedToolCallLog | None = None,
    tool_names: tuple[str, ...] = ("route_lookup", "pricing_apply", "mcp_maps_geocode"),
) -> tuple[Console, FakeToolPolicy, FakeToolProvider, FakeConversationStore, FakeAuditSink]:
    profiles = _profiles()
    runner = FakeAgentRunner(
        outcome
        if outcome is not None
        else TurnOutcome(
            turn_id=TurnId("t-console"),
            result=TurnResult(text="Rerouted; margin +12 EUR."),
        )
    )
    tools = FakeToolProvider(toolset=object(), tool_names=tool_names)
    policy = FakeToolPolicy(_rules(caller))
    store = FakeConversationStore()
    audit = FakeAuditSink()

    start_turn = StartTurn(
        runner=runner,
        tools=tools,
        policy=policy,
        store=store,
        audit=audit,
        context=_StubContextEngine(),
        skills=_StubSkillRegistry(),
        profiles=profiles,
    )

    ids = iter(f"turn-{n}" for n in range(1, 100))
    console = Console(
        profiles=profiles,
        tools=tools,
        policy=policy,
        start_turn=start_turn,
        tool_calls=tool_calls if tool_calls is not None else _ScriptedToolCallLog(),
        caller=caller,
        session=session,
        write_line=printed.append,
        read_line=script,
        new_turn_id=lambda: TurnId(next(ids)),
    )
    return console, policy, tools, store, audit


# --------------------------------------------------------------------------------------
# The banner: what this mode does NOT exercise
# --------------------------------------------------------------------------------------


def test_the_banner_says_which_guarantees_this_mode_does_not_exercise(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """A tool that silently exercises less than the real path is how somebody concludes
    the system works when they have not tested the part that breaks.

    The console calls `StartTurn` directly, so neither DBOS durability nor the per-session
    coalescing window is on. That is the right call for an inspection tool and the wrong
    thing to hide, so it is the first thing printed.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller, session=cli_session, script=_script(":quit"), printed=printed
    )

    asyncio.run(console.run())

    banner = "\n".join(printed).lower()
    assert "durab" in banner
    assert "coalesc" in banner
    assert "admin" in banner, "the console renders an ADMIN audience and must say so"


# --------------------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------------------


def test_agents_lists_every_loaded_profile_with_its_version(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":agents", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    assert "delivery_optimizer" in output
    assert "fraud_analyst" in output
    # D20: a profile that never passed a registry is version 0, and a turn must never
    # record an unverifiable number. If the console shows 0 here, the load path is wrong.
    assert "v1" in output


def test_use_switches_the_agent_and_says_what_changed(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Switching agent changes the persona, the toolset AND what policy will allow.

    An operator who is told only "switched" has to go and ask three more questions.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":use fraud_analyst", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    assert "fraud_analyst" in output
    assert "fraud" in output, "the new toolset is part of what changed"
    assert console.profile_id == "fraud_analyst"


def test_use_refuses_an_unknown_id_without_switching(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """`StartTurn` refuses to fall back to a default profile; so does this.

    A typo that silently leaves you talking to the previous agent is worse than an error:
    every answer after it is attributed to the wrong profile.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":use nope", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert console.profile_id == "delivery_optimizer"
    assert "nope" in "\n".join(printed)


# --------------------------------------------------------------------------------------
# :profile - the RESOLVED configuration, and never a credential
# --------------------------------------------------------------------------------------


def test_profile_renders_the_resolved_configuration(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Resolved, not the raw YAML: the point is to see what the system made of the file.

    So the defaulted blocks appear too - knowledge and peers are absent from both profile
    files on disk, and an operator asking "does this agent have knowledge retrieval" must
    get an answer rather than silence.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":profile", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    assert "claude-sonnet-5" in output
    assert "delivery" in output
    assert "15" in output, "max_iterations"
    assert "0.25" in output, "max_cost_usd"
    assert "pricing_apply" in output, "the approval rule the profile carries"
    assert "abs(pct_change) > 15" in output, "and the condition it fires on"
    assert "knowledge" in output.lower()
    assert "maps" in output, "the MCP server is named"


def test_profile_never_prints_a_credential(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """An MCP URL is where a token lives in real configuration.

    The console is an inspection tool people run over a shoulder and paste into tickets.
    Naming the server is the useful part; reprinting what authenticates to it is not.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":profile", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert _SECRET not in "\n".join(printed)


# --------------------------------------------------------------------------------------
# :tools - the profile -> toolset resolution, through the real port
# --------------------------------------------------------------------------------------


def test_tools_resolves_through_the_real_tool_provider_and_shows_the_narrowing(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """`ToolProvider.tool_names_for(profile)` is what t-f1-21 exists to perform.

    The verdict beside each name is the other half: the model is never SHOWN a DENY tool
    (advertising a forbidden tool invites the model to keep trying it and burns budget),
    so an operator needs to see both the resolved set and the narrowed one.
    """
    printed: list[str] = []
    console, _policy, tools, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":tools", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert [profile.id for profile in tools.tool_names_for_calls] == ["delivery_optimizer"]

    output = "\n".join(printed)
    assert "route_lookup" in output
    assert "mcp_maps_geocode" in output, "an mcp_-prefixed name must be visible as such"
    assert Effect.DENY.value in output
    assert "no-third-party" in output, "and the rule that denied it"


# --------------------------------------------------------------------------------------
# :policy - which rule wins, by rule_id
# --------------------------------------------------------------------------------------


def test_policy_for_one_tool_names_the_rule_that_wins(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Policy is a silent-bug area: a hole never fails a test, it just never fires.

    A human being able to ask the live engine directly is worth more than another test,
    and the answer is only useful if it names the rule_id an auditor would read.
    """
    printed: list[str] = []
    console, policy, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":policy pricing_apply", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert policy.load_rules_calls == [cli_caller], "the live engine, for THIS caller"

    output = "\n".join(printed)
    assert "pricing_apply" in output
    assert Effect.NEEDS_APPROVAL.value in output
    assert "pricing-four-eyes" in output
    assert "A price change is decided by a person." in output


def test_policy_with_no_tool_answers_for_every_tool_of_the_profile(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":policy", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    for name in ("route_lookup", "pricing_apply", "mcp_maps_geocode"):
        assert name in output
    for rule_id in ("delivery-read", "pricing-four-eyes", "no-third-party"):
        assert rule_id in output


def test_policy_reports_an_unknown_tool_as_the_default_effect(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """The default must be DENY, and an operator must be able to see that it is.

    A default of ALLOW makes every newly registered tool world-usable the moment it is
    imported, and nothing in the suite says so - this is the question that catches it.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":policy drop_database", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    assert "drop_database" in output
    assert Effect.DENY.value in output


# --------------------------------------------------------------------------------------
# A turn, and the tool calls it made
# --------------------------------------------------------------------------------------


def test_plain_text_runs_a_turn_through_the_real_use_case(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Plain text is a turn. It goes through `StartTurn`, which means through policy,
    through the store and through the audit sink - never around them.
    """
    printed: list[str] = []
    console, policy, tools, store, audit = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script("reroute the 14:00 batch", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert policy.load_rules_calls == [cli_caller]
    assert [profile.id for profile in tools.tool_names_for_calls] == ["delivery_optimizer"]
    assert [turn_id for turn_id, _ in store.requests] == [TurnId("turn-1")]
    assert store.requests[0][1].input.text == "reroute the 14:00 batch"
    assert store.requests[0][1].profile_id == "delivery_optimizer"
    assert [call.kind for call in audit.calls] == ["turn_end"]
    assert "Rerouted; margin +12 EUR." in "\n".join(printed)


def test_a_turn_shows_every_tool_call_with_its_decision_and_rule_id(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """"Ver cómo usan las herramientas" is the point; the rest is navigation.

    This is also F1's own done-when criterion made visible: a tool denied by policy does
    not execute, and the denial - with the rule_id that produced it - is in the trail.
    """
    log = _ScriptedToolCallLog(
        (
            ObservedToolCall(
                tool_name="route_lookup",
                arguments={"batch": "14:00"},
                effect=Effect.ALLOW,
                rule_id="delivery-read",
            ),
            ObservedToolCall(
                tool_name="mcp_maps_geocode",
                arguments={"address": "Calle Mayor 1"},
                effect=Effect.DENY,
                rule_id="no-third-party",
            ),
        )
    )
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script("reroute the 14:00 batch", ":quit"),
        printed=printed,
        tool_calls=log,
    )

    asyncio.run(console.run())

    assert log.asked == [TurnId("turn-1")], "the trail is read for the turn that just ran"

    output = "\n".join(printed)
    assert "route_lookup" in output
    assert "delivery-read" in output
    assert "14:00" in output, "arguments, in brief"
    assert "mcp_maps_geocode" in output
    assert "no-third-party" in output
    assert Effect.DENY.value in output
    # Whether it RAN is a different fact from what policy said, and both must be legible.
    assert "not executed" in output.lower()


def test_audit_re_renders_the_tool_calls_of_the_last_turn(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    log = _ScriptedToolCallLog(
        (
            ObservedToolCall(
                tool_name="pricing_apply",
                arguments={"pct_change": 22},
                effect=Effect.NEEDS_APPROVAL,
                rule_id="pricing-four-eyes",
            ),
        )
    )
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script("raise the price", ":audit", ":quit"),
        printed=printed,
        tool_calls=log,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    assert output.count("pricing-four-eyes") >= 2, "once in the turn, once from :audit"
    assert Effect.NEEDS_APPROVAL.value in output


def test_audit_before_any_turn_says_so_rather_than_printing_nothing(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":audit", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert "no turn" in "\n".join(printed).lower()


# --------------------------------------------------------------------------------------
# Suspension
# --------------------------------------------------------------------------------------


def test_a_suspended_turn_says_so_and_says_what_it_is_waiting_for(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Non-negotiable #11 governs what a USER sees. This is an ADMIN surface.

    So the tool name and the correlation the operator needs to answer it are shown here,
    where they are exactly the information the job requires - and the console's banner
    says which audience it renders, so nobody mistakes this view for the user's.
    """
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script("freeze that account", ":quit"),
        printed=printed,
        outcome=TurnOutcome(
            turn_id=TurnId("t-console"),
            pending=(
                PendingRequest(
                    kind=PendingKind.APPROVAL,
                    tool_call_id=ToolCallId("call-9"),
                    tool_name="freeze_account",
                    arguments={"account_id": "a-1"},
                    reason="Freezing an account always needs a human.",
                ),
            ),
        ),
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    assert "suspend" in output.lower()
    assert PendingKind.APPROVAL.value in output
    assert "freeze_account" in output
    assert "call-9" in output
    assert "Freezing an account always needs a human." in output


# --------------------------------------------------------------------------------------
# The loop itself
# --------------------------------------------------------------------------------------


def test_help_lists_every_command_the_console_accepts(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":help", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    output = "\n".join(printed)
    for command in (":agents", ":use", ":profile", ":tools", ":policy", ":audit", ":quit"):
        assert command in output


def test_an_unknown_command_is_reported_and_the_loop_survives_it(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """A REPL that dies on a typo is a REPL an operator stops trusting mid-investigation."""
    printed: list[str] = []
    console, _policy, _tools, store, _audit = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script(":nonsense", "hello", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert ":nonsense" in "\n".join(printed)
    assert len(store.requests) == 1, "the turn after the bad command still ran"


def test_an_empty_line_is_not_a_turn(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Pressing enter must not spend money."""
    printed: list[str] = []
    console, _policy, _tools, store, _audit = _console(
        caller=cli_caller,
        session=cli_session,
        script=_script("", "   ", ":quit"),
        printed=printed,
    )

    asyncio.run(console.run())

    assert store.requests == []


def test_end_of_input_ends_the_session_like_quit_does(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """`read_line` returning None is EOF - a piped script, or ctrl-D."""
    printed: list[str] = []
    console, *_ = _console(
        caller=cli_caller, session=cli_session, script=_script(), printed=printed
    )

    asyncio.run(console.run())

    assert printed, "the banner still printed"


def test_an_unknown_profile_at_construction_is_refused(
    cli_caller: CallerIdentity, cli_session: SessionRef
) -> None:
    """Same reasoning as `:use`, one layer earlier: a mistyped `--profile` must not
    silently start a session against some other agent's permissions.
    """
    profiles = _profiles()
    with pytest.raises(KeyError):
        Console(
            profiles=profiles,
            tools=FakeToolProvider(toolset=object(), tool_names=()),
            policy=FakeToolPolicy(_rules(cli_caller)),
            start_turn=StartTurn(
                runner=FakeAgentRunner(
                    TurnOutcome(turn_id=TurnId("x"), result=TurnResult(text=""))
                ),
                tools=FakeToolProvider(toolset=object(), tool_names=()),
                policy=FakeToolPolicy(_rules(cli_caller)),
                store=FakeConversationStore(),
                audit=FakeAuditSink(),
                context=_StubContextEngine(),
                skills=_StubSkillRegistry(),
                profiles=profiles,
            ),
            tool_calls=_ScriptedToolCallLog(),
            caller=cli_caller,
            session=cli_session,
            write_line=lambda line: None,
            read_line=_script(),
            profile_id="not_a_profile",
        )
