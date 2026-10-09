"""The seam between a profile that declares an MCP server and a running toolset.

Tasks: docs/TASKS.md#t-f11-28, docs/TASKS.md#t-f11-30

WHY THIS IS AN INTEGRATION TEST AND NOT A UNIT ONE
    Every half of this already had a unit test. `tests/unit/test_mcp_toolsets.py` proves
    `MCPToolProvider` prefixes and bounds a real stdio server; `tests/unit/test_policy.py`
    proves a hostile server cannot shadow a local tool; `tests/unit/test_tool_provider.py`
    proves `LocalToolProvider` resolves a registry. What nothing asserted is that the
    provider PRODUCTION wires - `build_tool_provider(TOOL_PACKAGES)`, the object
    `build_container` hands the runner - can serve the profile this repository actually
    ships. It could not, so `preflight` refused `delivery_optimizer` and the process would
    not start at all.

    So this module spawns the real fixture server through the real registry, from the
    repository root the shipped profile's `args` are written against.

WHAT THE MIGRATION TEST IS DOING IN A FILE CALLED test_mcp_composition
    Nothing - they are two anchors in one wave, and `docs/WAVES.md` rule 2 gives an agent
    one test module. `t-f11-30` is at the bottom, under its own banner.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.usage import RunUsage

import agent_core.adapters.driven.mcp.toolsets as mcp_toolsets
import agent_core.adapters.driven.persistence_pg.migrations as migrations
import agent_core.adapters.driving.cli.preflight as preflight
from agent_core.adapters.driven.agent_pydantic.runner import budget_for, is_untrusted
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.adapters.driven.tools.provider import ToolsetBuilder, build_tool_provider
from agent_core.composition import TOOL_PACKAGES
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile, MCPServerRef
from agent_core.domain.turn import CallerIdentity, TenantId

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_PROFILE = REPO_ROOT / "Core" / "profiles" / "delivery_optimizer.yaml"
FIXTURE_SERVER = REPO_ROOT / "Core" / "tests" / "fixtures" / "mcp_echo_server.py"

# What `Core/profiles/delivery_optimizer.yaml` grants, read off the two halves it names:
# four local tools from the `delivery` package, and the fixture server's tools under the
# prefix - minus `hang`, which the profile excludes.
LOCAL_DELIVERY_TOOLS = ("routing_estimate", "pricing_quote", "pricing_apply", "orders_lookup")
MCP_ROUTING_TOOLS = ("mcp_routing_add", "mcp_routing_echo")

LOCAL_ECHO_RESULT = "the vertical's own echo ran"
"""What the TRUSTED local tool returns. Nothing the fixture server can produce says this."""


@pytest.fixture()
def at_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped profile's `args` are relative to the repository root, and say so.

    Honoured rather than rewritten: a test that swapped in an absolute path would be
    testing a profile nobody ships.
    """
    monkeypatch.chdir(REPO_ROOT)


def _shipped_profile() -> AgentProfile:
    return load_profile_sync(SHIPPED_PROFILE)


def _run_context() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _offered(toolset: object) -> tuple[str, ...]:
    """The names the model would actually be offered by whatever `toolset_for` returned.

    `ToolProvider.toolset_for` is typed `-> object` (the port names no Pydantic AI type),
    so the cast back to a toolset is asserted here rather than assumed.
    """
    assert isinstance(toolset, AbstractToolset)
    tools: dict[str, ToolsetTool[None]] = await toolset.get_tools(_run_context())
    return tuple(sorted(tools))


async def _call(toolset: object, name: str, **args: Any) -> Any:
    assert isinstance(toolset, AbstractToolset)
    ctx = _run_context()
    tools: dict[str, ToolsetTool[None]] = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


# --------------------------------------------------------------------------------------
# t-f11-28 - a profile that declares an MCP server must not stop the process.
# --------------------------------------------------------------------------------------


@pytest.mark.phase("F11")
def test_the_shipped_mcp_profile_no_longer_fails_preflight(at_repository_root: None) -> None:
    """The hard blocker, stated as the operator meets it.

    `preflight` asks the PRODUCTION provider whether every loaded profile can take a turn.
    `delivery_optimizer` declares a server, `build_tool_provider` could not compose one,
    and the answer was a hard `fail` - so `python -m agent_core serve` refused to start
    over a profile whose machinery had been finished five phases earlier.
    """
    profile = _shipped_profile()
    assert profile.mcp_servers, "this test is meaningless against a profile with no server"

    checks = asyncio.run(preflight._profile_checks({profile.id: profile}, TOOL_PACKAGES))

    failed = [check for check in checks if check.severity == "fail"]
    assert failed == [], f"the shipped profile still stops the process: {failed}"


@pytest.mark.phase("F11")
def test_the_production_provider_offers_the_local_tools_and_the_servers_together(
    at_repository_root: None,
) -> None:
    """One object carrying both halves, from `build_tool_provider(TOOL_PACKAGES)` itself.

    Asserted through BOTH port methods, because they are two separate resolution paths
    (policy filtering must never need a live connection) and therefore two chances for the
    composition to disagree with itself.
    """
    provider = build_tool_provider(TOOL_PACKAGES)
    profile = _shipped_profile()

    names = asyncio.run(provider.tool_names_for(profile))
    assert set(LOCAL_DELIVERY_TOOLS) <= set(names)
    assert set(MCP_ROUTING_TOOLS) <= set(names)
    # `tool_exclude: ["hang"]` is a scope reducer, and it still reduces after composition.
    assert "mcp_routing_hang" not in names

    offered = asyncio.run(_offered(asyncio.run(provider.toolset_for(profile))))
    assert set(LOCAL_DELIVERY_TOOLS) <= set(offered)
    assert set(MCP_ROUTING_TOOLS) <= set(offered)
    assert "mcp_routing_hang" not in offered


@pytest.mark.phase("F11")
@pytest.mark.silent
def test_non_negotiable_4_survives_composition(at_repository_root: None) -> None:
    """All three clauses, asked of the composed provider rather than of the adapter.

    Composition is exactly where each of them could be lost: the names could arrive
    unprefixed (and then no `mcp_*` rule reaches them), or arrive under a local name (and
    then the wrapper and the reduced budget are decided by the wrong door).
    """
    provider = build_tool_provider(TOOL_PACKAGES)
    profile = _shipped_profile()
    names = asyncio.run(provider.tool_names_for(profile))

    # 1. The SAME ToolPolicy. The flat list a turn hands `filter_toolset` is this one, and
    #    a single `mcp_*` rule reaches every third-party tool and no local one.
    rules = RuleSet.for_caller(
        _caller(),
        rules=(
            PolicyRule(
                rule_id="r-local",
                tool_pattern="routing_*",
                effect=Effect.ALLOW,
                reason="The vertical's own tools are ours.",
            ),
            PolicyRule(
                rule_id="r-mcp",
                tool_pattern="mcp_*",
                effect=Effect.DENY,
                reason="A third-party server is not trusted by default.",
            ),
        ),
    )
    for name in MCP_ROUTING_TOOLS:
        applicable = rules.applicable(name)
        assert applicable and applicable[0].rule_id == "r-mcp"
    assert rules.applicable("routing_estimate")[0].rule_id == "r-local"

    # 2. Untrusted-content wrapping. The runner decides by prefix, so the prefix must have
    #    survived into the name the policy and the audit record both see.
    for name in MCP_ROUTING_TOOLS:
        assert is_untrusted(name), f"{name} would not be wrapped"
    for name in LOCAL_DELIVERY_TOOLS:
        assert not is_untrusted(name)

    # 3. A SMALLER result budget, both the flat one the runner applies and the per-server
    #    number the profile declares.
    for name in MCP_ROUTING_TOOLS:
        assert budget_for(name) < budget_for("routing_estimate")
        assert mcp_toolsets.result_budget_for(profile, name) == 50_000
    assert mcp_toolsets.result_budget_for(profile, "routing_estimate") is None

    assert set(names) >= set(MCP_ROUTING_TOOLS) | set(LOCAL_DELIVERY_TOOLS)


@pytest.mark.phase("F11")
@pytest.mark.silent
def test_a_hostile_server_still_cannot_shadow_a_local_tool_after_composition() -> None:
    """t-f6-06's defence, re-asserted through the seam that could have lost it.

    The unit test proves `MCPToolProvider` prefixes. This one proves the provider
    `build_container` wires still does - a composition that handed the local toolset and
    the server toolset to the same namespace would pass every MCP test in the suite and
    silently let a third party answer to `echo`.
    """
    provider = build_tool_provider({"echo_vertical": _local_echo_builder()})
    profile = _shadowing_profile()
    shadowed = f"{mcp_toolsets.server_prefix('hostile')}_echo"

    names = asyncio.run(provider.tool_names_for(profile))
    assert "echo" in names, "the local tool must survive the composition"
    assert shadowed in names, "the server's tool must survive it too, under the prefix"

    async def scenario() -> None:
        toolset = await provider.toolset_for(profile)
        offered = await _offered(toolset)
        assert "echo" in offered
        assert shadowed in offered
        # The bare name still runs OUR tool. That is the whole claim.
        assert await _call(toolset, "echo", text="ping") == LOCAL_ECHO_RESULT
        # And the server is still reachable, only never as `echo`.
        assert await _call(toolset, shadowed, text="ping") == "ping"

    asyncio.run(scenario())


@pytest.mark.phase("F11")
def test_an_unreachable_server_degrades_the_profile_and_never_stops_the_process() -> None:
    """The documented answer to "refusal or degraded start?", in executable form.

    DEGRADED. A server that is down costs the profile that server's tools and nothing
    else: the local tools are untouched, `toolset_for` returns, and `preflight` reports a
    servable profile. A refusal here would mean any third party could stop this deployment
    booting by going offline.
    """
    provider = build_tool_provider({"echo_vertical": _local_echo_builder()})
    profile = _profile_with(_dead_server())

    names = asyncio.run(provider.tool_names_for(profile))
    assert names == ("echo",)

    offered = asyncio.run(_offered(asyncio.run(provider.toolset_for(profile))))
    assert offered == ("echo",)

    checks = asyncio.run(
        preflight._profile_checks({profile.id: profile}, {"echo_vertical": _local_echo_builder()})
    )
    assert [check for check in checks if check.severity == "fail"] == []


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _local_echo_builder() -> ToolsetBuilder:
    """A one-tool vertical named `echo` - the name the hostile server impersonates."""

    def build() -> FunctionToolset[None]:
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def echo(ctx: RunContext[None], text: str) -> str:
            """The vertical's OWN `echo`."""
            return LOCAL_ECHO_RESULT

        return toolset

    return build


def _profile_with(*servers: MCPServerRef) -> AgentProfile:
    return AgentProfile(
        id="composition_probe",
        persona="does not matter here",
        model="minimax/MiniMax-M3",
        toolsets=("echo_vertical",),
        mcp_servers=servers,
    )


def _shadowing_profile() -> AgentProfile:
    """Labelled `hostile`; the executable is the ordinary fixture server.

    What makes it hostile is not the code it runs but the name it claims.
    """
    return _profile_with(
        MCPServerRef(
            name="hostile",
            transport="stdio",
            command=sys.executable,
            args=(str(FIXTURE_SERVER),),
        )
    )


def _dead_server() -> MCPServerRef:
    """A server that exits before it says a single word."""
    return MCPServerRef(
        name="down",
        transport="stdio",
        command=sys.executable,
        args=("-c", "raise SystemExit(1)"),
    )


# --------------------------------------------------------------------------------------
# t-f11-30 - the allocation is the NUMBER; the suffix is a comment.
#
# `DuplicateMigrationIdError` compared whole ids, so `0023_audit_tool_calls_tenant` and
# `0023_anything_else` coexisted silently - the exact failure the pre-allocation table in
# docs/TASKS.md exists to prevent, one character narrower than the check guarding it. A
# reused number against a database that already recorded the first one is a migration that
# never runs, on that database only, forever.
#
# The duplicate is INJECTED rather than committed: writing a real second `0023_*` module
# into the package would break every other test in the suite by design.
# --------------------------------------------------------------------------------------


def _module_defining(name: str, migration: migrations.Migration) -> tuple[str, ModuleType]:
    module = ModuleType(name)
    module.__dict__["MIGRATION"] = migration
    return (name, module)


@pytest.mark.phase("F11")
def test_two_migrations_sharing_a_numeric_prefix_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different suffix, same allocation - and the message must name both ids."""
    first = migrations.Migration(id="0023_audit_tool_calls_tenant", sql="SELECT 1;")
    second = migrations.Migration(id="0023_something_else", sql="SELECT 2;")
    monkeypatch.setattr(
        migrations,
        "_iter_package_modules",
        lambda: [
            _module_defining("audit_tenant_migration", first),
            _module_defining("something_else_migration", second),
        ],
    )

    with pytest.raises(migrations.DuplicateMigrationIdError) as refused:
        migrations.discover_app_migrations()

    message = str(refused.value)
    assert "0023" in message
    assert first.id in message and second.id in message


@pytest.mark.phase("F11")
def test_the_same_migration_reached_through_two_modules_is_still_one_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module importing another's `Migration` is not a second allocation.

    The prefix comparison must not turn a re-export into a false alarm, or the fix for
    `t-f11-30` would take startup down over the convention it is defending.
    """
    shared = migrations.Migration(id="0023_audit_tool_calls_tenant", sql="SELECT 1;")
    monkeypatch.setattr(
        migrations,
        "_iter_package_modules",
        lambda: [
            _module_defining("audit_tenant_migration", shared),
            _module_defining("re_exporter", shared),
        ],
    )

    assert migrations.discover_app_migrations() == (shared,)


@pytest.mark.phase("F11")
def test_the_real_migration_package_allocates_every_number_once() -> None:
    """The check, run against the tree it guards. No two shipped ids share a number."""
    discovered = migrations.discover_app_migrations()
    numbers = [migration.id.split("_", 1)[0] for migration in discovered]
    assert len(numbers) == len(set(numbers)), f"a number is allocated twice: {sorted(numbers)}"
