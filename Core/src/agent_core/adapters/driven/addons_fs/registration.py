"""Register what the enabled addons provide, through the declared extension points only.

Phase:   Phase 2 (addon system), A4
Tests:   Core/tests/unit/test_addon_registration.py
Implements: no port - startup wiring (D74).

WHAT THIS MODULE DOES
    Turns the active addons (`loader.load_addons_from_environment`) into `AddonContributions`,
    which the composition root merges into the seams it already has: `TOOL_PACKAGES`,
    `collect_audit_policy`, the policy files, the profile fragments, the profile directory and
    `Settings.pull_channels`. It touches none of them itself.

THE RULE: AN ADDON CONTRIBUTES WHAT ITS MANIFEST `provides` DECLARES, AND NOTHING ELSE
    * toolsets     `provides.toolsets: [name]` -> `<addon>/toolsets/<name>.py` with a zero-argument
                   `build_toolset()` returning a `FunctionToolset`. The module may NOT declare
                   `AUDIT_ALLOW` / `AUDIT_PRESENCE_ONLY` (audit is declared in the manifest, so a
                   reviewer reads one file); that is refused rather than ignored.
    * audit        `provides.audit.allow|presence_only` for tools of the addon's OWN toolsets,
                   through the same `collect_audit_policy` guards as a built-in package.
    * policy       `provides.policy`: one rules file. `allow` rules only for the addon's own tools
                   (a DENY or an approval requirement may narrow anything, never a grant).
    * fragments    `provides.fragments`: `<name>.md` files `include:` can name.
    * profiles     `provides.profiles`: `*.yaml` profile templates.
    * channels     `provides.channels`: pull-mode channel ids.
    Every name is global: a collision between addons, or with a built-in, stops startup naming
    the addon (`AddonRegistrationError`). With no addons enabled the result is `EMPTY` and the
    caller does exactly what it did before.

ADDON CODE RUNS HERE, AND ONLY HERE
    Reading a manifest imports nothing (D70). Registering an ENABLED addon imports its toolset
    modules, by file path, once. Nothing else of an addon is ever imported.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Final

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError
from agent_core.adapters.driven.addons_fs.loader import ResolvedAddon
from agent_core.adapters.driven.persistence_pg.audit_repository import (
    AuditContribution,
    AuditPolicyConflictError,
    collect_audit_policy,
)
from agent_core.adapters.driven.policy_fs.loader import parse_policy_rules
from agent_core.adapters.driven.tools.provider import ToolsetBuilder
from agent_core.domain.policy import Effect, PolicyRule

__all__ = [
    "EMPTY",
    "AddonContributions",
    "collect_audit_for",
    "register_addons",
]

_MODULE_PREFIX: Final = "agent_core_addons"
_AUDIT_ATTRIBUTES: Final = ("AUDIT_ALLOW", "AUDIT_PRESENCE_ONLY")


@dataclass(frozen=True)
class AddonContributions:
    """What the active addons add to the root. Every field is empty for no addons."""

    toolsets: Mapping[str, ToolsetBuilder] = field(default_factory=dict)
    audit: Mapping[str, AuditContribution] = field(default_factory=dict)
    policy_rules: tuple[PolicyRule, ...] = ()
    fragments: Mapping[str, Path] = field(default_factory=dict)
    # (addon, profile file), in activation order then file name.
    profile_files: tuple[tuple[str, Path], ...] = ()
    pull_channels: tuple[str, ...] = ()
    # addon -> names of the tools its toolsets export (what `audit` and `policy` are checked on).
    tools: Mapping[str, frozenset[str]] = field(default_factory=dict)


EMPTY: Final = AddonContributions()


def _refuse(addon: str, field_name: str, reason: str) -> AddonRegistrationError:
    return AddonRegistrationError(addon, field_name, reason)


def _import_toolset(addon: ResolvedAddon, name: str) -> ModuleType:
    owner = addon.manifest.name
    path = addon.root / "toolsets" / f"{name}.py"
    if not path.is_file():
        raise _refuse(owner, "provides.toolsets", f"{name!r} has no file toolsets/{name}.py")
    module_name = f"{_MODULE_PREFIX}.{owner}.{name.replace('-', '_')}"
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a .py file always has a loader
        raise _refuse(owner, "provides.toolsets", f"{name!r} cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        del sys.modules[module_name]
        raise _refuse(
            owner,
            "provides.toolsets",
            f"{name!r} failed to import: {type(error).__name__}: {error}",
        ) from error
    return module


def _register_toolsets(
    addon: ResolvedAddon, known: dict[str, str], tools_owner: dict[str, str]
) -> tuple[dict[str, ToolsetBuilder], frozenset[str]]:
    owner = addon.manifest.name
    builders: dict[str, ToolsetBuilder] = {}
    own_tools: set[str] = set()
    for name in addon.manifest.provides.toolsets:
        if name in known:
            raise _refuse(
                owner, "provides.toolsets", f"{name!r} is already provided by {known[name]}"
            )
        module = _import_toolset(addon, name)
        for attribute in _AUDIT_ATTRIBUTES:
            if hasattr(module, attribute):
                raise _refuse(
                    owner,
                    "provides.toolsets",
                    f"{name!r} declares {attribute}; declare audit in addon.yaml provides.audit",
                )
        builder = getattr(module, "build_toolset", None)
        if not callable(builder):
            raise _refuse(owner, "provides.toolsets", f"{name!r} has no build_toolset()")
        try:
            built = builder()
        except Exception as error:
            raise _refuse(
                owner,
                "provides.toolsets",
                f"{name!r}: build_toolset() raised {type(error).__name__}: {error}",
            ) from error
        if not isinstance(built, FunctionToolset):
            raise _refuse(
                owner,
                "provides.toolsets",
                f"{name!r}: build_toolset() must return a FunctionToolset",
            )
        for tool in built.tools:
            if tool in tools_owner:
                raise _refuse(
                    owner,
                    "provides.toolsets",
                    f"tool {tool!r} of {name!r} is already provided by {tools_owner[tool]}",
                )
            tools_owner[tool] = f"addon {owner!r}"
            own_tools.add(tool)
        known[name] = f"addon {owner!r}"
        builders[name] = builder
    return builders, frozenset(own_tools)


def _audit_of(addon: ResolvedAddon, own_tools: frozenset[str]) -> AuditContribution | None:
    audit = addon.manifest.provides.audit
    declared = {**audit.allow, **audit.presence_only}
    for tool in declared:
        if tool not in own_tools:
            raise _refuse(
                addon.manifest.name,
                "provides.audit",
                f"{tool!r} is not a tool of this addon's toolsets; an addon audits only its own",
            )
    if not declared:
        return None
    return (
        {tool: frozenset(fields) for tool, fields in audit.allow.items()},
        {tool: frozenset(fields) for tool, fields in audit.presence_only.items()},
    )


def _matches(pattern: str, tool: str) -> bool:
    pattern, tool = pattern.lower(), tool.lower()
    return tool.startswith(pattern[:-1]) if pattern.endswith("*") else tool == pattern


def _policy_of(
    addon: ResolvedAddon,
    own_tools: frozenset[str],
    foreign_tools: Collection[str],
    taken: dict[str, str],
) -> tuple[PolicyRule, ...]:
    relative = addon.manifest.provides.policy
    if relative is None:
        return ()
    owner = addon.manifest.name
    try:
        rules = parse_policy_rules(
            (addon.root / relative).read_text(encoding="utf-8"), source=relative
        )
    except (ValueError, yaml.YAMLError, OSError) as error:
        raise _refuse(owner, "provides.policy", str(error)) from error
    for rule in rules:
        if rule.rule_id in taken:
            raise _refuse(
                owner,
                "provides.policy",
                f"rule_id {rule.rule_id!r} is already declared by {taken[rule.rule_id]}",
            )
        taken[rule.rule_id] = f"addon {owner!r}"
        if rule.effect is not Effect.ALLOW:
            continue  # a DENY or an approval requirement only narrows what is permitted
        if not any(_matches(rule.tool_pattern, tool) for tool in own_tools):
            raise _refuse(
                owner,
                "provides.policy",
                f"rule {rule.rule_id!r} allows {rule.tool_pattern!r}, which is none of the "
                "addon's own tools; an addon grants only its own",
            )
        if any(_matches(rule.tool_pattern, tool) for tool in foreign_tools):
            raise _refuse(
                owner,
                "provides.policy",
                f"rule {rule.rule_id!r} allows {rule.tool_pattern!r}, which also reaches a tool "
                "the addon does not own; an addon grants only its own",
            )
    return rules


def _declared_files(
    addon: ResolvedAddon, field_name: str, relative: str, pattern: str
) -> list[Path]:
    """The `pattern` files of a declared directory; a missing or empty one is refused.

    The loader checked the directory when the manifest was read; registration does not trust that
    it still holds, and a declaration that yields nothing is a mistake to name, not an empty
    contribution to ship.
    """
    directory = addon.root / relative
    if not directory.is_dir():
        raise _refuse(
            addon.manifest.name, field_name, f"{relative!r} does not exist in the addon directory"
        )
    found = sorted(directory.glob(pattern))
    if not found:
        raise _refuse(addon.manifest.name, field_name, f"{relative!r} declares no {pattern} files")
    return found


def _fragments_of(
    addon: ResolvedAddon, builtin_dir: Path | None, taken: dict[str, str]
) -> dict[str, Path]:
    relative = addon.manifest.provides.fragments
    if relative is None:
        return {}
    owner = addon.manifest.name
    found: dict[str, Path] = {}
    for path in _declared_files(addon, "provides.fragments", relative, "*.md"):
        if path.stem in taken:
            raise _refuse(
                owner,
                "provides.fragments",
                f"{path.stem!r} is already provided by {taken[path.stem]}",
            )
        if builtin_dir is not None and (builtin_dir / path.name).is_file():
            raise _refuse(
                owner, "provides.fragments", f"{path.stem!r} is already a built-in fragment"
            )
        taken[path.stem] = f"addon {owner!r}"
        found[path.stem] = path
    return found


def _channels_of(
    addon: ResolvedAddon, reserved: Collection[str], taken: dict[str, str]
) -> tuple[str, ...]:
    owner = addon.manifest.name
    for name in addon.manifest.provides.channels:
        if name in reserved:
            raise _refuse(owner, "provides.channels", f"{name!r} is already a channel of Core")
        if name in taken:
            raise _refuse(
                owner, "provides.channels", f"{name!r} is already provided by {taken[name]}"
            )
        taken[name] = f"addon {owner!r}"
    return tuple(addon.manifest.provides.channels)


def register_addons(
    addons: Sequence[ResolvedAddon],
    *,
    builtin_toolsets: Mapping[str, ToolsetBuilder],
    builtin_fragments_dir: Path | None,
    reserved_channels: Collection[str],
    builtin_rule_ids: Collection[str] = (),
    manages_policy_files: bool = True,
) -> AddonContributions:
    """What `addons` contribute, validated. `EMPTY` when there are none. Raises `AddonError`s.

    `reserved_channels` are the ids Core (and the operator's `AGENT_CORE_PULL_CHANNELS`) already
    own; `builtin_rule_ids` the rules the deployment's own policy files declare;
    `manages_policy_files` says whether the deployment reconciles policy from files at all (an
    addon's rules need that: reconciling would otherwise delete the rules kept in the database).
    """
    if not addons:
        return EMPTY

    toolset_owner: dict[str, str] = {name: "Core" for name in builtin_toolsets}
    tool_owner: dict[str, str] = {
        tool: "Core" for builder in builtin_toolsets.values() for tool in builder().tools
    }
    rule_owner: dict[str, str] = {rule_id: "Core's policy files" for rule_id in builtin_rule_ids}
    fragment_owner: dict[str, str] = {}
    channel_owner: dict[str, str] = {}

    toolsets: dict[str, ToolsetBuilder] = {}
    own: dict[str, frozenset[str]] = {}
    for addon in addons:
        builders, own_tools = _register_toolsets(addon, toolset_owner, tool_owner)
        toolsets.update(builders)
        own[addon.manifest.name] = own_tools

    audit: dict[str, AuditContribution] = {}
    rules: list[PolicyRule] = []
    fragments: dict[str, Path] = {}
    profile_files: list[tuple[str, Path]] = []
    channels: list[str] = []
    for addon in addons:
        owner = addon.manifest.name
        contribution = _audit_of(addon, own[owner])
        if contribution is not None:
            audit[owner] = contribution
        foreign = [tool for tool in tool_owner if tool not in own[owner]]
        declared = _policy_of(addon, own[owner], foreign, rule_owner)
        if declared and not manages_policy_files:
            raise _refuse(
                owner,
                "provides.policy",
                "contributes rules but this deployment does not manage policy from files "
                "(no policy directory), so reconciling them would delete the rules kept in "
                "the database; create the policy directory or remove the addon's policy",
            )
        rules.extend(declared)
        fragments.update(_fragments_of(addon, builtin_fragments_dir, fragment_owner))
        channels.extend(_channels_of(addon, reserved_channels, channel_owner))
        relative = addon.manifest.provides.profiles
        if relative is not None:
            for path in _declared_files(addon, "provides.profiles", relative, "*.yaml"):
                profile_files.append((owner, path))

    return AddonContributions(
        toolsets=toolsets,
        audit=audit,
        policy_rules=tuple(rules),
        fragments=fragments,
        profile_files=tuple(profile_files),
        pull_channels=tuple(channels),
        tools=own,
    )


def collect_audit_for(
    packages: Mapping[str, ToolsetBuilder], contributions: AddonContributions
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """`collect_audit_policy` over the built-in packages plus the addons' manifest declarations.

    A conflict that an addon caused is an `AddonRegistrationError` naming that addon; one between
    built-ins stays the `ValueError` it always was.
    """
    try:
        return collect_audit_policy(packages, contributions=contributions.audit)
    except AuditPolicyConflictError as error:
        for source in error.sources:
            if source in contributions.audit:
                raise _refuse(source, "provides.audit", str(error)) from error
        raise
