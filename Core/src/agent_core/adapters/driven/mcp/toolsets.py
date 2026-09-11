"""Driven adapter: MCP servers composed into the toolset.

Phase:   F6
Tasks:   docs/TASKS.md#t-f6-03, docs/TASKS.md#t-f6-05
Status:  IMPLEMENTED (t-f6-03, t-f6-05).
Implements: part of ports/tool_provider.py - MCP IS NOT ITS OWN PORT

WHY THIS IS NOT A PORT
    Pydantic AI ships `MCPToolset`, which wraps the FastMCP client and speaks stdio,
    Streamable HTTP and SSE. This adapter composes those with the vertical's local tools
    and hands back one set. That is why only two ports change per vertical.
    docs/DECISIONS.md#d10.

NON-NEGOTIABLE #4 - THREE PARTS, ALL REQUIRED
    1. MCP tools pass through the SAME ToolPolicy as local tools. Nothing here filters by
       caller: `tool_names_for` hands the flat prefixed list to `ToolPolicy`, exactly as
       the local provider does, and the enforcement point stays the one in the runner.
    2. Their results are wrapped in untrusted-content delimiters - the runner does that
       (docs/TASKS.md#t-f6-04). What this module owns is `result_budget_for`, because
       only this module can say which server a prefixed name came from.
    3. They get a smaller result budget. That number is per-server profile data
       (`MCPServerRef.result_budget_chars`, 50K against 100K for local) and Hermes' stated
       reason is that MCP servers routinely return un-paginated 20-50K payloads.

NAME PREFIXING IS A SECURITY CONTROL, NOT COSMETICS
    `mcp_<server>_<tool>`. It lets a policy rule match `mcp_*`, and it stops a malicious
    server shadowing a local tool by naming itself `write_file`. The prefix is applied by
    Pydantic AI's `PrefixedToolset`, which also strips it again on the way into
    `call_tool`, so the server is still asked for the name IT advertised. The hostile
    shadowing test is docs/TASKS.md#t-f6-06.

    `tool_include` / `tool_exclude` are trailing-wildcard patterns applied at DISCOVERY.
    They reduce scope; they are not the security boundary. `ToolPolicy` is.

FAILURE HANDLING - MCP IS THE FLAKIEST DEPENDENCY IN THE SYSTEM
    Servers hang, die, and return garbage. Three separate bounds, because they fail at
    three separate moments:

      - `init_timeout` on the `MCPToolset` bounds the handshake, so a server that never
        finishes starting cannot hold the turn open.
      - `_GuardedToolset.get_tools` bounds discovery and, on ANY failure, contributes an
        empty toolset. A dead server costs the profile that server's tools and nothing
        else - it never blocks turn start.
      - `_GuardedToolset.call_tool` bounds the call and raises `ModelRetry`, which is the
        Pydantic AI signal for "this tool call failed, keep going". The turn survives and
        the model is told what happened. Raising anything else here ends the run.

    Swallowing discovery failures is deliberate and is the one place this module chooses
    silence over noise, so it logs at WARNING: an operator has to be able to tell "this
    server offers no tools" from "this server is down".

SCHEMA CACHE (t-f6-05) - WHAT INVALIDATES AN ENTRY
    Policy filtering (`ToolPolicy.filter_toolset`) runs on every turn via `tool_names_for`.
    Without a cache that means every turn pays a process launch - or an HTTP handshake -
    just to ask a question whose answer has not changed since the last turn. `_SchemaCache`
    remembers, per server NAME, the raw (unprefixed, unfiltered) tool names it last saw
    plus a fingerprint of the connection config that produced them.

    An entry is served on a cache HIT: same server name, same fingerprint. It is treated
    as MISSING and refreshed - one fresh discovery call, then re-cached - in exactly two
    cases:
      1. Nothing is cached yet for that server name.
      2. The fingerprint (transport + command + args + url) no longer matches: the profile
         now points that server name at a different process or endpoint, so the schema on
         file describes something that will not actually answer.

    `tool_include` / `tool_exclude` do NOT invalidate the entry and are not part of the
    fingerprint: they are scope filters applied to the cached raw names on every read
    (see `_is_included`), so changing them costs nothing and never triggers a spawn.

    There is no time-based expiry. A schema cache with a TTL still spawns a server on a
    schedule nobody asked for; this one is only as stale as the connection config it was
    built from.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

# The three transports are re-exported by `pydantic_ai.mcp`, but only as implicit
# re-exports, which strict mypy rejects. They are imported from the module that defines
# them - the same objects, and one fewer indirection to be surprised by.
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, ToolsetTool, WrapperToolset

from agent_core.domain.profile import AgentProfile, MCPServerRef
from agent_core.ports.tool_provider import ToolProvider

_log = logging.getLogger(__name__)

PREFIX = "mcp"
"""The first segment of `mcp_<server>_<tool>`, and what a `mcp_*` policy rule matches."""

DEFAULT_CALL_TIMEOUT_SECONDS = 30.0
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 10.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


class MCPConfigurationError(ValueError):
    """A profile declared an MCP server this adapter cannot build.

    Raised rather than skipped. A silently ignored server is an agent that is
    mysteriously less capable with nothing in the logs - the same failure mode
    ports/tool_provider.py refuses for an unknown local toolset name.
    """


def server_prefix(server_name: str) -> str:
    """`mcp_<server>`, the prefix every tool of that server carries."""
    return f"{PREFIX}_{server_name}"


def result_budget_for(profile: AgentProfile, tool_name: str) -> int | None:
    """The result budget for a prefixed MCP tool name, or None when it is not one.

    None means "not an MCP tool", not "no budget": a local tool keeps the local budget,
    which is the runner's business. Longest prefix wins, so a server named `files` and a
    server named `files_ro` cannot be confused with each other.
    """
    best: MCPServerRef | None = None
    for server in profile.mcp_servers:
        prefix = f"{server_prefix(server.name)}_"
        if tool_name.startswith(prefix) and (best is None or len(server.name) > len(best.name)):
            best = server
    return None if best is None else best.result_budget_chars


def _matches(patterns: tuple[str, ...], name: str) -> bool:
    """Trailing-wildcard match, the only pattern shape `MCPServerRef` promises."""
    return any(
        name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern
        for pattern in patterns
    )


def _fingerprint(server: MCPServerRef) -> tuple[str, str, tuple[str, ...], str]:
    """What a cached schema is valid FOR. Changing any of this invalidates the entry.

    Deliberately excludes `tool_include`/`tool_exclude` and `result_budget_chars`: those
    do not change what the server advertises, only what this adapter does with the
    advertisement, so they must never force a re-discovery.
    """
    return (server.transport, server.command or "", server.args, server.url or "")


@dataclass
class _SchemaCacheEntry:
    fingerprint: tuple[str, str, tuple[str, ...], str]
    raw_tool_names: tuple[str, ...]


class _SchemaCache:
    """Per-server-name cache of raw (unprefixed) advertised tool names.

    See the module docstring's "SCHEMA CACHE" section for the invalidation rule. Lives on
    the `MCPToolProvider` instance, not at module scope: a provider is composed once
    (F1's `composition.py`) and reused across turns, which is exactly the lifetime this
    cache needs - and it means two providers in the same test never share state.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _SchemaCacheEntry] = {}

    def get(self, server: MCPServerRef) -> tuple[str, ...] | None:
        entry = self._entries.get(server.name)
        if entry is None or entry.fingerprint != _fingerprint(server):
            return None
        return entry.raw_tool_names

    def put(self, server: MCPServerRef, raw_tool_names: tuple[str, ...]) -> None:
        self._entries[server.name] = _SchemaCacheEntry(_fingerprint(server), raw_tool_names)


def _is_included(server: MCPServerRef, tool_name: str) -> bool:
    if server.tool_include and not _matches(server.tool_include, tool_name):
        return False
    return not _matches(server.tool_exclude, tool_name)


def _transport(server: MCPServerRef) -> StdioTransport | StreamableHttpTransport | SSETransport:
    if server.transport == "stdio":
        if not server.command:
            raise MCPConfigurationError(f"MCP server {server.name!r}: stdio needs a command.")
        return StdioTransport(server.command, list(server.args))
    if server.transport in ("http", "sse"):
        if not server.url:
            raise MCPConfigurationError(
                f"MCP server {server.name!r}: {server.transport} needs a url."
            )
        if server.transport == "http":
            return StreamableHttpTransport(server.url)
        return SSETransport(server.url)
    raise MCPConfigurationError(
        f"MCP server {server.name!r}: unknown transport {server.transport!r}; "
        "expected stdio, http or sse."
    )


@dataclass
class _GuardedToolset(WrapperToolset[Any]):
    """Bounds one MCP server so its failures stay its own.

    Wraps the PREFIXED toolset rather than the raw one, so the names in the timeout
    message are the names the model actually used.
    """

    server_name: str
    call_timeout_seconds: float
    discovery_timeout_seconds: float

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        try:
            async with asyncio.timeout(self.discovery_timeout_seconds):
                return await super().get_tools(ctx)
        except Exception:
            # Never let a dead server block turn start. The profile loses this server's
            # tools; every other toolset in the composition is untouched.
            _log.warning(
                "MCP server %r could not be listed; continuing without its tools.",
                self.server_name,
                exc_info=True,
            )
            return {}

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        try:
            async with asyncio.timeout(self.call_timeout_seconds):
                return await super().call_tool(name, tool_args, ctx, tool)
        except TimeoutError as exc:
            # ModelRetry, not a bare raise: this fails the tool call and leaves the turn
            # alive, which is the whole point of bounding the call.
            raise ModelRetry(
                f"MCP server {self.server_name!r} did not answer {name!r} within "
                f"{self.call_timeout_seconds:g}s. The call was abandoned; the tool may be "
                "unavailable."
            ) from exc


class MCPToolProvider:
    """`ToolProvider` that composes a local provider with the profile's MCP servers.

    `local` is the vertical's own provider (F4). It is optional because MCP is useful on
    its own and because F1's local adapter is not written yet; when it is absent the
    profile simply has whatever its servers offer. Local names are NOT prefixed - the
    prefix exists to mark tools that came from third-party code.
    """

    def __init__(
        self,
        local: ToolProvider | None = None,
        *,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._local = local
        self._call_timeout_seconds = call_timeout_seconds
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._schema_cache = _SchemaCache()

    async def toolset_for(self, profile: AgentProfile) -> object:
        """One `AbstractToolset` carrying the local tools plus every declared server."""
        toolsets: list[AbstractToolset[Any]] = []
        if self._local is not None:
            local = await self._local.toolset_for(profile)
            if not isinstance(local, AbstractToolset):
                raise MCPConfigurationError(
                    f"The local ToolProvider returned {type(local).__name__}; this adapter "
                    "composes Pydantic AI AbstractToolset values."
                )
            toolsets.append(local)
        toolsets.extend(self._guarded(server) for server in profile.mcp_servers)
        return CombinedToolset(toolsets)

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        """Flat prefixed names, for `ToolPolicy.filter_toolset` and the audit record.

        Local names come first and in the local provider's own order; MCP names follow in
        profile order, sorted within a server. Sorted because a server is free to change
        the order it lists tools in, and an audit record that reshuffles between two
        identical turns is one nobody can diff.

        Backed by `_SchemaCache` (t-f6-05): a server is only ever spawned to fill or
        refresh a cache entry, never merely to answer this method. See the module
        docstring's "SCHEMA CACHE" section for exactly what invalidates an entry.
        """
        names: list[str] = []
        if self._local is not None:
            names.extend(await self._local.tool_names_for(profile))
        for server in profile.mcp_servers:
            names.extend(await self._names_for_server(server))
        return tuple(names)

    async def _names_for_server(self, server: MCPServerRef) -> tuple[str, ...]:
        """Prefixed, filtered names for one server - a cache hit spawns nothing.

        `tool_include`/`tool_exclude` are applied here, AFTER the cache lookup/fill, so
        that changing them never counts as a cache miss (see `_fingerprint`).
        """
        raw = self._schema_cache.get(server)
        if raw is None:
            raw = await self._discover_raw_names(server)
            if raw is not None:
                self._schema_cache.put(server, raw)
        if raw is None:
            return ()
        prefix = server_prefix(server.name)
        return tuple(f"{prefix}_{name}" for name in raw if _is_included(server, name))

    async def _discover_raw_names(self, server: MCPServerRef) -> tuple[str, ...] | None:
        """The one place that actually spawns/connects to fill or refresh the cache.

        Returns `None` on ANY failure rather than caching an empty result - a dead server
        must be retried next turn, not permanently remembered as "offers nothing".
        """
        try:
            async with asyncio.timeout(self._discovery_timeout_seconds):
                advertised = await self._mcp_toolset(server).list_tools()
        except Exception:
            # Same bargain as `_GuardedToolset.get_tools`: policy filtering must not fail
            # because a server is down, so an unreachable server contributes no names.
            _log.warning(
                "MCP server %r could not be listed; continuing without its tools.",
                server.name,
                exc_info=True,
            )
            return None
        return tuple(sorted(tool.name for tool in advertised))

    def _mcp_toolset(self, server: MCPServerRef) -> MCPToolset[Any]:
        return MCPToolset(
            _transport(server),
            # `id` is what identifies this toolset's steps inside a durable workflow (F2).
            # It has to be stable across replays, so it is derived from the server name
            # and never from anything generated at construction time.
            id=server_prefix(server.name),
            init_timeout=self._connect_timeout_seconds,
        )

    def _guarded(self, server: MCPServerRef) -> AbstractToolset[Any]:
        toolset: AbstractToolset[Any] = self._mcp_toolset(server)
        if server.tool_include or server.tool_exclude:
            toolset = toolset.filtered(
                lambda _ctx, tool_def, _server=server: _is_included(_server, tool_def.name)  # type: ignore[misc]
            )
        return _GuardedToolset(
            wrapped=toolset.prefixed(server_prefix(server.name)),
            server_name=server.name,
            call_timeout_seconds=self._call_timeout_seconds,
            discovery_timeout_seconds=self._discovery_timeout_seconds,
        )


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_DISCOVERY_TIMEOUT_SECONDS",
    "PREFIX",
    "MCPConfigurationError",
    "MCPToolProvider",
    "result_budget_for",
    "server_prefix",
]
