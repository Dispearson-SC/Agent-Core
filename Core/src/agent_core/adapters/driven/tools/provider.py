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

WHAT F6 ADDS AND WHAT IT DOES NOT
    MCP composes INSIDE this adapter (docs/DECISIONS.md#d10): `toolset_for` will build an
    `MCPToolset` per `profile.mcp_servers`, apply tool_include/tool_exclude, prefix the
    names `mcp_<server>_<tool>`, and return them in the same composed object. Nothing above
    this file changes when that lands - which is what "only two ports change per vertical"
    means in practice.

    Until then a profile that DECLARES an MCP server is refused rather than served a
    silently smaller toolset. A capability the profile granted and the agent never received
    is the failure mode that produces "the agent is mysteriously less capable, with nothing
    in the logs" - the same sentence the port uses about swallowing an unknown name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from agent_core.adapters.driven.tools.delivery import tools as delivery_tools
from agent_core.domain.profile import AgentProfile

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
    """The profile declares MCP servers and this adapter is the F1 local-only one.

    Loud rather than ignored: see WHAT F6 ADDS in the module docstring. The composition
    seat is docs/TASKS.md#t-f6-05.
    """


class LocalToolProvider:
    """`ToolProvider` over an explicit registry of local tool packages.

    Structurally conforms to `ports.tool_provider.ToolProvider`; the port is
    `runtime_checkable` precisely so that can be asserted without importing this class.
    """

    def __init__(self, packages: Mapping[str, ToolsetBuilder]) -> None:
        # Copied, not aliased. A registry a caller can keep mutating after construction is
        # auto-discovery with extra steps: what an agent may do would depend on when the
        # turn ran rather than on what the profile says.
        self._packages: dict[str, ToolsetBuilder] = dict(packages)

    async def toolset_for(self, profile: AgentProfile) -> AbstractToolset[None]:
        """One object for `PydanticAgentRunner` to hand Pydantic AI.

        ASYNC (D13): F6 opens MCP transports here. F1 awaits nothing real, exactly as the
        port says it should.

        Always a `CombinedToolset`, even for one package and even for none, so the runner
        sees one shape rather than three. `AbstractToolset` is what
        `PydanticAgentRunner._toolsets_for` type-checks for, and an empty profile getting
        an empty toolset is the honest answer - an agent with no tools, not a fake one.
        """
        return CombinedToolset(self._resolve(profile))

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        """The flat name list, in profile order, for `ToolPolicy` and the audit record.

        ASYNC (D13): F6 reads the persisted schema cache here, which is I/O even on a hit.

        Resolves the same way `toolset_for` does - same registry, same refusals, same
        collision check - so the names the policy narrows are the names the model is
        offered. Two resolution paths would be two answers to one question.
        """
        return tuple(name for toolset in self._resolve(profile) for name in toolset.tools)

    def _resolve(self, profile: AgentProfile) -> list[FunctionToolset[None]]:
        """`profile.toolsets` -> built toolsets, in the profile's own order.

        Order is preserved because `AgentProfile` preserves it deliberately
        (domain/profile.py: "`toolsets` order is meaningful"), and because the flat name
        list feeds an audit record a human reads.
        """
        if profile.mcp_servers:
            declared = ", ".join(server.name for server in profile.mcp_servers)
            raise MCPNotComposedYetError(
                f"profile {profile.id!r} declares MCP server(s) {declared}; this is the "
                "F1 local-only ToolProvider (docs/TASKS.md#t-f6-05). Refusing rather "
                "than serving a silently smaller toolset."
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
) -> LocalToolProvider:
    """The provider the composition root wires. `packages` overrides the registry.

    The override exists for an embedding host, which is the case ports/tool_provider.py
    argues the whole no-auto-discovery rule for: someone embedding this core wants to
    register their own handful of tools, not inherit ours.
    """
    return LocalToolProvider(DEFAULT_TOOL_PACKAGES if packages is None else packages)
