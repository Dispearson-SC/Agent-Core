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
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml

from agent_core.adapters.driven.profiles_fs.fragments import FragmentFile
from agent_core.domain.profile import AgentProfile, ProfileValidationError

__all__ = [
    "PROFILE_FILE_ERRORS",
    "FragmentReader",
    "fragment_reader",
    "load_profile",
    "load_profile_sync",
    "parse_profile",
    "resolve_includes",
]

# What reading one profile file can raise: a rule broken (`ProfileValidationError` is a
# `ValueError`) or a document that is not YAML at all. The composition root catches this pair for
# an addon's profile so that `yaml` stays an import of this adapter, not of the root.
PROFILE_FILE_ERRORS: tuple[type[Exception], ...] = (ValueError, yaml.YAMLError)


def parse_profile(
    text: str,
    *,
    fragments_dir: Path | None = None,
    addon_fragments: Mapping[str, FragmentFile] | None = None,
) -> AgentProfile:
    """Parse one profile document. Raises `ProfileValidationError` on anything malformed.

    `safe_load`, never `load`: a profile file is configuration, and a loader that can
    construct arbitrary Python objects turns "someone may edit a profile" into "someone may
    run code in the agent process". Same reasoning as the note on `ApprovalRule.condition`.

    `include: [name, ...]` is resolved HERE, not in the domain, because it is file reading:
    each `<fragments_dir>/<name>.md` is appended to `persona` in the order listed, separated
    by a blank line. The domain only ever sees the resolved persona, so editing a fragment
    moves the content hash and earns a new profile version (D20). A missing fragment, or no
    fragments directory, is a `ProfileValidationError` at load - never a silently shorter
    persona.

    `addon_fragments` (Phase 2, A4) is `name -> file` for the fragments enabled addons provide.
    A name is looked up in `fragments_dir` first; the addon registration has already refused a
    name both provide, so the order never decides anything.
    """
    data: Any = yaml.safe_load(text)
    return AgentProfile.from_mapping(
        resolve_includes(data, fragment_reader(fragments_dir, addon_fragments))
    )


# A fragment name is a bare file stem. The pattern is what keeps `include: ["../x"]` from
# reading outside the fragments directory.
_FRAGMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

FragmentReader = Callable[[str], str]


def fragment_reader(
    fragments_dir: Path | None, addon_fragments: Mapping[str, FragmentFile] | None = None
) -> FragmentReader:
    """`name -> the fragment's text`, looked up in `fragments_dir` first, then in the addons'.

    Raises `ProfileValidationError` for a name that is not a bare stem and for a fragment that
    does not exist - never a silently shorter persona.
    """

    def read(name: str) -> str:
        if not _FRAGMENT_NAME.fullmatch(name) or ".." in name:
            raise ProfileValidationError(f"invalid fragment name {name!r}")
        path: FragmentFile | None = None if fragments_dir is None else fragments_dir / f"{name}.md"
        if (path is None or not path.is_file()) and addon_fragments and name in addon_fragments:
            path = addon_fragments[name]
        if path is None or not path.is_file():
            raise ProfileValidationError(
                f"include: fragment {name!r} not found"
                + ("" if fragments_dir is None else f" in {fragments_dir}")
            )
        return path.read_text(encoding="utf-8").strip()

    return read


def resolve_includes(data: Any, read: FragmentReader) -> Any:
    """`data` with `include:` resolved into `persona` (the document unchanged when it has none)."""
    if isinstance(data, dict) and "include" in data:
        data = {**data}
        names = data.pop("include")
        data["persona"] = _with_fragments(data.get("persona"), names, read)
    return data


def _with_fragments(persona: object, names: object, read: FragmentReader) -> object:
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ProfileValidationError("include must be a list of fragment names")
    if not isinstance(persona, str):
        return persona  # from_mapping reports the missing/invalid persona itself
    return "\n\n".join([persona, *(read(name) for name in names)])


def load_profile_sync(
    path: str | Path,
    fragments_dir: Path | None = None,
    addon_fragments: Mapping[str, FragmentFile] | None = None,
) -> AgentProfile:
    """Read and parse one profile file. The sync island - see the module docstring.

    Fragments default to the `fragments/` directory next to the profile file.
    """
    file = Path(path)
    directory = file.parent / "fragments" if fragments_dir is None else fragments_dir
    return parse_profile(
        file.read_text(encoding="utf-8"),
        fragments_dir=directory,
        addon_fragments=addon_fragments,
    )


async def load_profile(path: str | Path) -> AgentProfile:
    """Read and parse one profile file without blocking the event loop."""
    return await asyncio.to_thread(load_profile_sync, path)
