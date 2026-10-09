"""Adapter: the local `ToolProvider` - a profile's names resolved against registrations.

Phase:      F1 (local only) / F6 (MCP composes in here)
Tasks:      docs/TASKS.md#t-f1-21
Implements: ports/tool_provider.py (docs/TASKS.md#t-f1-07)
Per-vertical: the REGISTRY below is one of the two things a vertical touches. The
              resolution logic is not.

WHY THIS FILE EXISTS AT ALL
    `t-f1-07` froze the port and no anchor ever claimed the adapter, so the only
    implementation in the repository was `_DeliveryToolProvider` in
    tests/integration/test_f0_end_to_end.py - whose own docstring calls it "a TEST double,
    not an adapter". A frozen port with no adapter is not a finished port, and a test
    double standing in for the missing one is exactly what hides that.

NO AUTO-DISCOVERY - THE ONE RULE THIS FILE IS BUILT AROUND
    A profile NAMES its toolsets. This adapter resolves those names against packages that
    were REGISTERED explicitly, and refuses an unknown name loudly. It never scans
    `adapters/driven/tools/` for subpackages, never walks module namespaces, and never
    imports a vertical it was not handed.

    The cost of the alternative is measured, not theoretical: one `discover_builtin_tools()`
    call in Hermes AST-scans `tools/*.py` and turns a handful of intended tools into a ~396
    module import closure (ports/tool_provider.py). The security half is worse than the
    import half - a file dropped into a directory would become a capability the agent has
    and nobody granted.

    `tools/delivery/tools.py` holds the same rule one level down, in its literal list of
    four functions. Both halves are needed: a disciplined provider composing a package that
    discovers its own contents is still auto-discovery.

WHY THE NAMES ARE READ OFF THE TOOLSET AND NOT DECLARED AT REGISTRATION
    Registering `("delivery", build_toolset, ("routing_estimate", ...))` would state the
    tool names twice - here and in the vertical - and a duplicated fact drifts (CLAUDE.md,
    Conventions). The drift would be silent and one-directional: `tool_names_for` feeds
    `ToolPolicy.filter_toolset` and the audit record, so a name missing from the declared
    tuple is a tool the policy never narrowed and the audit never expected, while the model
    was still offered it.

    So a registration is a NAME and a BUILDER, and the names come from what the builder
    built. `FunctionToolset.tools` is that mapping, keyed by tool name in registration
    order. F6's persisted schema cache (ports/tool_provider.py) replaces this for MCP
    servers, which cannot be listed without either a cache or a spawned process - which is
    why the port keeps the two methods separate in the first place.

HOW MCP COMPOSES IN HERE - t-f11-28
    MCP composes INSIDE this adapter (docs/DECISIONS.md#d10), and now actually does.
    `LocalToolProvider` takes an optional `mcp` collaborator - the F6 adapter,
    `adapters/driven/mcp/toolsets.py` - and hands it the profile's `mcp_servers`. That
    adapter owns everything third-party about them: the transports, the
    `mcp_<server>_<tool>` prefix, `tool_include`/`tool_exclude`, the schema cache, and the
    per-server timeouts. This file owns only the JOIN: local names first in profile order,
    then the servers', in one flat list and one composed toolset. Nothing above this file
    changed when that landed - which is what "only two ports change per vertical" means in
    practice.

    The MCP half is composed the same way for both port methods, because they are two
    resolution paths answering one question. `tool_names_for` reads the schema cache and
    therefore never spawns a server merely to let `ToolPolicy` narrow a list.

    `build_tool_provider` ALWAYS supplies that collaborator. A bare `LocalToolProvider(...)`
    still refuses a profile that declares a server, with `MCPNotComposedYetError`: a
    capability the profile granted and the agent never received is the failure mode that
    produces "the agent is mysteriously less capable, with nothing in the logs" - the same
    sentence the port uses about swallowing an unknown name. Composed or refused; never
    silently smaller.

    THIS IS STILL NOT AUTO-DISCOVERY. A profile NAMES its servers, exactly as it names its
    toolsets (`t-f1-07` froze the port that way). Nothing here scans a directory of server
    definitions, and an `mcp_servers` entry this adapter cannot build is `MCPConfigurationError`
    from the F6 adapter rather than a skipped line.

AN UNREACHABLE SERVER IS A DEGRADED START, NOT A REFUSAL - AND THAT IS A DECISION
    A server that is down at startup costs the profile THAT SERVER'S tools and nothing
    else. The local toolsets are untouched, `toolset_for` returns, `tool_names_for` returns
    the local names, and the process starts. The F6 adapter already made this choice for a
    running turn (`_GuardedToolset.get_tools` and `_discover_raw_names` both log at WARNING
    and contribute nothing); composition inherits it rather than inventing a second answer.

    The alternative was considered and rejected: refusing to start would let any third
    party stop this deployment booting by going offline, and MCP is the flakiest dependency
    in the system. The cost of the choice is real - a profile can come up quietly smaller
    than its file says - which is why it is a WARNING in the log and why
    `adapters/driving/cli/preflight.py` is where an operator is meant to see it. A refusal
    there is reserved for what a restart cannot fix: an unregistered toolset name, or a
    server this adapter cannot build at all.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from agent_core.adapters.driven.mcp.toolsets import MCPToolProvider
from agent_core.adapters.driven.tools.delivery import tools as delivery_tools
from agent_core.domain.profile import AgentProfile
from agent_core.ports.tool_provider import ToolProvider

__all__ = [
    "DEFAULT_TOOL_PACKAGES",
    "LocalToolProvider",
    "MCPNotComposedYetError",
    "ToolNameCollisionError",
    "ToolsetBuilder",
    "UnknownToolsetError",
    "build_tool_provider",
]

# A registration is a name and a zero-argument builder. Zero-argument on purpose: a builder
# taking the profile could return different tools for different profiles, and then what an
# agent may do would no longer be readable from the profile file alone.
ToolsetBuilder = Callable[[], FunctionToolset[None]]


class UnknownToolsetError(LookupError):
    """A profile names a toolset nothing registered.

    A `LookupError`, and raised rather than skipped. Silently ignoring a typo gives an
    agent that is mysteriously less capable with nothing in the logs
    (ports/tool_provider.py), and the missing toolset is usually the one the persona was
    written around.
    """


class ToolNameCollisionError(ValueError):
    """Two toolsets in one profile export the same tool name.

    Refused at composition rather than resolved by order. `ToolPolicy` rules and audit
    rows both key on the tool NAME, so a shadowed tool means a rule written for one
    function gates a different one - the local-shadowing case ports/tool_provider.py
    names for MCP, arriving from two local packages instead.
    """


class MCPNotComposedYetError(NotImplementedError):
    """The profile declares MCP servers and this provider was built without an MCP half.

    t-f11-28 filled that seat, so `build_tool_provider` never produces a provider that
    raises this. It survives for the one case that is still real: a `LocalToolProvider`
    constructed directly with no `mcp` collaborator - an embedding host wiring its own
    handful of local tools, or a test - handed a profile that names a server.

    Loud rather than ignored, for the reason in the module docstring: composed or refused,
    never silently smaller. Note the distinction it draws with a DEGRADED start - this is
    "nothing here can ever serve that server", not "that server is down right now".
    """


class LocalToolProvider:
    """`ToolProvider` over an explicit registry of local tool packages.

    Structurally conforms to `ports.tool_provider.ToolProvider`; the port is
    `runtime_checkable` precisely so that can be asserted without importing this class.
    """

    def __init__(
        self, packages: Mapping[str, ToolsetBuilder], mcp: ToolProvider | None = None
    ) -> None:
        # Copied, not aliased. A registry a caller can keep mutating after construction is
        # auto-discovery with extra steps: what an agent may do would depend on when the
        # turn ran rather than on what the profile says.
        self._packages: dict[str, ToolsetBuilder] = dict(packages)
        # t-f11-28. The F6 adapter, used for the MCP half of the profile and nothing else.
        # `None` is the honest F1 shape and still refuses an MCP profile - see
        # `MCPNotComposedYetError`. Held for the life of the provider because the schema
        # cache lives on it (t-f6-05): rebuilding one per turn would spawn a server per
        # turn to answer a question whose answer has not changed.
        self._mcp = mcp

    async def toolset_for(self, profile: AgentProfile) -> AbstractToolset[None]:
        """One object for `PydanticAgentRunner` to hand Pydantic AI.

        ASYNC (D13): the MCP half builds transports here - constructed, not connected. A
        server is reached on first use and on schema discovery, never merely because a
        container was built (`composition.py`: NOTHING HERE CONNECTS).

        Always a `CombinedToolset`, even for one package and even for none, so the runner
        sees one shape rather than three. `AbstractToolset` is what
        `PydanticAgentRunner._toolsets_for` type-checks for, and an empty profile getting
        an empty toolset is the honest answer - an agent with no tools, not a fake one.
        """
        toolsets: list[AbstractToolset[None]] = list(self._resolve(profile))
        if self._mcp is not None and profile.mcp_servers:
            toolsets.append(await self._mcp_toolset_for(profile))
        return CombinedToolset(toolsets)

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        """The flat name list, in profile order, for `ToolPolicy` and the audit record.

        ASYNC (D13): the MCP half reads the persisted schema cache here, which is I/O even
        on a hit - and which is why this method exists separately from `toolset_for` at
        all: policy filtering runs every turn and must never need a live connection.

        Resolves the same way `toolset_for` does - same registry, same refusals, same
        collision check, same MCP collaborator - so the names the policy narrows are the
        names the model is offered. Two resolution paths would be two answers to one
        question.

        Local names come first, unprefixed; the servers' follow under `mcp_<server>_`. A
        local tool and a server tool of the same name are therefore two entries, never one
        - that is the whole of the shadowing defence (docs/TASKS.md#t-f6-06), and it is
        this join that could have lost it.
        """
        names = [name for toolset in self._resolve(profile) for name in toolset.tools]
        if self._mcp is not None and profile.mcp_servers:
            names.extend(await self._mcp.tool_names_for(profile))
        return tuple(names)

    async def _mcp_toolset_for(self, profile: AgentProfile) -> AbstractToolset[None]:
        """The MCP half, validated at the boundary the way the runner validates this one.

        `ToolProvider.toolset_for` is typed `-> object` - the port names no Pydantic AI
        type on purpose - so the cast back is checked here rather than assumed, and a
        collaborator that returns the wrong shape says so at composition instead of at
        `Agent.run`.
        """
        assert self._mcp is not None
        toolset = await self._mcp.toolset_for(profile)
        if not isinstance(toolset, AbstractToolset):
            raise TypeError(
                f"the MCP ToolProvider returned {type(toolset).__name__}; this adapter "
                "composes Pydantic AI AbstractToolset values."
            )
        return toolset

    def _resolve(self, profile: AgentProfile) -> list[FunctionToolset[None]]:
        """`profile.toolsets` -> built toolsets, in the profile's own order.

        Order is preserved because `AgentProfile` preserves it deliberately
        (domain/profile.py: "`toolsets` order is meaningful"), and because the flat name
        list feeds an audit record a human reads.
        """
        if profile.mcp_servers and self._mcp is None:
            declared = ", ".join(server.name for server in profile.mcp_servers)
            raise MCPNotComposedYetError(
                f"profile {profile.id!r} declares MCP server(s) {declared}, and this "
                "LocalToolProvider was built with no MCP collaborator. Build it with "
                "`build_tool_provider(...)`, which always supplies one "
                "(docs/TASKS.md#t-f11-28). Refusing rather than serving a silently "
                "smaller toolset."
            )

        built: list[FunctionToolset[None]] = []
        owner_of: dict[str, str] = {}

        for wanted in profile.toolsets:
            builder = self._packages.get(wanted)
            if builder is None:
                known = ", ".join(sorted(self._packages)) or "<nothing registered>"
                raise UnknownToolsetError(
                    f"profile {profile.id!r} names toolset {wanted!r}, which is not "
                    f"registered. Registered: {known}. Registration is explicit and "
                    "opt-in - see adapters/driven/tools/provider.py, NO AUTO-DISCOVERY."
                )
            toolset = builder()
            for tool_name in toolset.tools:
                owner = owner_of.get(tool_name)
                if owner is not None:
                    raise ToolNameCollisionError(
                        f"toolsets {owner!r} and {wanted!r} both export {tool_name!r} for "
                        f"profile {profile.id!r}; policy rules and audit rows key on the "
                        "tool name, so one would silently gate the other."
                    )
                owner_of[tool_name] = wanted
            built.append(toolset)

        return built


# THE REGISTRY. One line per vertical, added by hand, and that is the security property -
# not an implementation detail of it. `tools/fraud/` is deliberately absent: it is
# docstring-only (docs/TASKS.md#t-later-02), and a package becomes a capability when
# someone writes it down here, never by existing.
DEFAULT_TOOL_PACKAGES: Mapping[str, ToolsetBuilder] = {
    "delivery": delivery_tools.build_toolset,
}


def build_tool_provider(
    packages: Mapping[str, ToolsetBuilder] | None = None,
    mcp: ToolProvider | None = None,
) -> LocalToolProvider:
    """The provider the composition root wires. `packages` overrides the registry.

    The override exists for an embedding host, which is the case ports/tool_provider.py
    argues the whole no-auto-discovery rule for: someone embedding this core wants to
    register their own handful of tools, not inherit ours.

    ALWAYS COMPOSED WITH AN MCP HALF - t-f11-28. This is the one constructor production
    uses (`composition.build_container`) and the one `preflight` asks its question of, so
    a profile that declares a server is servable from both. Before this line existed, the
    F6 machinery was complete and reachable only from a test, `delivery_optimizer.yaml`
    declared a real server, and `preflight` correctly refused to start the process over a
    capability the tree already had.

    `mcp` is a seam, not a switch: pass a fake to test the join without spawning anything.
    Passing `None` here means "build the default F6 adapter", NOT "no MCP" - a bare
    `LocalToolProvider(packages)` is how you ask for a local-only provider, and it refuses
    an MCP profile out loud rather than serving it short.
    """
    return LocalToolProvider(
        DEFAULT_TOOL_PACKAGES if packages is None else packages,
        mcp=MCPToolProvider() if mcp is None else mcp,
    )
