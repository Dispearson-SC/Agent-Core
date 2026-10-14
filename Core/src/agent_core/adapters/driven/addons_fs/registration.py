"""Register what the enabled addons provide, through the declared extension points only.

Phase:   Phase 2 (addon system), A4, A6
Tests:   Core/tests/unit/test_addon_registration.py
Implements: no port - startup wiring (D74).

WHAT THIS MODULE DOES
    Turns the active addons (`loader.load_addons_from_environment`) into `AddonContributions`,
    which the composition root merges into the seams it already has: `TOOL_PACKAGES`,
    `collect_audit_policy`, the policy files, the profile fragments, the profile directory and
    `Settings.pull_channels`. It touches none of them itself.

THE RULE: AN ADDON CONTRIBUTES WHAT ITS MANIFEST `provides` DECLARES, AND NOTHING ELSE
    * toolsets     `provides.toolsets: [name]` -> `<addon>/toolsets/<name>.py` with a
                   `build_toolset()` (or `build_toolset(settings)`: the addon's own validated
                   settings, D84) returning a `FunctionToolset`. The module may NOT declare
                   `AUDIT_ALLOW` / `AUDIT_PRESENCE_ONLY` (audit is declared in the manifest, so a
                   reviewer reads one file); that is refused rather than ignored.
    * audit        `provides.audit.allow|presence_only` for tools of the addon's OWN toolsets,
                   through the same `collect_audit_policy` guards as a built-in package.
    * policy       `provides.policy`: one rules file. `allow` rules only for the addon's own tools
                   (a DENY or an approval requirement may narrow anything, never a grant).
    * fragments    `provides.fragments`: `<name>.md` files `include:` can name.
    * profiles     `provides.profiles`: `*.yaml` profile templates.
    * channels     `provides.channels`: pull-mode channel ids.
    * wrappers     `provides.wrappers: [{tool, wrapper}]` -> `<addon>/wrappers/<wrapper>.py` with a
                   `wrap(original, arguments, ctx)` (A6, D78). Only a tool of Core or of an addon in
                   the wrapper's dependency closure, never the addon's own; see
                   `adapters/driven/tools/wrapping.py` for what a wrapper may do.
    Every name is global: a collision between addons, or with a built-in, stops startup naming
    the addon (`AddonRegistrationError`). With no addons enabled the result is `EMPTY` and the
    caller does exactly what it did before.

ADDON CODE RUNS HERE, AND ONLY HERE
    Reading a manifest imports nothing (D70). Registering an ENABLED addon imports its toolset
    modules, by file path, once. Nothing else of an addon is ever imported.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, Final

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
from agent_core.adapters.driven.profiles_fs.fragments import AddonFragment, FragmentFile
from agent_core.adapters.driven.tools.provider import ToolsetBuilder
from agent_core.adapters.driven.tools.wrapping import ToolWrapper
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
    fragments: Mapping[str, FragmentFile] = field(default_factory=dict)
    # (addon, profile file), in activation order then file name.
    profile_files: tuple[tuple[str, Path], ...] = ()
    pull_channels: tuple[str, ...] = ()
    # (addon, profile id, slot, fragment path), in activation order: where the addons insert.
    inserts: tuple[tuple[str, str, str, FragmentFile], ...] = ()
    # addon -> names of the tools its toolsets export (what `audit` and `policy` are checked on).
    tools: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # The tool wrappers, in activation order (A6, D78).
    wrappers: tuple[ToolWrapper, ...] = ()


EMPTY: Final = AddonContributions()


def _refuse(addon: str, field_name: str, reason: str) -> AddonRegistrationError:
    return AddonRegistrationError(addon, field_name, reason)


def _package(name: str, directory: Path | None) -> ModuleType:
    """The synthetic package `name` whose modules are the files of `directory`.

    A toolset or wrapper file imports its siblings relatively (`from ._common import x`): the
    parent packages exist only so that import machinery finds them. When the same package name now
    points at another directory (another copy of the addon), everything imported under the old one
    is forgotten, so a helper is never reused from a stale copy.
    """
    existing = sys.modules.get(name)
    wanted = [] if directory is None else [str(directory)]
    if isinstance(existing, ModuleType) and getattr(existing, "__path__", None) == wanted:
        return existing
    for loaded in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
        del sys.modules[loaded]
    package = ModuleType(name)
    package.__path__ = wanted
    sys.modules[name] = package
    return package


def _import_addon_file(
    addon: ResolvedAddon, field_name: str, directory: str, name: str
) -> ModuleType:
    """Import `<addon>/<directory>/<name>.py` by path, once. Anything wrong is a refusal."""
    owner = addon.manifest.name
    path = addon.root / directory / f"{name}.py"
    if not path.is_file():
        raise _refuse(owner, field_name, f"{name!r} has no file {directory}/{name}.py")
    _package(_MODULE_PREFIX, None)
    _package(f"{_MODULE_PREFIX}.{owner}", addon.root)
    _package(f"{_MODULE_PREFIX}.{owner}.{directory}", addon.root / directory)
    module_name = f"{_MODULE_PREFIX}.{owner}.{directory}.{name.replace('-', '_')}"
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a .py file always has a loader
        raise _refuse(owner, field_name, f"{name!r} cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        del sys.modules[module_name]
        raise _refuse(
            owner, field_name, f"{name!r} failed to import: {type(error).__name__}: {error}"
        ) from error
    return module


def _import_toolset(addon: ResolvedAddon, name: str) -> ModuleType:
    return _import_addon_file(addon, "provides.toolsets", "toolsets", name)


def _bind_settings(
    owner: str,
    name: str,
    builder: Callable[..., FunctionToolset[None]],
    settings: Mapping[str, Any],
) -> ToolsetBuilder:
    """A zero-argument builder, whichever of the two shapes the module declared (D84).

    `build_toolset()` is used as it is. A builder that takes a parameter (`build_toolset(settings)`)
    is handed the addon's own validated settings; more than one required parameter is refused.
    """
    try:
        parameters = list(inspect.signature(builder).parameters.values())
    except (TypeError, ValueError):
        return builder
    positional = [
        p
        for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
    ]
    required = [
        p
        for p in parameters
        if p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    if len(required) > 1:
        raise _refuse(
            owner,
            "provides.toolsets",
            f"{name!r}: build_toolset() takes at most one parameter (the addon's settings), "
            f"but declares {len(required)} required parameters",
        )
    if not positional:
        return builder
    return partial(builder, settings)


def _identity_of(tool: Any) -> tuple[Any, ...]:
    """What the model and the runner see of a tool: description, argument schema, metadata."""
    return (
        tool.description,
        json.dumps(tool.function_schema.json_schema, sort_keys=True, default=repr),
        json.dumps(tool.metadata, sort_keys=True, default=repr),
    )


def _register_toolsets(
    addon: ResolvedAddon, known: dict[str, str], tools_owner: dict[str, str]
) -> tuple[dict[str, ToolsetBuilder], frozenset[str]]:
    owner = addon.manifest.name
    builders: dict[str, ToolsetBuilder] = {}
    own_tools: set[str] = set()
    definitions: dict[str, tuple[Any, ...]] = {}
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
        builder = _bind_settings(owner, name, builder, addon.settings)
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
        for tool, definition in built.tools.items():
            if tool in tools_owner and tools_owner[tool] != f"addon {owner!r}":
                raise _refuse(
                    owner,
                    "provides.toolsets",
                    f"tool {tool!r} of {name!r} is already provided by {tools_owner[tool]}",
                )
            identity = _identity_of(definition)
            if definitions.setdefault(tool, identity) != identity:
                raise _refuse(
                    owner,
                    "provides.toolsets",
                    f"tool {tool!r} of {name!r} differs from the same tool in another toolset "
                    "of this addon (description, arguments or replay class); policy and audit "
                    "key on the name, so one name is one definition",
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
) -> dict[str, FragmentFile]:
    relative = addon.manifest.provides.fragments
    if relative is None:
        return {}
    owner = addon.manifest.name
    found: dict[str, FragmentFile] = {}
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
        try:
            # A template the settings cannot fill stops here, naming the addon and the file.
            text = AddonFragment(path, addon.settings).read_text(encoding="utf-8")
        except (ValueError, OSError) as error:
            raise _refuse(owner, "provides.fragments", f"{path.name}: {error}") from error
        found[path.stem] = _CheckedFragment(path, text)
    return found


@dataclass(frozen=True)
class _CheckedFragment:
    """A fragment as registration rendered and checked it (slice 29 advisory).

    Serving the checked text, not the file, means a fragment edited or removed after startup can
    neither reach a profile unchecked nor fail later with an error that names no addon.
    """

    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    def is_file(self) -> bool:
        return True

    def read_text(self, encoding: str | None = None) -> str:
        return self.text


def _closure(name: str, manifests: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """Every addon `name` depends on, directly or through another (not `name` itself)."""
    seen: set[str] = set()
    pending = list(manifests.get(name, ()))
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(manifests.get(current, ()))
    return frozenset(seen)


def _wrappers_of(
    addon: ResolvedAddon,
    own: Mapping[str, frozenset[str]],
    depends: Mapping[str, Sequence[str]],
    core_tools: Collection[str],
) -> tuple[ToolWrapper, ...]:
    """The addon's `provides.wrappers`, resolved. A wrapper reaches Core's tools and the tools of
    the addons it depends on (directly or not); nothing else, and not its own."""
    owner = addon.manifest.name
    closure = _closure(owner, depends)
    found: list[ToolWrapper] = []
    for declared in addon.manifest.provides.wrappers:
        tool = declared.tool
        if tool in own[owner]:
            raise _refuse(
                owner,
                "provides.wrappers",
                f"{tool!r} is a tool of this addon's own toolsets; an addon wraps the tools of "
                "Core and of the addons it depends on, not its own",
            )
        if tool not in core_tools:
            providers = sorted(name for name, tools in own.items() if tool in tools)
            if not providers:
                raise _refuse(
                    owner, "provides.wrappers", f"{tool!r} is not a tool any enabled toolset has"
                )
            if not any(name in closure for name in providers):
                raise _refuse(
                    owner,
                    "provides.wrappers",
                    f"{tool!r} belongs to addon {providers[0]!r}, which this addon does not "
                    "depend on (directly or through another dependency); add it to depends",
                )
        module = _import_addon_file(addon, "provides.wrappers", "wrappers", declared.wrapper)
        function = getattr(module, "wrap", None)
        if not callable(function):
            raise _refuse(
                owner,
                "provides.wrappers",
                f"{declared.wrapper!r} has no wrap(original, arguments, ctx) function",
            )
        found.append(ToolWrapper(addon=owner, tool=tool, call=function))
    return tuple(found)


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
    fragments: dict[str, FragmentFile] = {}
    profile_files: list[tuple[str, Path]] = []
    channels: list[str] = []
    inserts: list[tuple[str, str, str, FragmentFile]] = []
    wrappers: list[ToolWrapper] = []
    depends = {a.manifest.name: a.manifest.depends for a in addons}
    core_tools = frozenset(tool for tool, who in tool_owner.items() if who == "Core")
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
        own_fragments = _fragments_of(addon, builtin_fragments_dir, fragment_owner)
        fragments.update(own_fragments)
        for insertion in addon.manifest.provides.inserts:
            if insertion.fragment not in own_fragments:
                raise _refuse(
                    owner,
                    "provides.inserts",
                    f"fragment {insertion.fragment!r} is not one of the addon's own "
                    "(provides.fragments); an addon inserts only its own text",
                )
            inserts.append(
                (owner, insertion.profile, insertion.slot, own_fragments[insertion.fragment])
            )
        wrappers.extend(_wrappers_of(addon, own, depends, core_tools))
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
        inserts=tuple(inserts),
        tools=own,
        wrappers=tuple(wrappers),
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
