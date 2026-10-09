"""Driven adapter: the repository's OWN stdio MCP server, so a fresh clone has one.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-39
Tests:   Core/tests/integration/test_mcp_profile.py,
         Core/tests/integration/test_mcp_composition.py,
         Core/tests/integration/test_cli_acceptance.py
Run as:  python -m agent_core.adapters.driven.mcp.reference_server

WHY A PRODUCT FILE AND NOT THE TEST FIXTURE IT REPLACES
    `Core/profiles/delivery_optimizer.yaml` declared its `routing` server as
    `python Core/tests/fixtures/mcp_echo_server.py`. That is two defects in one line.

    It shipped a TEST as production configuration. A file whose first paragraph is "a
    profile is DATA - read top to bottom, this file answers what this agent is allowed to
    do without opening any code" taught every operator who read it to point a deployment
    at `tests/`. Configuration is the one surface a reviewer reads instead of the code, so
    a bad example there is copied rather than caught.

    And the path was relative to the WORKING DIRECTORY. A stdio server is spawned with the
    parent's cwd, and a profile file cannot know what that is: from the repository root it
    resolved, and from `Core/src` - which is where `python -m agent_core` resolves the
    package from - python answered `can't open file
    '...\\Core\\src\\Core\\tests\\fixtures\\mcp_echo_server.py'`. The shipped profile
    worked in exactly one directory, and not the one the shipped process starts in.

    Both go away together: this module belongs to the product, and `-m` addresses it by
    IMPORT PATH, which resolves from wherever the package is importable and therefore from
    anywhere the process can start at all. docs/TASKS.md#t-f11-39.

WHAT IT IS FOR, WHICH IS NOT "PRETENDING TO BE A TRAFFIC SERVICE"
    It is a diagnostic. Everything between a profile's `mcp_servers:` block and a model
    seeing `mcp_<server>_<tool>` - the transport, the handshake, the schema cache, the
    prefix, `ToolPolicy`, the untrusted wrapper, the reduced result budget - is machinery
    an operator has no way to exercise without SOME server to point at, and requiring a
    third-party one to find out whether the local half works is the failure ladder again.
    So a fresh clone can run the shipped `delivery_optimizer` profile end to end with no
    network, no credentials and nothing installed, and see the whole path light up.

    `echo` and `add` are the canonical MCP smoke tools: one proves bytes come back from a
    real child process unchanged, the other proves a typed schema survived discovery.

WHY IT SHIPS A TOOL THAT NEVER ANSWERS
    `hang` is deliberately wedged, and it is the only reason the shipped profile's
    `tool_exclude: ["hang"]` teaches anything - an example scope reducer with nothing to
    reduce is decoration. It also exercises the two bounds that only matter when a server
    misbehaves: `_GuardedToolset.call_tool`'s timeout, which fails ONE tool call and
    leaves the turn alive, and discovery's bound, which never lets a server block turn
    start. Neither can be demonstrated against a server that always behaves.

    It is not a hazard an agent can reach by accident: the profile excludes it at
    discovery, and `ToolPolicy` - never the exclude list - is the boundary that decides
    what a caller may call (CLAUDE.md non-negotiable #4). Nothing here reads a file, opens
    a socket, or touches this deployment's state; a reference server that could would be a
    third-party server with our privileges.

POINT IT AT THE REAL THING AND NOTHING ELSE MOVES
    That is F11's claim. `command` and `args` in the profile are the whole of it - there
    is no code change, no registration, and no import anywhere that names this module. It
    is reachable only because a YAML file says so, which is exactly the property being
    demonstrated.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

__all__ = ["HANG_SECONDS", "SERVER_NAME", "server"]

SERVER_NAME = "agent-core-reference"
"""What this server calls ITSELF, which is not what a profile calls it.

The profile's `name:` decides the `mcp_<name>_` prefix every tool arrives under, so the
shipped profile's server is `routing` and these tools are `mcp_routing_*`. A server does
not get to choose the namespace it lands in - that is the point of the prefix.
"""

HANG_SECONDS = 3600.0
"""Far longer than any caller waits, and finite rather than `while True`.

The client gives up first - that is the behaviour under test - so this number is only
ever the upper bound on how long an abandoned child process lingers.
"""

server: MCPServer = MCPServer(SERVER_NAME)


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
async def hang() -> str:
    """Never answer within the lifetime of a call. See WHY IT SHIPS A TOOL THAT NEVER
    ANSWERS in the module docstring; the shipped profile excludes this at discovery."""
    await asyncio.sleep(HANG_SECONDS)
    return "unreachable"


if __name__ == "__main__":  # pragma: no cover - spawned as a child process, over stdio
    server.run()
