"""Resolve the enabled addons into one ordered, Core-compatible set (Phase 2, A2; roadmap 5.4).

Phase:   Phase 2 (addon system)
Tests:   Core/tests/unit/test_addon_resolver.py
Implements: no port - startup wiring (D70).

PURE ON PURPOSE
    Manifests in, an ordered tuple out; no file is read here (`loader.py` reads them). That keeps
    every rule below testable with strings.

THE RULES
    * An addon runs only if it is in the explicit list (`ADDONS`). There is no directory scan that
      enables anything.
    * A dependency is never pulled in for you: if `top` depends on `base` and `base` is not listed,
      startup fails and says to list it. Enabling something is always a visible decision.
    * A BRIDGE (`auto_install: true`) is the one addon that is not listed. It activates when ALL its
      dependencies are active, repeated until nothing changes (a bridge may depend on a bridge),
      and never otherwise. A bridge that IS listed is required like any other addon.
    * Every active addon (bridges included) must accept this Core's version. A bridge that would
      activate but does not fit fails startup: staying silently off would hide the mismatch.
    * The order is dependencies first; addons that do not depend on each other keep the listed
      order, bridges after them in activation order.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from packaging.version import InvalidVersion, Version

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError
from agent_core.adapters.driven.addons_fs.manifest import AddonManifest

__all__ = ["ResolvedManifest", "parse_enabled", "resolve_addons"]

_NAME: Final = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


@dataclass(frozen=True)
class ResolvedManifest:
    """One active addon and how it became active."""

    manifest: AddonManifest
    source: Literal["explicit", "auto_install"]


def parse_enabled(raw: str) -> tuple[str, ...]:
    """The `ADDONS` list: comma separated names; blank means none. Malformed entries are refused
    (an empty entry or a repeat is almost always a typo, and silently skipping it would enable
    less than the operator wrote)."""
    if not raw.strip():
        return ()
    names = tuple(part.strip() for part in raw.split(","))
    for name in names:
        if not _NAME.match(name):
            raise AddonResolutionError(
                None, None, f"ADDONS has an invalid entry {name!r}; use names separated by commas"
            )
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise AddonResolutionError(repeated[0], None, "is listed more than once in ADDONS")
    return names


def resolve_addons(
    *,
    enabled: Sequence[str],
    available: Mapping[str, AddonManifest],
    core_version: str,
) -> tuple[ResolvedManifest, ...]:
    """The active addons in activation order, or an `AddonResolutionError`.

    `available` holds the manifests of the listed addons plus every candidate bridge.
    """
    try:
        core = Version(core_version)
    except InvalidVersion:
        raise AddonResolutionError(
            None, None, f"the Core version {core_version!r} is not a version; cannot check addons"
        ) from None

    for name in enabled:
        if name not in available:
            raise AddonResolutionError(name, None, "is listed in ADDONS but has no manifest")

    sources: dict[str, Literal["explicit", "auto_install"]] = dict.fromkeys(enabled, "explicit")
    order = list(enabled)
    bridges = sorted(n for n, m in available.items() if m.auto_install and n not in sources)
    activated = True
    while activated:
        activated = False
        for name in bridges:
            if name not in sources and all(d in sources for d in available[name].depends):
                sources[name] = "auto_install"
                order.append(name)
                activated = True

    for name in order:
        manifest = available[name]
        if not manifest.core_spec.contains(core, prereleases=True):
            raise AddonResolutionError(
                name, "core", f"needs Core {manifest.core} but this is Core {core_version}"
            )
        for dependency in manifest.depends:
            if dependency not in sources:
                raise AddonResolutionError(
                    name,
                    dependency,
                    f"depends on {dependency!r}, which is not enabled; add it to ADDONS "
                    "(dependencies are never enabled for you)",
                )

    ordered: list[str] = []
    done: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in done:
            return
        if name in path:
            cycle = " -> ".join((*path[path.index(name) :], name))
            raise AddonResolutionError(name, None, f"is part of a dependency cycle: {cycle}")
        for dependency in available[name].depends:
            visit(dependency, (*path, name))
        done.add(name)
        ordered.append(name)

    for name in order:
        visit(name, ())
    return tuple(ResolvedManifest(available[name], sources[name]) for name in ordered)
