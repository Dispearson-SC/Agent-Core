"""Driven adapter: MCP servers composed into the toolset.

Phase:   F6
Tasks:   docs/TASKS.md#t-f6-03
Implements: part of ports/tool_provider.py - MCP IS NOT ITS OWN PORT

WHY THIS IS NOT A PORT
    Pydantic AI ships `MCPToolset`, which wraps the FastMCP client and speaks stdio,
    Streamable HTTP and SSE. This adapter composes those with the vertical's local tools
    and hands back one set. That is why the port count stayed at eleven and only two ports
    change per vertical. docs/DECISIONS.md#d10.

NON-NEGOTIABLE #4 - THREE PARTS, ALL REQUIRED
    1. MCP tools pass through the SAME ToolPolicy as local tools.
    2. Their results are wrapped in untrusted-content delimiters.
    3. They get a smaller result budget (50K vs 100K chars). Hermes' stated reason: MCP
       servers routinely return un-paginated 20-50K payloads.

NAME PREFIXING IS A SECURITY CONTROL, NOT COSMETICS
    `mcp_<server>_<tool>`. It lets a policy rule match `mcp_*`, and it stops a malicious
    server shadowing a local tool by naming itself `write_file`. Write a test that
    registers a hostile server advertising a local tool name and assert the local one wins.

FAILURE HANDLING - MCP IS THE FLAKIEST DEPENDENCY IN THE SYSTEM
    Servers hang, die, and return garbage. Bound every call with a timeout, fail the tool
    call rather than the turn, and never let a dead server block turn start.

    TODO(F6): persist a schema cache keyed by server name + a fingerprint of the connection
    config, so tool NAMES are known without spawning stdio children. Policy filtering must
    not need a live connection. Hermes does this in tools/mcp_schema_cache.py.
"""
