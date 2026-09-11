"""MCP schema cache: policy filtering must not spawn a server to answer a name list.

Tasks: docs/TASKS.md#t-f6-05

WHY THIS TEST FAKES THE LAUNCHER INSTEAD OF SPAWNING A REAL SERVER
    t-f6-03's suite (test_mcp_toolsets.py) spawns the real stdio fixture to prove the
    transport-level properties. This task proves the OPPOSITE property: that discovery is
    skipped entirely on a cache hit. A real server can only prove "this call was fast" -
    it cannot prove "zero servers were spawned". So the test overrides
    `MCPToolProvider._mcp_toolset`, the one seam every discovery path already goes
    through, with a fake that records every call it receives. Zero recorded calls is the
    only way to prove zero spawns.

THE TWO PROPERTIES
    1. A cache hit answers `tool_names_for` without calling the launcher at all - policy
       filtering runs on every turn (CLAUDE.md), and a schema cache that still spawns a
       server on every turn is not a cache.
    2. A cache entry keyed to a server's connection config that has since changed (same
       server NAME, different command/args/url) is STALE and must be refreshed rather
       than served - serving a stale schema would offer the model tools a server no
       longer has, or hide ones it gained.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import agent_core.adapters.driven.mcp.toolsets as mcp_toolsets
from agent_core.domain.profile import AgentProfile, MCPServerRef


def _profile(*servers: MCPServerRef) -> AgentProfile:
    return AgentProfile(
        id="delivery_optimizer",
        persona="does not matter here",
        model="claude-sonnet-5",
        mcp_servers=servers,
    )


def _server(**overrides: Any) -> MCPServerRef:
    kwargs: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "command": "fake-launcher",
        "args": ("v1",),
    }
    kwargs.update(overrides)
    return MCPServerRef(**kwargs)


@dataclass
class _Advertised:
    """Stands in for whatever `MCPToolset.list_tools()` returns - only `.name` is used."""

    name: str


@dataclass
class _FakeToolset:
    tool_names: tuple[str, ...]

    async def list_tools(self) -> list[_Advertised]:
        return [_Advertised(name=n) for n in self.tool_names]


class _FakeLauncherProvider(mcp_toolsets.MCPToolProvider):
    """A real `MCPToolProvider`, except `_mcp_toolset` never spawns anything.

    `calls` records every server it was asked to launch, in order, by server name. This
    IS the launcher a real server would sit behind - the fake replaces exactly the seam
    `_names_for_server` calls to reach the network/process boundary, nothing above it.
    """

    def __init__(self, responses: dict[tuple[str, ...], tuple[str, ...]]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[str] = []

    def _mcp_toolset(self, server: MCPServerRef) -> Any:
        self.calls.append(server.name)
        return _FakeToolset(self.responses[server.args])


def test_a_cache_hit_answers_tool_names_for_without_spawning_any_server() -> None:
    provider = _FakeLauncherProvider(responses={("v1",): ("echo", "add")})
    profile = _profile(_server(args=("v1",)))

    first = asyncio.run(provider.tool_names_for(profile))
    assert first == ("mcp_fixture_add", "mcp_fixture_echo")
    assert provider.calls == ["fixture"], "cold cache: exactly one launch to fill it"

    provider.calls.clear()
    second = asyncio.run(provider.tool_names_for(profile))
    assert second == ("mcp_fixture_add", "mcp_fixture_echo")
    assert provider.calls == [], "warm cache: zero servers spawned to answer the same profile"


def test_a_stale_cache_entry_is_refreshed_rather_than_served() -> None:
    """Same server NAME, different connection config -> the old entry must not be served."""
    provider = _FakeLauncherProvider(
        responses={("v1",): ("echo",), ("v2",): ("echo", "add")}
    )

    first = asyncio.run(provider.tool_names_for(_profile(_server(args=("v1",)))))
    assert first == ("mcp_fixture_echo",)
    assert provider.calls == ["fixture"]

    # The profile now points the same server at a different command/args - the cached
    # schema for "fixture" no longer describes what will actually answer.
    second = asyncio.run(provider.tool_names_for(_profile(_server(args=("v2",)))))
    assert second == ("mcp_fixture_add", "mcp_fixture_echo")
    assert provider.calls == ["fixture", "fixture"], "stale entry refreshed, not served"


def test_tool_include_exclude_are_re_applied_to_a_cached_entry() -> None:
    """Filtering is not part of the cached fingerprint: it is re-applied on every read."""
    provider = _FakeLauncherProvider(responses={("v1",): ("echo", "add", "hang")})
    profile = _profile(_server(args=("v1",), tool_exclude=("han*",)))

    first = asyncio.run(provider.tool_names_for(profile))
    assert first == ("mcp_fixture_add", "mcp_fixture_echo")
    assert provider.calls == ["fixture"]

    provider.calls.clear()
    same_server_different_filter = _profile(_server(args=("v1",), tool_include=("ech*",)))
    second = asyncio.run(provider.tool_names_for(same_server_different_filter))
    assert second == ("mcp_fixture_echo",)
    assert provider.calls == [], "still a hit: include/exclude changed, connection config did not"
