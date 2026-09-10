"""Delivery vertical toolset - exactly the named tools, no auto-discovery.

Tasks: docs/TASKS.md#t-f4-01

The property under test is CLAUDE.md non-negotiable #8 and `ports/tool_provider.py`'s
"MUST NOT auto-discover", aimed at one vertical's package instead of the composite
provider: a vertical's `tools.py` registers its tools EXPLICITLY, by naming them, never by
scanning the module namespace for callables. `t-f1-07`'s own docstring names the failure
mode this guards against - one `discover_builtin_tools()` call in Hermes AST-scanned
`tools/*.py` and turned into an accidental ~396-module import closure from a single line.

This is a regression lock on a *design choice*, not just a snapshot of today's four names:
the second test proves the builder cannot be fooled by a function that merely EXISTS in the
module, which is the shape a lazy "scan `globals()`" implementation would pass by accident.
"""

from __future__ import annotations

import agent_core.adapters.driven.tools.delivery.tools as delivery_tools

EXPECTED_TOOL_NAMES = frozenset(
    {
        "routing_estimate",
        "pricing_quote",
        "pricing_apply",
        "orders_lookup",
    }
)


def _tool_names(toolset: object) -> frozenset[str]:
    # `FunctionToolset` keeps its registrations in a plain, synchronously readable `.tools`
    # dict keyed by name - no `RunContext` is needed to read WHAT was registered, only to
    # CALL one of them.
    return frozenset(toolset.tools.keys())  # type: ignore[attr-defined]


def test_toolset_exposes_exactly_the_named_delivery_tools() -> None:
    toolset = delivery_tools.build_toolset()

    assert _tool_names(toolset) == EXPECTED_TOOL_NAMES


def test_request_evidence_and_ask_peer_are_not_here() -> None:
    """TASKS.md moved these to shared modules; a vertical package must not re-copy them."""
    names = _tool_names(delivery_tools.build_toolset())

    assert "request_evidence" not in names
    assert "ask_peer" not in names


def test_adding_a_function_to_the_module_does_not_add_a_tool() -> None:
    """No auto-discovery: a builder that scanned the module namespace would pick this up.

    The smuggled function has the exact shape of a real tool (a plain function taking a
    string) so the only thing distinguishing it from `orders_lookup` is that nobody named
    it in the registration list.
    """

    def smuggled_tool(order_id: str) -> str:
        return order_id

    delivery_tools.smuggled_tool = smuggled_tool  # type: ignore[attr-defined]
    try:
        names = _tool_names(delivery_tools.build_toolset())

        assert names == EXPECTED_TOOL_NAMES
        assert "smuggled_tool" not in names
    finally:
        del delivery_tools.smuggled_tool  # type: ignore[attr-defined]
