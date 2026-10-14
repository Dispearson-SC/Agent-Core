"""Per-addon settings: a declarative schema, validated values, nothing else to edit (A3).

Phase:   Phase 2 (addon system)
Tests:   Core/tests/unit/test_addon_settings.py
Implements: no port - startup wiring (D72).

THE SCHEMA IS DATA IN THE MANIFEST
    `settings:` in `addon.yaml` declares every key an addon accepts. Each entry is one of:

        n1_max_cost: 150                      # a scalar: that type, that default
        tiers:                                # a mapping without `type`: a group of settings
          n1_max_cost: 150
        threshold:                            # an explicit field
          type: int                           # str | int | float | bool | list | object
          default: 5                          # no default means the deployment must set it
          minimum: 1                          # int/float only (also `maximum`)
          choices: [1, 5, 10]                 # str/int only
          description: ...
        tags: {type: list, items: str, default: []}
        limits: {type: object, fields: {...}}   # an explicit group (use it for a key named `type`)

    Values are validated STRICTLY (a string is not a number, `true` is not 1), at startup, naming
    the addon and the key, and never echoing the value: a setting can be a secret.

WHERE VALUES COME FROM, LOWEST TO HIGHEST
    the manifest default, the deployment file (`AGENT_CORE_ADDONS_CONFIG`: `addon: {key: value}`),
    `AGENT_CORE_ADDON__<ADDON>__<KEY>[__<KEY>]` environment variables (upper case; a double
    underscore separates segments, which is why neither an addon name nor a key may contain one).
    A value for an addon that is not active, or for a key the schema does not declare, is an error:
    a typo must never configure nothing silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, cast

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml
from pydantic import Field, StrictBool, TypeAdapter, ValidationError

from agent_core.adapters.driven.addons_fs.errors import AddonSettingsError, ManifestError
from agent_core.adapters.driven.addons_fs.manifest import AddonManifest

__all__ = ["ENV_PREFIX", "read_addons_config", "resolve_settings"]

ENV_PREFIX: Final = "AGENT_CORE_ADDON__"

_KEY: Final = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_SCALARS: Final = ("str", "int", "float", "bool")
_TYPES: Final = (*_SCALARS, "list", "object")
_SPEC_KEYS: Final = {
    "type", "default", "description", "minimum", "maximum", "choices", "items", "fields",
}  # fmt: skip
_MISSING: Final = object()


@dataclass(frozen=True)
class _Leaf:
    kind: str  # str | int | float | bool | list
    item_kind: str | None
    adapter: TypeAdapter[Any]
    default: Any  # _MISSING when the deployment must supply it


@dataclass(frozen=True)
class _Group:
    children: Mapping[str, _Leaf | _Group]


_Node = _Leaf | _Group


# --- compiling the schema (a problem here is the manifest's) -----------------------------------


def _dotted(*parts: str | int) -> str:
    return ".".join(str(p) for p in parts if p != "")


def _scalar_type(kind: str, minimum: Any, maximum: Any, choices: Sequence[Any] | None) -> Any:
    if choices is not None:
        return Literal[tuple(choices)]
    if kind == "str":
        return Annotated[str, Field(strict=True)]
    if kind == "int":
        return Annotated[int, Field(strict=True, ge=minimum, le=maximum)]
    if kind == "float":
        return Annotated[float, Field(strict=True, ge=minimum, le=maximum)]
    return StrictBool


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _compile_entry(addon: str, path: tuple[str, ...], value: Any) -> _Node:
    def bad(field: str, reason: str) -> ManifestError:
        return ManifestError(addon, _dotted("settings", *path, field), reason)

    if isinstance(value, bool | int | float | str):
        kind = "bool" if isinstance(value, bool) else type(value).__name__
        leaf = _Leaf(kind, None, TypeAdapter(_scalar_type(kind, None, None, None)), value)
        return leaf
    if not isinstance(value, dict):
        raise bad("", "must be a default value (str, int, float, bool) or a mapping of settings")
    if "type" not in value:
        return _compile_group(addon, path, value)

    for key in value:
        if key not in _SPEC_KEYS:
            raise bad(str(key), "is not a setting-schema key")
    kind = value["type"]
    if kind not in _TYPES:
        raise bad("type", f"must be one of {', '.join(_TYPES)}")
    if kind == "object":
        extra = [k for k in value if k not in ("type", "fields", "description")]
        if extra:
            raise bad(str(extra[0]), "does not apply to an object; declare it inside 'fields'")
        fields = value.get("fields")
        if not isinstance(fields, dict):
            raise bad("fields", "an object needs 'fields': a mapping of settings")
        return _compile_group(addon, path, fields)

    item_kind: str | None = None
    if kind == "list":
        item_kind = value.get("items")
        if item_kind not in _SCALARS:
            raise bad("items", f"a list needs 'items': one of {', '.join(_SCALARS)}")
        for key in ("minimum", "maximum", "choices"):
            if key in value:
                raise bad(key, "does not apply to a list")
    minimum, maximum = value.get("minimum"), value.get("maximum")
    for key, bound in (("minimum", minimum), ("maximum", maximum)):
        if key in value and (kind not in ("int", "float") or not _number(bound)):
            raise bad(key, "applies to an int or float setting and must be a number")
    choices = value.get("choices")
    if "choices" in value:
        if kind not in ("str", "int") or not isinstance(choices, list) or not choices:
            raise bad("choices", "applies to a str or int setting and must be a non-empty list")
        element = TypeAdapter(_scalar_type(kind, None, None, None))
        for choice in choices:
            try:
                element.validate_python(choice)
            except ValidationError:
                raise bad("choices", f"every choice must be a {kind}") from None
            # A `Literal` cannot carry bounds, so both constraints are held by refusing a choice
            # the bounds would reject: no declared bound is silently dropped (D73).
            if kind == "int" and (
                (minimum is not None and choice < minimum)
                or (maximum is not None and choice > maximum)
            ):
                raise bad("choices", f"every choice must lie within minimum and maximum: {choice}")

    if kind == "list":
        assert item_kind is not None
        annotation: Any = Annotated[
            list[_scalar_type(item_kind, None, None, None)],  # type: ignore[misc]
            Field(strict=True),
        ]
    else:
        annotation = _scalar_type(kind, minimum, maximum, choices)
    adapter: TypeAdapter[Any] = TypeAdapter(annotation)
    default = value.get("default", _MISSING)
    if default is not _MISSING:
        try:
            default = adapter.validate_python(default)
        except ValidationError as error:
            raise bad("default", _first_message(error)) from None
    return _Leaf(kind, item_kind, adapter, default)


def _compile_group(addon: str, path: tuple[str, ...], mapping: Mapping[Any, Any]) -> _Group:
    children: dict[str, _Node] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not _KEY.match(key):
            raise ManifestError(
                addon,
                _dotted("settings", *path, str(key)),
                "must be lower case words joined by single underscores",
            )
        children[key] = _compile_entry(addon, (*path, key), value)
    return _Group(children)


def _first_message(error: ValidationError) -> str:
    first = error.errors(include_input=False, include_url=False)[0]
    return str(first["msg"]).removeprefix("Value error, ")


# --- resolving values --------------------------------------------------------------------------


def _parse_scalar(kind: str, text: str) -> Any:
    """An environment string as the scalar the schema asks for; `ValueError` when it is not."""
    if kind == "str":
        return text
    if kind == "bool":
        if text in ("true", "false"):
            return text == "true"
        raise ValueError("must be true or false")
    try:
        return int(text) if kind == "int" else float(text)
    except ValueError:
        what = "a whole number" if kind == "int" else "a number"
        raise ValueError(f"must be {what}") from None


def _parse_env(leaf: _Leaf, text: str) -> Any:
    if leaf.kind != "list":
        return _parse_scalar(leaf.kind, text.strip() if leaf.kind != "str" else text)
    assert leaf.item_kind is not None
    if not text.strip():
        return []
    return [_parse_scalar(leaf.item_kind, part.strip()) for part in text.split(",")]


def _env_overrides(
    trees: Mapping[str, _Group], env: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for variable in sorted(env):
        if not variable.startswith(ENV_PREFIX):
            continue
        parts = variable[len(ENV_PREFIX) :].split("__")
        addon = parts[0].lower()
        if addon not in trees:
            raise AddonSettingsError(
                addon, None, f"has {variable} set but is not enabled; remove it or enable the addon"
            )
        keys = [part.lower() for part in parts[1:]]
        if not keys or "" in keys:
            raise AddonSettingsError(addon, None, f"{variable} does not name a setting")
        node: _Node = trees[addon]
        trail: list[str] = []
        for key in keys:
            trail.append(key)
            if not isinstance(node, _Group) or key not in node.children:
                raise AddonSettingsError(
                    addon, _dotted(*trail), f"is not a setting of this addon (from {variable})"
                )
            node = node.children[key]
        if isinstance(node, _Group):
            raise AddonSettingsError(
                addon, _dotted(*trail), f"is a group of settings; set its keys ({variable})"
            )
        try:
            value = _parse_env(node, env[variable])
        except ValueError as problem:
            raise AddonSettingsError(addon, _dotted(*trail), f"{variable} {problem}") from None
        target = found.setdefault(addon, {})
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return found


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(value)
    return value


def _merge(
    addon: str,
    group: _Group,
    configured: Any,
    from_env: Mapping[str, Any],
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not isinstance(configured, Mapping):
        raise AddonSettingsError(addon, _dotted(*path) or None, "must be a mapping of settings")
    for key in configured:
        if key not in group.children:
            raise AddonSettingsError(
                addon, _dotted(*path, str(key)), "is not a setting of this addon"
            )
    values: dict[str, Any] = {}
    for key, node in group.children.items():
        here = (*path, key)
        if isinstance(node, _Group):
            values[key] = _merge(addon, node, configured.get(key, {}), from_env.get(key, {}), here)
            continue
        if key in from_env:
            raw = from_env[key]
        elif key in configured:
            raw = configured[key]
        elif node.default is not _MISSING:
            values[key] = node.default
            continue
        else:
            raise AddonSettingsError(addon, _dotted(*here), "is required and has no default")
        try:
            values[key] = node.adapter.validate_python(raw)
        except ValidationError as error:
            first = error.errors(include_input=False, include_url=False)[0]
            where = _dotted(*here, *(str(p) for p in first["loc"]))
            raise AddonSettingsError(addon, where, _first_message(error)) from None
    return values


def resolve_settings(
    manifests: Sequence[AddonManifest],
    *,
    config: Mapping[str, Any],
    env: Mapping[str, str],
) -> dict[str, Mapping[str, Any]]:
    """The validated settings of each active addon, by addon name, read-only.

    `config` is the parsed deployment file, `env` the environment. Raises `ManifestError` for a bad
    schema and `AddonSettingsError` for a bad value, naming the addon and the key.
    """
    trees = {m.name: _compile_group(m.name, (), m.settings) for m in manifests}
    for name in sorted(config):
        if name not in trees:
            raise AddonSettingsError(
                str(name), None, "has settings in the deployment file but is not enabled"
            )
    from_env = _env_overrides(trees, env)
    return {
        name: cast(
            "Mapping[str, Any]",
            _freeze(_merge(name, tree, config.get(name, {}), from_env.get(name, {}))),
        )
        for name, tree in trees.items()
    }


def read_addons_config(path: Path) -> dict[str, Any]:
    """The deployment file: `addon_name: {setting: value}`. A missing or malformed file is an
    error - a path the operator named and that is not there must not mean "use the defaults"."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        reason = error.strerror or "unreadable"
        message = f"cannot read the addons file {path}: {reason}"
        raise AddonSettingsError(None, None, message) from None
    except yaml.YAMLError:
        raise AddonSettingsError(None, None, f"the addons file {path} is not valid YAML") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise AddonSettingsError(
            None, None, f"the addons file {path} must map each addon name to its settings"
        )
    return cast("dict[str, Any]", raw)
