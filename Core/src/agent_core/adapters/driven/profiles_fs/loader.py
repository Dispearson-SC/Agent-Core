"""Driven adapter: AgentProfile files on disk.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-04
Implements: no port - see WHY THERE IS NO PORT HERE.

LAYOUT
    Core/profiles/<id>.yaml - one file per agent, the shape `domain/profile.py` documents.

WHAT THIS ADAPTER OWNS, AND WHY IT IS NOT THE DOMAIN
    The path, the bytes and the YAML parser. All three are outside the domain by rule:
    `domain/` imports nothing external (CLAUDE.md, "Layer rules") and is the sync island
    with no I/O to await (D13). `AgentProfile.from_mapping` owns every rule about what a
    profile MAY SAY; this module owns only how those keys got off a disk.

    The split is what keeps profile validation testable with a dict and no filesystem, and
    it is what lets a profile arrive from somewhere else later - an object store, a config
    service, an HTTP body - by adding a sibling adapter and changing nothing in the domain.

WHY THERE IS NO PORT HERE
    Nothing in `application/` asks for a profile: profiles are resolved once at startup by
    the composition root and passed in as data. A port would be a protocol with exactly one
    caller that is itself the wiring - a seam nobody uses. Add one the day a use case needs
    to load a profile mid-turn, not before.

SYNC AND ASYNC - BOTH, DELIBERATELY
    `load_profile` is the adapter's real signature: async, per D13, with the blocking read
    inside `asyncio.to_thread` so it never stalls the event loop. Same shape as the
    Postgres repositories, which are sync behind `to_thread` for the same reason.

    `load_profile_sync` is the sync island underneath it, exposed rather than hidden
    because the composition root runs at startup, is synchronous by design, and has no
    event loop to be blocked yet. Spinning up a loop with `asyncio.run` per profile file
    to reach the same read would be ceremony that buys nothing, and it would break the
    moment `build_container` were ever called from inside a running loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml

from agent_core.domain.profile import AgentProfile

__all__ = ["load_profile", "load_profile_sync", "parse_profile"]


def parse_profile(text: str) -> AgentProfile:
    """Parse one profile document. Raises `ProfileValidationError` on anything malformed.

    `safe_load`, never `load`: a profile file is configuration, and a loader that can
    construct arbitrary Python objects turns "someone may edit a profile" into "someone may
    run code in the agent process". Same reasoning as the note on `ApprovalRule.condition`.
    """
    data: Any = yaml.safe_load(text)
    return AgentProfile.from_mapping(data)


def load_profile_sync(path: str | Path) -> AgentProfile:
    """Read and parse one profile file. The sync island - see the module docstring."""
    return parse_profile(Path(path).read_text(encoding="utf-8"))


async def load_profile(path: str | Path) -> AgentProfile:
    """Read and parse one profile file without blocking the event loop."""
    return await asyncio.to_thread(load_profile_sync, path)
