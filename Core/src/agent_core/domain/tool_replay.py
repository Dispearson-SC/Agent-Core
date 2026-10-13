"""What happens to a tool call that a crash interrupted (C30, D65). Pure.

A turn that dies mid-run is run again by the durable workflow. Most of it is harmless to repeat, but
a tool that SENDS, CHARGES or WRITES may already have done its work when the process died, and
running it a second time does it twice. Each tool says which kind it is, in its metadata:

  * `replay: safe`        - repeating it is harmless (it only reads, or it only defers). It simply
                            runs again.
  * `replay: interrupted` - repeating it is not known to be harmless. If an earlier run of THIS
                            call began and its end was never recorded, it is NOT run again: the
                            model is told the call was interrupted and decides what to do.

The default is `interrupted`: a tool nobody classified (a vertical's, an MCP server's) is the one
whose effects nobody has vouched for. A value the code does not recognise reads the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "DEFAULT_REPLAY",
    "REPLAY_METADATA_KEY",
    "ReplayPolicy",
    "interrupted_result",
    "replay_policy",
    "safe_tool_metadata",
]

REPLAY_METADATA_KEY: Final[str] = "replay"


class ReplayPolicy(StrEnum):
    SAFE = "safe"
    INTERRUPTED = "interrupted"


DEFAULT_REPLAY: Final[ReplayPolicy] = ReplayPolicy.INTERRUPTED


def safe_tool_metadata() -> dict[str, Any]:
    """What a toolset passes to declare a tool safe: `Tool(fn, metadata=safe_tool_metadata())`. A
    new dict per tool, so no two tools share a mutable mapping."""
    return {REPLAY_METADATA_KEY: ReplayPolicy.SAFE.value}


def replay_policy(metadata: Mapping[str, Any] | None) -> ReplayPolicy:
    """The replay policy a tool's metadata declares; the default when it says nothing or says
    something this code does not know (fail closed: an unknown word never reads as `safe`)."""
    raw = (metadata or {}).get(REPLAY_METADATA_KEY)
    try:
        return ReplayPolicy(raw) if raw is not None else DEFAULT_REPLAY
    except ValueError:
        return DEFAULT_REPLAY


def interrupted_result(tool_name: str) -> str:
    """The text the MODEL receives in place of the result of an interrupted call. It says what is
    known (the call began, and ended without a recorded result), what was NOT done (it was not run
    again) and what the model may do (check first, or tell the person)."""
    return (
        f"Tool call interrupted and not repeated: `{tool_name}` was already started by an earlier "
        "attempt of this turn that was cut short, and its result was never recorded. It may or "
        "may not have taken effect. Check the current state with a read-only tool before trying "
        "again, or tell the person what happened."
    )
