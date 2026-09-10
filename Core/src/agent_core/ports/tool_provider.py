"""Port: ToolProvider - which tools exist for this profile?

Phase:      F0 (one toy tool) / F4 (first vertical) / F6 (MCP composed in)
Tasks:      docs/TASKS.md#t-f1-07
Adapter:    adapters/driven/tools/ + adapters/driven/mcp/
Per-vertical: YES - THIS IS ONE OF THE ONLY TWO PORTS THAT CHANGES

WHERE MCP LIVES - THE ANSWER TO "IS MCP A PORT?"
    No. MCP composes INSIDE this adapter. Pydantic AI ships `MCPToolset`, which wraps the
    FastMCP client and speaks stdio, Streamable HTTP and SSE. The adapter composes the
    vertical's local tools with whatever MCPToolsets the profile declares and returns one
    set.

    That is why the port count stayed at eleven and why only two ports change per
    vertical: skills and MCP enter as DATA AND COMPOSITION, not as new code.
    docs/DECISIONS.md#d10.

NON-NEGOTIABLE (CLAUDE.md #4)
    MCP tools pass through the SAME ToolPolicy as local ones. Their results are wrapped
    in untrusted-content delimiters, and they get a smaller result budget. An MCP server
    is third-party code returning text the model will read - the natural vector for
    indirect prompt injection.

    Tool NAMES from MCP are prefixed (`mcp_<server>_<tool>`) so a policy rule can match
    `mcp_*` and so a malicious server cannot shadow a local tool by naming itself
    `write_file`. Verify collision handling with a test that registers a hostile name.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.profile import AgentProfile


class ToolProvider(Protocol):
    def toolset_for(self, profile: AgentProfile) -> object:
        """PSEUDO-CODE - F1 local only, F6 adds MCP.

        1. Resolve each name in `profile.toolsets` to a registered local toolset.
           Unknown name -> raise. Silently ignoring a typo gives an agent that is
           mysteriously less capable, with nothing in the logs.
        2. For each `profile.mcp_servers`, build an MCPToolset, apply tool_include /
           tool_exclude, and prefix the names.
        3. Compose and return ONE object the AgentRunner adapter hands to Pydantic AI.

        MUST NOT auto-discover. Registration is explicit and opt-in.

        This is the single most important lesson from measuring Hermes: ONE line in its
        model_tools.py calls discover_builtin_tools(), which AST-scans tools/*.py and
        imports ~156 modules - and through three of them, the whole gateway. That single
        line is why its static import closure said 142 modules and reality was 396. An
        embedding host wants to register its own handful of tools, not inherit someone
        else's catalogue.
        """
        ...

    def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        """Flat list of names, for ToolPolicy.filter_toolset and for the audit record.

        Kept separate from `toolset_for` so policy filtering never needs to construct
        live MCP connections. Starting an stdio child process just to answer "what may
        this caller use" is slow and can fail for reasons that have nothing to do with
        policy.

        TODO(F6): back this with a persisted schema cache keyed by server name plus a
        fingerprint of the connection config, so names are known without spawning the
        server. Hermes does exactly this in tools/mcp_schema_cache.py.
        """
        ...
