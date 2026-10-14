"""The `addon.yaml` manifest: schema, parsing and validation (Phase 2, A1; roadmap 5.3).

Phase:   Phase 2 (addon system)
Tests:   Core/tests/unit/test_addon_manifest.py
Implements: no port - the composition root consumes it at startup (D70).

WHAT THIS MODULE DOES NOT DO
    It validates the SHAPE of a manifest and nothing else: it imports no addon code, checks no
    file the manifest points at, and knows nothing about other addons. Resolution (dependencies,
    Core version, bridges) is `resolver.py`; the `settings` block is only checked to be a mapping
    here and compiled by `settings.py`.

AUDIT ENTRIES ARE A MAPPING, NOT A LIST
    The roadmap example writes `audit.allow: [propose_action, decide]`. The seam this feeds,
    `collect_audit_policy`, takes `tool name -> argument names`, and a bare list cannot say which
    arguments of which tool. The manifest therefore takes the seam's own shape (D70).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from agent_core.adapters.driven.addons_fs.errors import ManifestError

__all__ = [
    "MANIFEST_FILENAME",
    "AddonManifest",
    "AuditProvides",
    "Hooks",
    "Provides",
    "ToolWrapperDecl",
    "load_manifest",
    "parse_manifest",
]

MANIFEST_FILENAME: Final = "addon.yaml"

# `a`, `a_b`, `a1_b2`: lower case, single underscores. A double underscore is the separator of the
# environment variable that carries an addon's settings (`settings.py`), so a name may not hold one.
_NAME: Final = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_SLUG: Final = re.compile(r"^[a-z][a-z0-9_-]*$")
_IDENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOOK: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


def _matches(pattern: re.Pattern[str], what: str) -> Any:
    def check(value: str) -> str:
        if not pattern.match(value):
            raise ValueError(f"must be {what}")
        return value

    return AfterValidator(check)


def _relative(value: str) -> str:
    """A path inside the addon's own directory: relative, no `..`, no backslash."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or "\0" in value:
        raise ValueError("must be a relative path inside the addon directory")
    return value


Name = Annotated[str, _matches(_NAME, "lower case words joined by single underscores")]
Slug = Annotated[str, _matches(_SLUG, "lower case letters, digits, '_' or '-', from a letter")]
Ident = Annotated[str, _matches(_IDENT, "a plain identifier")]
# `*` as the profile of an insert means every profile that declares the slot (D89).
InsertTarget = Annotated[
    str,
    _matches(
        re.compile(r"^(\*|[A-Za-z_][A-Za-z0-9_]*)$"), "a profile id, or '*' for every profile"
    ),
]
FragmentName = Annotated[
    str, _matches(re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"), "a fragment file stem")
]
HookRef = Annotated[str, _matches(_HOOK, "'module.path:function'")]
RelPath = Annotated[str, AfterValidator(_relative)]


class _Strict(BaseModel):
    """Unknown keys are errors: a typo in a manifest must not silently drop an extension point."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditProvides(_Strict):
    """`tool name -> argument names`, the shape `collect_audit_policy` already takes."""

    allow: dict[Ident, tuple[Ident, ...]] = Field(default_factory=dict)
    presence_only: dict[Ident, tuple[Ident, ...]] = Field(default_factory=dict)


class ProfileInsertion(_Strict):
    """`provides.inserts` entry: put one of the addon's fragments at a named slot of a profile."""

    profile: InsertTarget
    slot: Ident
    fragment: FragmentName


class ToolWrapperDecl(_Strict):
    """`provides.wrappers` entry: wrap `tool` with `wrappers/<wrapper>.py` of the addon (A6)."""

    tool: Ident
    wrapper: Slug


class Provides(_Strict):
    toolsets: tuple[Slug, ...] = ()
    profiles: RelPath | None = None
    fragments: RelPath | None = None
    policy: RelPath | None = None
    audit: AuditProvides = Field(default_factory=AuditProvides)
    channels: tuple[Slug, ...] = ()
    inserts: tuple[ProfileInsertion, ...] = ()
    wrappers: tuple[ToolWrapperDecl, ...] = ()

    @field_validator("wrappers")
    @classmethod
    def _wrappers_once(cls, value: tuple[ToolWrapperDecl, ...]) -> tuple[ToolWrapperDecl, ...]:
        pairs = [(entry.tool, entry.wrapper) for entry in value]
        if len(set(pairs)) != len(pairs):
            raise ValueError("lists the same tool and wrapper twice")
        return value


class Hooks(_Strict):
    post_init: HookRef | None = None


class AddonManifest(_Strict):
    name: Name
    version: str
    core: str
    depends: tuple[Name, ...] = ()
    auto_install: StrictBool = False
    provides: Provides = Field(default_factory=Provides)
    settings: dict[str, Any] = Field(default_factory=dict)
    hooks: Hooks = Field(default_factory=Hooks)
    migrations: RelPath | None = None

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion:
            raise ValueError("must be a version such as 1.2.0") from None
        return value

    @field_validator("core")
    @classmethod
    def _core(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier:
            raise ValueError("must be a version range such as '>=1.0,<2'") from None
        return value

    @field_validator("depends")
    @classmethod
    def _depends(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("lists an addon twice")
        if info.data.get("name") in value:
            raise ValueError("an addon cannot depend on itself")
        return value

    @field_validator("auto_install")
    @classmethod
    def _bridge_has_dependencies(cls, value: bool, info: ValidationInfo) -> bool:
        # A bridge activates when ALL its dependencies are enabled; with none, that is always.
        if value and "depends" in info.data and not info.data["depends"]:
            raise ValueError("a bridge (auto_install) must depend on at least one addon")
        return value

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)

    @property
    def core_spec(self) -> SpecifierSet:
        return SpecifierSet(self.core)


def _field_path(location: tuple[int | str, ...]) -> str | None:
    return ".".join(str(part) for part in location) or None


def parse_manifest(
    text: str, *, source: str = MANIFEST_FILENAME, addon: str | None = None
) -> AddonManifest:
    """Parse one manifest document. Raises `ManifestError` naming the addon and the field.

    `source` says where the text came from; `addon` is the label to use when the document does not
    carry a usable name (the directory it was found in). `safe_load`, never `load`: a manifest is
    configuration, and it must not be able to construct Python objects.
    """

    def refuse(label: str | None, field: str | None, reason: str) -> ManifestError:
        suffix = "" if label else f" (read from {source})"
        return ManifestError(label, field, reason + suffix)

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        line = getattr(getattr(error, "problem_mark", None), "line", None)
        where = f" at line {line + 1}" if line is not None else ""
        problem = getattr(error, "problem", "")
        raise refuse(addon, None, f"not valid YAML{where}: {problem}") from None
    if not isinstance(raw, dict):
        raise refuse(addon, None, "must be a mapping of keys, as in the authoring guide")

    peeked = raw.get("name")
    label = peeked if isinstance(peeked, str) and _NAME.match(peeked) else addon
    try:
        return AddonManifest.model_validate(raw)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False)[0]
        field = _field_path(first["loc"])
        reason = first["msg"].removeprefix("Value error, ")
        if first["type"] == "missing":
            reason = "is required"
        elif first["type"] == "extra_forbidden":
            reason = "is not a manifest key"
        raise refuse(None if field == "name" else label, field, reason) from None


def load_manifest(directory: Path) -> AddonManifest:
    """Read `<directory>/addon.yaml`. The directory must carry the addon's name."""
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError(directory.name, None, f"no {MANIFEST_FILENAME} in {directory}")
    text = path.read_text(encoding="utf-8")
    manifest = parse_manifest(text, source=str(path), addon=directory.name)
    if manifest.name != directory.name:
        raise ManifestError(
            manifest.name,
            "name",
            f"is {manifest.name!r} but its directory {directory.name!r} must carry that name",
        )
    return manifest
