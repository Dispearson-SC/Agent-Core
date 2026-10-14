"""The `glazed_past` toolset: exactly this role's tools (see _glazed.py)."""

from typing import Any

from pydantic_ai.toolsets import FunctionToolset

from ._glazed import build_role


def build_toolset(settings: Any) -> FunctionToolset[Any]:
    return build_role(settings, "past")
