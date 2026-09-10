"""A minimal stdio MCP server, spawned as a child process by the MCP toolset tests.

Tasks: docs/TASKS.md#t-f6-03

WHY A REAL SERVER AND NOT A MOCKED TRANSPORT
    Mocking the transport tests the mock. Name prefixing, discovery and per-call failure
    handling only mean anything against something that actually speaks MCP over stdio,
    so this module is run as `python mcp_echo_server.py`.

    `hang` sleeps far longer than any test could wait, on purpose: it is how the suite
    proves that a wedged server fails ONE tool call instead of the whole turn. It never
    sleeps for the full hour, because the client gives up on it first.

    `MCPServer` is the mcp 2.x name for what used to be `FastMCP`. The `fastmcp` package
    installed here is the slim client-only build and cannot host a server, so the fixture
    uses the SDK directly.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

server: MCPServer = MCPServer("fixture")


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
    """Never answer within the lifetime of a test."""
    await asyncio.sleep(3600)
    return "unreachable"


if __name__ == "__main__":
    server.run()
