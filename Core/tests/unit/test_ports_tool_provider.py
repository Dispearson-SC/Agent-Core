"""The ToolProvider port contract.

Tasks: docs/TASKS.md#t-f1-07

The property under test is the reason the port has two methods instead of one:
`tool_names_for` must answer WITHOUT constructing a live toolset. Policy filtering and
the audit record only ever need names, and building the toolset means opening MCP
transports - stdio child processes, HTTP or SSE connections - which is slow and fails for
reasons that have nothing to do with "may this caller use this tool".

The port is checked structurally, never through an adapter: `ports/` owns the contract,
and a test that has to import a concrete adapter to verify it is testing the adapter.

There is deliberately no auto-discovery anywhere in this file. Registration is explicit
(see the port docstring); a test that asked the provider to "find" its tools would be
asserting the exact behaviour the port forbids.
"""

import asyncio
import inspect

# Imported as a module so that a name that is not frozen yet fails inside the test that
# needs it, rather than failing the whole file at COLLECTION. The submodule is imported
# explicitly rather than pulled off the package: `ports/__init__.py` re-exports nothing.
import agent_core.ports.tool_provider as tool_provider_port
from agent_core.domain.profile import AgentProfile

PROFILE = AgentProfile(
    id="delivery_optimizer",
    persona="does not matter here",
    model="claude-sonnet-5",
    toolsets=("delivery",),
)


class NamesOnlyProvider:
    """Answers names from a registered table; explodes if anything builds a toolset.

    A recording fake rather than a mock: the test asserts on `toolset_calls` afterwards,
    so the failure message says what happened instead of what was expected to happen.
    """

    def __init__(self) -> None:
        self.toolset_calls = 0

    async def toolset_for(self, profile: AgentProfile) -> object:
        self.toolset_calls += 1
        raise AssertionError(
            "tool_names_for must not construct a toolset - that opens MCP transports"
        )

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        return ("delivery_quote", "mcp_maps_route")


class ToolsetOnlyProvider:
    """Half a provider: names are only obtainable by building the live toolset."""

    async def toolset_for(self, profile: AgentProfile) -> object:
        return object()


def _is_runtime_checkable(protocol: type) -> bool:
    return getattr(protocol, "_is_runtime_protocol", False) is True


def test_tool_names_for_answers_without_constructing_a_toolset() -> None:
    port = tool_provider_port.ToolProvider

    # Structural conformance has to be checkable from ports/ alone - otherwise the only
    # way to prove an implementation satisfies the port is to import an adapter.
    assert _is_runtime_checkable(port), "ToolProvider must be a runtime_checkable Protocol"

    provider = NamesOnlyProvider()
    assert isinstance(provider, port)

    names = asyncio.run(provider.tool_names_for(PROFILE))

    assert names == ("delivery_quote", "mcp_maps_route")
    assert provider.toolset_calls == 0, "answering names must have no discovery side effect"


def test_a_provider_that_can_only_build_a_toolset_does_not_satisfy_the_port() -> None:
    """The two methods are not interchangeable, and the port says so.

    If `tool_names_for` were optional, an implementation would answer the policy question
    by constructing the toolset first, and the separation would quietly stop existing.
    """
    port = tool_provider_port.ToolProvider

    assert _is_runtime_checkable(port), "ToolProvider must be a runtime_checkable Protocol"
    assert not isinstance(ToolsetOnlyProvider(), port)


def test_both_port_methods_are_awaitable() -> None:
    """D13: both are I/O - transports on one side, the F6 schema cache on the other."""
    port = tool_provider_port.ToolProvider

    assert inspect.iscoroutinefunction(port.toolset_for)
    assert inspect.iscoroutinefunction(port.tool_names_for)
