"""Driven adapter: SkillRegistry over SKILL.md files.

Phase:   F6
Tasks:   docs/TASKS.md#t-f6-02
Status:  DONE - index caches by (path, mtime_ns); read resolves through the index and
         proves containment. tests/unit/test_skills_fs.py.
Implements: ports/skill_registry.py

LAYOUT
    Core/skills/<name>/SKILL.md - YAML frontmatter + markdown body.

THE RULE THAT MAKES THIS WORTH BUILDING
    index() parses FRONTMATTER ONLY and never touches the body. Roughly one line per skill
    reaches the system prompt; the model calls `skill_view` when it wants more.

    Fifty skills: ~5K characters instead of ~200K. If a body ever reaches the prompt, the
    optimisation is silently undone and the only symptom is a larger bill.

    So `read_frontmatter` stops reading AT the closing delimiter rather than slurping the
    file and slicing it afterwards. Slicing would give the same metadata; it would also
    put every body through memory on every turn, which is the same cost by another route.

CACHE
    index() runs every turn. Cache by (path, mtime). Re-reading fifty files per turn is
    real latency for information that changes weekly at most.

    Key on `st_mtime_ns`, not `st_mtime`: a float mtime on a fast filesystem can compare
    equal across two edits inside the same tick, and a cache that misses a real edit is
    worse than no cache - the stale description is advertised until the process restarts.
    The directory scan still runs each turn; a scan is not a file read, and it is what
    lets a newly added skill appear without a restart.

PATH TRAVERSAL - read() takes a name from the MODEL
    Resolve strictly through the index. NEVER join the name onto a directory. A model that
    has read a hostile web page will try `../../.env`, and it costs nothing to try.

    Two independent gates, because either alone is a single point of failure:

    1. `name` is looked up in the index. It is a key, never a path fragment, so a name
       that is not a known skill cannot reach the filesystem at all.
    2. Whatever path comes back is resolved and proven to sit inside the skills root by
       `contained_path`. This is what catches an entry that is legitimate in the index but
       points outside once symlinks are followed - a case gate 1 cannot see.

    Note what is deliberately NOT here: a check for the characters "..". It rejects a
    legitimate path that normalises back inside the root, and it accepts an escaping path
    that never spells them - a symlink, or an absolute path. Resolution answers both.

FAIL CLOSED, PER SKILL
    A skill whose frontmatter is malformed, whose declared name disagrees with its
    directory, or whose `requires` are unmet is not advertised, and index() keeps going.
    One bad file must not empty an index that runs on every turn of every agent, and it
    must not advertise something the model will then fail to use.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml

from agent_core.domain.profile import AgentProfile
from agent_core.ports.skill_registry import SkillMeta

__all__ = [
    "FilesystemSkillRegistry",
    "SkillPathEscapeError",
    "SkillRegistryError",
    "UnknownSkillError",
    "contained_path",
    "read_frontmatter",
]

SKILL_FILENAME = "SKILL.md"
_DELIMITER = "---"


class SkillRegistryError(Exception):
    """Base for every refusal this adapter makes, so a caller can catch the boundary."""


class UnknownSkillError(SkillRegistryError, LookupError):
    """`name` is not a skill in the index. It is never retried as a path."""


class SkillPathEscapeError(SkillRegistryError, ValueError):
    """A resolved path landed outside the skills root."""


def contained_path(root: Path | str, candidate: Path | str) -> Path:
    """Resolve `candidate` and return it only if it lies inside `root`.

    Resolution happens first and on both sides: symlinks followed, `..` collapsed, drive
    and case normalised by the platform. Comparing unresolved paths would compare two
    spellings rather than two locations.
    """
    resolved_root = Path(root).resolve()
    resolved = Path(candidate).resolve()
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise SkillPathEscapeError(f"path resolves outside the skills root: {resolved.name}")
    return resolved


def read_frontmatter(path: Path) -> str:
    """The frontmatter block of one SKILL.md, without ever reading into the body.

    Returns "" when the file does not open with a delimiter line or never closes it -
    both are malformed, and the caller drops the skill rather than guessing.
    """
    lines: list[str] = []
    with path.open(encoding="utf-8") as handle:
        first = handle.readline()
        if first.strip() != _DELIMITER:
            return ""
        for line in handle:
            if line.strip() == _DELIMITER:
                return "".join(lines)
            lines.append(line)
    return ""


def read_body(path: Path) -> str:
    """Everything after the frontmatter. The only function here that touches a body."""
    with path.open(encoding="utf-8") as handle:
        first = handle.readline()
        if first.strip() != _DELIMITER:
            handle.seek(0)
            return handle.read()
        for line in handle:
            if line.strip() == _DELIMITER:
                break
        return handle.read().lstrip("\n")


@dataclass(frozen=True, slots=True)
class _Skill:
    """One parsed skill: the prompt payload, plus what index() filters on."""

    meta: SkillMeta
    path: Path
    namespaces: frozenset[str]
    requires_env: tuple[str, ...]
    requires_bin: tuple[str, ...]
    requires_platform: tuple[str, ...]

    def is_available(self) -> bool:
        if any(not os.environ.get(name) for name in self.requires_env):
            return False
        if any(shutil.which(binary) is None for binary in self.requires_bin):
            return False
        return not (self.requires_platform and sys.platform not in self.requires_platform)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    mtime_ns: int
    skill: _Skill | None  # None: this file was read and found malformed. Do not re-read it.


def _as_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def _parse(path: Path, root: Path, directory_name: str) -> _Skill | None:
    """Frontmatter to a `_Skill`, or None when the file may not be advertised."""
    text = read_frontmatter(path)
    if not text.strip():
        return None
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, Mapping):
        return None
    mapping: Mapping[str, object] = data

    declared = mapping.get("name")
    # Identity is the DIRECTORY name, because that is what a path can be recovered from.
    # A frontmatter name that disagrees means index() and read() would answer to different
    # keys for the same file, so the skill is dropped rather than advertised ambiguously.
    if declared is not None and str(declared) != directory_name:
        return None

    description = mapping.get("description")
    if not isinstance(description, str) or not description.strip():
        return None

    requires = mapping.get("requires")
    requires_map: Mapping[str, object] = requires if isinstance(requires, Mapping) else {}

    return _Skill(
        meta=SkillMeta(
            name=directory_name,
            description=" ".join(description.split()),
            # Relative and POSIX: an absolute host path would put the deployment layout
            # into every system prompt, once per skill.
            path=path.relative_to(root).as_posix(),
        ),
        path=path,
        namespaces=frozenset(_as_str_tuple(mapping.get("namespaces"))),
        requires_env=_as_str_tuple(requires_map.get("env")),
        requires_bin=_as_str_tuple(requires_map.get("bin")),
        requires_platform=_as_str_tuple(requires_map.get("platform")),
    )


class FilesystemSkillRegistry:
    """`SkillRegistry` over a directory of SKILL.md files. See the module docstring."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._cache: dict[Path, _CacheEntry] = {}

    async def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        """Metadata for the skills this profile may see, and nothing else.

        ASYNC (D13): the scan and any cache miss are blocking filesystem calls, so they
        run in a thread rather than stalling the loop for every turn of every session.
        """
        return await asyncio.to_thread(self._index_sync, profile)

    async def read(self, name: str) -> str:
        """One body, by name, without its frontmatter."""
        return await asyncio.to_thread(self._read_sync, name)

    def _index_sync(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        wanted = frozenset(profile.skill_namespaces)
        return tuple(
            skill.meta
            for skill in self._scan()
            # Empty namespaces on either side advertise nothing. Fail closed: the fraud
            # agent receiving the delivery agent's index is the failure this filter exists
            # for, and a default of "everything" makes a forgotten field into that leak.
            if (skill.namespaces & wanted) and skill.is_available()
        )

    def _read_sync(self, name: str) -> str:
        for skill in self._scan():
            if skill.meta.name == name:
                # Gate 2. The index is not evidence about the filesystem: an entry can be
                # well-formed and still point outside the root once symlinks resolve.
                return read_body(contained_path(self._root, skill.path))
        # Gate 1. `name` was a key that missed. It is never retried as a path.
        raise UnknownSkillError(f"no such skill: {name!r}")

    def _scan(self) -> tuple[_Skill, ...]:
        """Every valid skill under the root, refreshing only what changed.

        Sorted by name so the prompt is byte-identical across turns and across machines -
        an index that reorders itself invalidates the provider's prefix cache for free.
        """
        seen: set[Path] = set()
        skills: list[_Skill] = []
        for path in self._skill_files():
            seen.add(path)
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue  # Deleted between the walk and the stat. Next turn will agree.
            cached = self._cache.get(path)
            if cached is None or cached.mtime_ns != mtime_ns:
                cached = _CacheEntry(
                    mtime_ns=mtime_ns,
                    skill=_parse(path, self._root, path.parent.name),
                )
                self._cache[path] = cached
            if cached.skill is not None:
                skills.append(cached.skill)
        for gone in self._cache.keys() - seen:
            del self._cache[gone]
        return tuple(sorted(skills, key=lambda skill: skill.meta.name))

    def _skill_files(self) -> Iterator[Path]:
        """Candidate SKILL.md paths, each already proven to sit inside the root."""
        if not self._root.is_dir():
            return
        for directory in sorted(self._root.iterdir()):
            if not directory.is_dir():
                continue
            candidate = directory / SKILL_FILENAME
            if not candidate.is_file():
                continue
            try:
                yield contained_path(self._root, candidate)
            except SkillPathEscapeError:
                continue  # A symlinked skill directory pointing out of the root.
