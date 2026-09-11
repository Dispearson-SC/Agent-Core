"""The `ToolProvider` adapter, and the container seat it was blocking.

Phase:   F1 (the local provider) / F4 (the vertical it resolves against)
Tasks:   docs/TASKS.md#t-f1-21
Covers:  adapters/driven/tools/provider.py
         composition.py (the `start_turn` seat)

WHAT THIS FILE IS FOR
    `t-f1-07` froze `ports/tool_provider.py` and nothing implemented it. The only
    implementation in the repository was `_DeliveryToolProvider` inside
    tests/integration/test_f0_end_to_end.py, whose own docstring calls it "a TEST double,
    not an adapter" - so `composition.py` could wire every other seat and still refuse to
    produce a working `start_turn`. A frozen port with no adapter is not a finished port.

THE ASSERTION THAT MATTERS: NO AUTO-DISCOVERY, SEEN FROM THE MODEL'S SIDE
    `ports/tool_provider.py` and `tools/delivery/tools.py` both state the rule in prose:
    a profile NAMES its toolsets, registration is explicit, and nothing is inherited by
    living in the tree. The failure that rule prevents is not "the provider holds the
    wrong dict" - it is "the model was advertised a capability nobody granted it".

    So the toolset assertion goes through a real `pydantic_ai` agent and reads the tool
    definitions the model was actually offered (`TestModel.last_model_request_parameters`),
    rather than reaching into `FunctionToolset.tools`. A provider could compose the right
    objects and still advertise the wrong names; only the model's own view settles it.

    Two packages are registered and the profile names ONE. The second package's tool is
    registered, importable, and present in the provider - and it must not reach the model.
"""

from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from agent_core.adapters.driven.tools.delivery import tools as delivery_tools
from agent_core.domain.profile import AgentProfile

pytestmark = [pytest.mark.phase("F1")]

_PROVIDER_MODULE = "agent_core.adapters.driven.tools.provider"

_DELIVERY_TOOL_NAMES = frozenset(
    {"routing_estimate", "pricing_quote", "pricing_apply", "orders_lookup"}
)
_UNGRANTED_TOOL_NAME = "issue_refund"


def _provider_module() -> ModuleType:
    """The adapter module, with a readable assertion when it is not there yet.

    `find_spec` first, deliberately: a bare `import_module` of a module that does not
    exist raises `ModuleNotFoundError`, and an error is not a red test - it proves the
    file failed to load, not that the behaviour is absent (same reasoning as
    tests/unit/test_composition.py::_composition).
    """
    assert importlib.util.find_spec(_PROVIDER_MODULE) is not None, (
        f"{_PROVIDER_MODULE} does not exist. `t-f1-07` froze `ports/tool_provider.py` "
        "and nothing implements it (docs/TASKS.md#t-f1-21)."
    )
    return importlib.import_module(_PROVIDER_MODULE)


def issue_refund(order_id: str) -> str:
    """A tool the delivery profile was never granted. Deliberately mutating-sounding."""
    return f"refunded {order_id}"


def _billing_toolset() -> FunctionToolset[None]:
    return FunctionToolset([issue_refund])


def _profile(*toolsets: str) -> AgentProfile:
    return AgentProfile(
        id="delivery_optimizer",
        persona="You optimize delivery routing and pricing.",
        model="minimax/MiniMax-M3",
        toolsets=toolsets,
    )


def _registered_provider() -> Any:
    """A provider holding BOTH packages. The profile is what narrows it, not the registry."""
    module = _provider_module()
    return module.LocalToolProvider(
        {
            "delivery": delivery_tools.build_toolset,
            "billing": _billing_toolset,
        }
    )


async def _advertised_tool_names(toolset: AbstractToolset[None]) -> frozenset[str]:
    """The tool names a model is actually offered when handed this toolset."""
    model = TestModel(call_tools=[])
    await Agent(model, toolsets=[toolset]).run("hello")
    parameters = model.last_model_request_parameters
    assert parameters is not None, "the run made no model request; nothing was advertised"
    return frozenset(definition.name for definition in parameters.function_tools)


@pytest.mark.asyncio
async def test_only_the_toolsets_the_profile_names_reach_the_model() -> None:
    """A registered package the profile does not name contributes nothing. No auto-discovery."""
    provider = _registered_provider()
    profile = _profile("delivery")

    names = await provider.tool_names_for(profile)

    assert frozenset(names) == _DELIVERY_TOOL_NAMES
    assert _UNGRANTED_TOOL_NAME not in names, (
        "a tool from a registered but unnamed package leaked into the flat name list; "
        "ToolPolicy.filter_toolset and the audit record both read this"
    )

    toolset = await provider.toolset_for(profile)
    assert isinstance(toolset, AbstractToolset), (
        "PydanticAgentRunner._toolsets_for rejects anything that is not an AbstractToolset"
    )

    advertised = await _advertised_tool_names(toolset)
    assert advertised == _DELIVERY_TOOL_NAMES
    assert _UNGRANTED_TOOL_NAME not in advertised, (
        "the model was advertised a tool this profile never named - that is the "
        "auto-discovery failure ports/tool_provider.py exists to prevent"
    )


@pytest.mark.asyncio
async def test_a_toolset_name_nothing_registered_is_refused_loudly() -> None:
    """`ports/tool_provider.py`: unknown name -> raise. A typo must not silently disarm."""
    provider = _registered_provider()

    with pytest.raises(LookupError, match="maps"):
        await provider.toolset_for(_profile("delivery", "maps"))

    with pytest.raises(LookupError, match="maps"):
        await provider.tool_names_for(_profile("delivery", "maps"))


@pytest.mark.phase("F1")
def test_build_container_now_produces_a_usable_start_turn() -> None:
    """The seat `composition.py` refused to fill, filled.

    `ToolProvider` was the last genuinely empty one - `ContextEngine` and `SkillRegistry`
    are both implemented and the MISSING SEATS note listing them was stale. The container
    must now carry a `StartTurn` whose eight collaborators are all real objects; a
    container that builds and dies on the first turn is what that file exists to prevent.
    """
    composition = importlib.import_module("agent_core.composition")
    start_turn_class = importlib.import_module("agent_core.application.start_turn").StartTurn

    container = composition.build_container()
    try:
        assert hasattr(container, "start_turn"), (
            "the container still has no `start_turn`; ToolProvider now has an adapter "
            "(docs/TASKS.md#t-f1-21) so there is no empty seat left to refuse for"
        )
        assert isinstance(container.start_turn, start_turn_class)
        assert container.tools is not None
        assert container.context is not None
        assert container.skills is not None
    finally:
        container.domain_pool.close()
        container.audit_pool.close()
