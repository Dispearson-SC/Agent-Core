"""The `glazed_liaison` toolset - exactly this role's tools (listed in glazed/tools.py)."""

from __future__ import annotations

from typing import Any

from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.glazed.tools import TOOLSETS


def build_toolset() -> FunctionToolset[Any]:
    """A fresh toolset per call, like every vertical's builder."""
    return TOOLSETS["liaison"]()
