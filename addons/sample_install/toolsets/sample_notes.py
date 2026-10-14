"""The sample install addon's toolset: one tool whose audit is declared in the manifest."""

from __future__ import annotations

from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


def sample_note_add(topic: str, note: str) -> str:
    """Acknowledge a note about a topic (nothing is stored)."""
    return f"noted: {topic}"


def build_toolset() -> FunctionToolset[None]:
    return FunctionToolset([Tool(sample_note_add)])
