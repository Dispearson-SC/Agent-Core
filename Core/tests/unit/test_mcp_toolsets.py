"""MCP servers composed into the ToolProvider: name prefixing, and per-call failure.

Tasks: docs/TASKS.md#t-f6-03

WHY THIS DRIVES A REAL STDIO SERVER
    Every interesting property of this adapter lives in the transport. A mocked transport
    would answer whatever the mock was told to answer, so a test built on one asserts that
    the test author remembered the prefix - not that the adapter applies it. The suite
    therefore spawns `tests/fixtures/mcp_echo_server.py` as a child process and talks MCP
    to it.

THE TWO PROPERTIES
    PREFIXING is a security control, not cosmetics (CLAUDE.md non-negotiable #4). Every
    tool a server advertises must reach the model as `mcp_<server>_<tool>`, so a policy
    rule can match `mcp_*` and so a hostile server cannot shadow a local tool by naming
    itself after one. The assertion is over EVERY name the server offers, because a
    prefixer that misses one tool is a prefixer that does not work.

    PER-CALL FAILURE is the one that does not fail on its own. MCP is the flakiest
    dependency in the system: servers hang and die. A hung call must fail that call and
    leave the turn alive, so `test_a_hung_server_fails_that_one_call_and_not_the_turn`
    asserts both halves - the wedged tool raises, and the very next call to a healthy
    tool on the SAME server still answers. Without the second half, a toolset that tears
    down the whole connection on a timeout passes.

The module is imported as a module, matching tests/unit/test_skills_fs.py, so a name that
does not exist yet fails inside the test that needs it rather than at COLLECTION.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.usage import RunUsage

import agent_core.adapters.driven.mcp.toolsets as mcp_toolsets
from agent_core.domain.profile import AgentProfile, MCPServerRef

FIXTURE_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_echo_server.py"

# Everything the fixture server advertises, prefixed. `hang` is in the list on purpose:
# a tool that will never answer is still a tool the model must be told about.
EVERY_FIXTURE_TOOL = ("mcp_fixture_add", "mcp_fixture_echo", "mcp_fixture_hang")


def _profile(*servers: MCPServerRef) -> AgentProfile:
    return AgentProfile(
        id="delivery_optimizer",
        persona="does not matter here",
        model="claude-sonnet-5",
        mcp_servers=servers,
    )


def _fixture_server(**overrides: Any) -> MCPServerRef:
    kwargs: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "command": sys.executable,
        "args": (str(FIXTURE_SERVER),),
    }
    kwargs.update(overrides)
    return MCPServerRef(**kwargs)


def _dead_server() -> MCPServerRef:
    """A server that exits before it says a single word."""
    return MCPServerRef(
        name="dead",
        transport="stdio",
        command=sys.executable,
        args=("-c", "raise SystemExit(1)"),
    )


def _run_context() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _names_in(toolset: object) -> tuple[str, ...]:
    """The names the model would actually be offered by whatever `toolset_for` returned.

    `ToolProvider.toolset_for` is typed `-> object` on purpose (the port names no Pydantic
    AI type), so the cast back to a toolset is asserted here rather than assumed.
    """
    assert isinstance(toolset, AbstractToolset)
    tools: dict[str, ToolsetTool[None]] = await toolset.get_tools(_run_context())
    return tuple(sorted(tools))


async def _call(toolset: AbstractToolset[None], name: str, **args: Any) -> Any:
    ctx = _run_context()
    tools: dict[str, ToolsetTool[None]] = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


def test_every_mcp_tool_is_exposed_as_mcp_server_tool() -> None:
    """`mcp_<server>_<tool>`, for every tool, in both answers the port gives.

    `tool_names_for` and `toolset_for` are separate methods for a reason (policy
    filtering must not need a live connection), which means they are two chances to get
    the prefix wrong. Both are pinned to the same list.
    """
    provider = mcp_toolsets.MCPToolProvider()
    profile = _profile(_fixture_server())

    names = asyncio.run(provider.tool_names_for(profile))
    assert names == EVERY_FIXTURE_TOOL

    toolset = asyncio.run(provider.toolset_for(profile))
    assert asyncio.run(_names_in(toolset)) == EVERY_FIXTURE_TOOL


@pytest.mark.silent
def test_a_hung_server_fails_that_one_call_and_not_the_turn() -> None:
    """The wedged tool raises after the bound; the next tool on that server answers.

    Silent-bug area: a toolset that drops the connection on a timeout passes the first
    assertion and fails the second, and nothing else in the suite would notice.
    """
    provider = mcp_toolsets.MCPToolProvider(call_timeout_seconds=2.0)

    async def scenario() -> str:
        # ONE event loop, because this is one turn: the whole claim is that the second
        # call happens after the first one gave up, on the same live connection.
        toolset = await provider.toolset_for(_profile(_fixture_server()))
        assert isinstance(toolset, AbstractToolset)
        with pytest.raises(ModelRetry) as raised:
            await _call(toolset, "mcp_fixture_hang")
        assert "mcp_fixture_hang" in str(raised.value)
        answer = await _call(toolset, "mcp_fixture_echo", text="still here")
        assert isinstance(answer, str)
        return answer

    assert asyncio.run(scenario()) == "still here"


def test_a_dead_server_does_not_block_turn_start() -> None:
    """One server that cannot start must not cost the profile its other tools."""
    provider = mcp_toolsets.MCPToolProvider(discovery_timeout_seconds=10.0)
    profile = _profile(_dead_server(), _fixture_server())

    assert asyncio.run(provider.tool_names_for(profile)) == EVERY_FIXTURE_TOOL
    toolset = asyncio.run(provider.toolset_for(profile))
    assert asyncio.run(_names_in(toolset)) == EVERY_FIXTURE_TOOL


def test_tool_exclude_reduces_scope_before_the_model_sees_the_name() -> None:
    """`tool_include` / `tool_exclude` are trailing-wildcard patterns, applied at discovery."""
    provider = mcp_toolsets.MCPToolProvider()
    profile = _profile(_fixture_server(tool_exclude=("han*",)))

    assert asyncio.run(provider.tool_names_for(profile)) == (
        "mcp_fixture_add",
        "mcp_fixture_echo",
    )
    toolset = asyncio.run(provider.toolset_for(profile))
    assert asyncio.run(_names_in(toolset)) == ("mcp_fixture_add", "mcp_fixture_echo")


def test_local_tools_keep_their_names_and_compose_with_mcp() -> None:
    """MCP composes INSIDE ToolProvider; the local names are not prefixed."""
    local_toolset: FunctionToolset[None] = FunctionToolset()

    @local_toolset.tool
    def quote_price(ctx: RunContext[None], sku: str) -> str:
        return f"price of {sku}"

    class _LocalProvider:
        async def toolset_for(self, profile: AgentProfile) -> object:
            return local_toolset

        async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
            return ("quote_price",)

    provider = mcp_toolsets.MCPToolProvider(local=_LocalProvider())
    profile = _profile(_fixture_server())

    assert asyncio.run(provider.tool_names_for(profile)) == ("quote_price", *EVERY_FIXTURE_TOOL)
    toolset = asyncio.run(provider.toolset_for(profile))
    assert asyncio.run(_names_in(toolset)) == tuple(sorted(("quote_price", *EVERY_FIXTURE_TOOL)))


def test_the_mcp_result_budget_is_smaller_than_the_local_one() -> None:
    """Non-negotiable #4, third part: `mcp_*` gets a smaller budget than a local tool.

    The wrapping itself is docs/TASKS.md#t-f6-04, in the runner. What lives here is the
    only thing that can answer WHICH budget a prefixed name carries, because only this
    module knows which server a prefixed name came from.
    """
    profile = _profile(_fixture_server(result_budget_chars=12_345))

    assert mcp_toolsets.result_budget_for(profile, "mcp_fixture_echo") == 12_345
    assert mcp_toolsets.result_budget_for(profile, "quote_price") is None
