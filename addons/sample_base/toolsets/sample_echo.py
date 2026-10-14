"""The sample addon's toolset: one tool that returns its input."""

from __future__ import annotations

from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


def sample_echo(text: str) -> str:
    """Return `text` unchanged."""
    return text


def build_toolset() -> FunctionToolset[None]:
    return FunctionToolset([Tool(sample_echo)])
