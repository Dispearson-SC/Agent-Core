"""Read a backend's OpenAPI document into the bridge manifest (Phase 3, G1b, D85).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_openapi.py
Implements: no port - a second source for `BridgeManifest`, next to the YAML file.

THE IDEA
    The backend already describes its internal API in OpenAPI. It marks the operations an agent may
    call with `x-agent-tool: true`; this module turns exactly those into the same `BridgeManifest`
    the YAML loader builds (through `parse_bridge_manifest`), so the builder, the client and the
    replay rules are unchanged. An operation that is not marked is invisible to the model.

THE MARKS (operation level)
    x-agent-tool         true            makes the operation a tool (a non-boolean is an error)
    x-agent-effect       read | write    default: GET -> read, any other method -> write
    x-agent-name         tool name       default: operationId (must match ^[a-z][a-z0-9_]*$)
    x-agent-description  text            default: summary, then description
    x-agent-fields       [key, ...]      the model-visible projection (manifest `response.pick`)
    x-agent-rows-key     key             rows live under this key of the reply
    x-agent-row-limit    int             rows returned (default: the `default_row_limit` setting)
    x-agent-replay       safe|interrupted
    x-agent-context      [session|agent|tenant|subject, ...]  which turn identities become headers
    x-agent-warning      bool            surface the service warning header
    x-agent-idempotency  [arg, ...]|true arguments that key the Idempotency-Key of a write

ARGUMENTS
    `parameters` (path-item level, then operation level) and a flat JSON object `requestBody`
    become typed arguments (string, integer, number, boolean, array, object; enum, minimum, maximum,
    default, required). Header parameters are skipped (the bridge sends identity headers; the model
    never does); a cookie parameter, a `$ref` that is not local, `oneOf`/`anyOf` (other than
    "type or null"), exclusive bounds, a non-JSON or non-object body and a schema without a type
    are errors at load, never a silently wider tool.

OVERLAY
    A manual manifest (YAML file or mapping) stays a fallback and an overlay. An entry whose
    `name` is a generated tool patches it field by field (`arguments` and `response` merge by
    key). An entry with no twin must be a complete manual tool (its `route` gets the overlay's
    `api_prefix`); a partial entry with no twin is a contract error. An entry may pin
    `operation: "GET /path"`, which must still be the operation behind the tool.

Everything raises `OpenApiContractError` (a `BridgeManifestError`) at build time, naming the
operation.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml

from agent_core.adapters.driven.tools.http_bridge.manifest import (
    BridgeManifest,
    BridgeManifestError,
    parse_bridge_manifest,
)

__all__ = ["OpenApiContractError", "OpenApiSource", "load_openapi_manifest"]

OpenApiSource = Path | str | Mapping[str, Any]

_METHODS = ("get", "post", "put", "patch", "delete")
_TYPES = ("string", "integer", "number", "boolean", "array", "object")
_OVERLAY_KEYS = {"api_prefix", "defaults", "tools"}


class OpenApiContractError(BridgeManifestError):
    """The OpenAPI document (or its overlay) cannot be used as the tool contract."""


class _Resolver:
    """Local `$ref`s only (`#/components/...`): a tool contract never fetches another document."""

    def __init__(self, document: Mapping[str, Any], label: str) -> None:
        self._document, self._label = document, label

    def resolve(self, node: Any, where: str) -> Any:
        seen: set[str] = set()
        while isinstance(node, Mapping) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise OpenApiContractError(
                    f"{where}: remote $ref {ref!r} is not supported (local '#/...' references only)"
                )
            if ref in seen:
                raise OpenApiContractError(f"{where}: $ref {ref!r} is circular")
            seen.add(ref)
            target: Any = self._document
            for part in ref[2:].split("/"):
                key = unquote(part).replace("~1", "/").replace("~0", "~")
                if not isinstance(target, Mapping) or key not in target:
                    raise OpenApiContractError(f"{where}: $ref {ref!r} does not resolve")
                target = target[key]
            node = target
        return node


def _fail(where: str, message: str) -> OpenApiContractError:
    return OpenApiContractError(f"{where}: {message}")


# --- the document ---------------------------------------------------------------------------------


def _load_document(
    source: OpenApiSource, timeout: float, transport: httpx.BaseTransport | None
) -> tuple[dict[str, Any], str]:
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source)), "OpenAPI document"
    text = str(source)
    if "://" in text:
        return _fetch(text, timeout, transport), text
    path = Path(text)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise _fail(str(path), f"OpenAPI document is missing or unreadable ({error})") from error
    try:
        parsed = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError) as error:
        raise _fail(str(path), f"not a valid OpenAPI document ({error})") from error
    return _as_document(parsed, str(path)), str(path)


def _fetch(url: str, timeout: float, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    if not url.lower().startswith(("http://", "https://")):
        scheme = url.split("://", 1)[0]
        raise _fail(url, f"only http and https URLs are fetched, not {scheme!r}")
    try:
        with httpx.Client(timeout=timeout, transport=transport) as http:
            response = http.get(url)
    except httpx.HTTPError as error:
        raise _fail(
            url, f"OpenAPI document could not be fetched ({type(error).__name__})"
        ) from error
    if response.status_code >= 400:
        raise _fail(url, f"OpenAPI document could not be fetched (HTTP {response.status_code})")
    try:
        parsed = response.json()
    except ValueError as error:
        raise _fail(url, "the reply is not a JSON OpenAPI document") from error
    return _as_document(parsed, url)


def _as_document(parsed: Any, label: str) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise _fail(label, "not an OpenAPI document (expected a mapping)")
    return parsed


# --- one operation -> one manifest tool ---------------------------------------------------------


def _argument(
    resolver: _Resolver, schema: Any, *, where: str, location: str, required: bool, text: str
) -> dict[str, Any]:
    node = _plain(resolver, schema, where)
    for bound in ("exclusiveMinimum", "exclusiveMaximum"):
        if node.get(bound) not in (None, False):
            raise _fail(where, f"{bound} (exclusive bounds) is not supported")
    kind = node.get("type")
    if isinstance(kind, list):
        kind = _single([k for k in kind if k != "null"], where, "type")
    if kind is None:
        raise _fail(where, "the schema has no type")
    if kind not in _TYPES:
        raise _fail(where, f"type {kind!r} is not supported")
    entry: dict[str, Any] = {"type": kind, "in": location}
    if required:
        entry["required"] = True
    description = text or str(node.get("description") or "")
    if description:
        entry["description"] = description
    if node.get("default") is not None:
        entry["default"] = node["default"]
    if isinstance(node.get("enum"), list):
        entry["enum"] = [v for v in node["enum"] if v is not None]
    if kind in ("integer", "number"):
        for key in ("minimum", "maximum"):
            if key in node:
                entry[key] = node[key]
    return entry


def _single(options: list[Any], where: str, what: str) -> Any:
    if len(options) != 1:
        raise _fail(where, f"{what} with several alternatives is not supported")
    return options[0]


def _plain(resolver: _Resolver, schema: Any, where: str) -> dict[str, Any]:
    """The schema with refs followed and `allOf: [one]` / `anyOf: [X, null]` unwrapped."""
    node = resolver.resolve(schema, where)
    if not isinstance(node, Mapping):
        raise _fail(where, "the schema is not an object")
    node = dict(node)
    for combinator in ("allOf", "anyOf", "oneOf"):
        if combinator not in node:
            continue
        options = node.pop(combinator)
        if not isinstance(options, list):
            raise _fail(where, f"{combinator} must be a list")
        alive = [o for o in options if not (isinstance(o, Mapping) and o.get("type") == "null")]
        if len(alive) != 1:
            raise _fail(where, f"{combinator} with several alternatives is not supported")
        inner = _plain(resolver, alive[0], where)
        node = {**inner, **node}
    return node


def _parameters(
    resolver: _Resolver, item: Mapping[str, Any], operation: Mapping[str, Any], label: str
) -> dict[str, dict[str, Any]]:
    merged: dict[tuple[str, str], Any] = {}
    for source in (item.get("parameters", []), operation.get("parameters", [])):
        for raw in source:
            param = resolver.resolve(raw, label)
            merged[(str(param.get("name")), str(param.get("in")))] = param
    arguments: dict[str, dict[str, Any]] = {}
    for (name, location), param in merged.items():
        if location == "header":
            continue  # identity headers are the bridge's to send, never the model's
        if location == "cookie":
            raise _fail(label, f"cookie parameter {name!r} cannot be sent by the bridge")
        if location not in ("path", "query"):
            raise _fail(label, f"parameter {name!r} is in {location!r}, which is not supported")
        if "schema" not in param:
            raise _fail(label, f"parameter {name!r} has no schema")
        if name in arguments:
            raise _fail(label, f"argument name {name!r} is used twice")
        arguments[name] = _argument(
            resolver,
            param["schema"],
            where=f"{label}: parameter {name!r}",
            location=location,
            required=location == "path" or bool(param.get("required", False)),
            text=str(param.get("description") or ""),
        )
    return arguments


def _body(
    resolver: _Resolver, operation: Mapping[str, Any], taken: Mapping[str, Any], label: str
) -> dict[str, dict[str, Any]]:
    if "requestBody" not in operation:
        return {}
    body = resolver.resolve(operation["requestBody"], label)
    content = body.get("content", {}) if isinstance(body, Mapping) else {}
    media = next(
        (v for k, v in content.items() if k.split(";")[0].strip().lower() == "application/json"),
        None,
    )
    if media is None:
        raise _fail(label, "the request body needs an application/json content type")
    schema = _plain(resolver, media.get("schema", {}), f"{label}: request body")
    if schema.get("type") != "object":
        raise _fail(label, "the request body must be a JSON object")
    properties = schema.get("properties") or {}
    if not properties:
        raise _fail(label, "the request body object declares no properties")
    required = set(schema.get("required", []))
    arguments: dict[str, dict[str, Any]] = {}
    for name, sub in properties.items():
        if name in taken:
            raise _fail(label, f"body property {name!r} has the name of a path or query argument")
        arguments[name] = _argument(
            resolver,
            sub,
            where=f"{label}: body property {name!r}",
            location="body",
            required=name in required,
            text="",
        )
    return arguments


def _tool(
    resolver: _Resolver, path: str, item: Mapping[str, Any], method: str, label: str
) -> dict[str, Any]:
    operation = item[method]
    name = operation.get("x-agent-name") or operation.get("operationId")
    if not name:
        raise _fail(label, "has neither x-agent-name nor operationId to name the tool")
    description = next(
        (
            str(text)
            for key in ("x-agent-description", "summary", "description")
            if (text := operation.get(key))
        ),
        "",
    )
    if not description:
        raise _fail(label, "has no x-agent-description, summary or description")
    arguments = _parameters(resolver, item, operation, label)
    arguments.update(_body(resolver, operation, arguments, label))
    effect = operation.get("x-agent-effect", "read" if method == "get" else "write")
    if effect not in ("read", "write"):
        raise _fail(label, f"x-agent-effect must be 'read' or 'write', not {effect!r}")
    tool: dict[str, Any] = {
        "name": name,
        "description": description,
        "method": method.upper(),
        "route": path,
        "kind": effect,
        "arguments": arguments,
    }
    response = {
        target: operation[mark]
        for mark, target in (
            ("x-agent-fields", "pick"),
            ("x-agent-rows-key", "rows_key"),
            ("x-agent-row-limit", "row_limit"),
        )
        if mark in operation
    }
    if response:
        tool["response"] = response
    for mark, target in (
        ("x-agent-replay", "replay"),
        ("x-agent-context", "context"),
        ("x-agent-warning", "warning"),
    ):
        if mark in operation:
            tool[target] = operation[mark]
    if "x-agent-idempotency" in operation:
        parts = operation["x-agent-idempotency"]
        tool["idempotency"] = list(arguments) if parts is True else parts
    try:
        parse_bridge_manifest({"tools": [tool]}, source=label)
    except BridgeManifestError as error:
        raise OpenApiContractError(str(error)) from error
    return tool


def _generate(
    document: Mapping[str, Any], label: str
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    if not str(document.get("openapi", "")).startswith("3."):
        raise _fail(label, "not an OpenAPI 3.x document (the 'openapi' field must start with 3.)")
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise _fail(label, "has no paths")
    resolver = _Resolver(document, label)
    tools: dict[str, dict[str, Any]] = {}
    origin: dict[str, str] = {}
    for path, item in paths.items():
        for method in _METHODS:
            operation = item.get(method) if isinstance(item, Mapping) else None
            if not isinstance(operation, Mapping):
                continue
            tag = f"{method.upper()} {path}"
            op_id = operation.get("operationId")
            where = f"{label}: operation {tag}" + (f" ({op_id})" if op_id else "")
            mark = operation.get("x-agent-tool")
            if mark is not None and not isinstance(mark, bool):
                raise _fail(where, f"x-agent-tool must be true or false, not {mark!r}")
            if mark is not True:
                continue
            tool = _tool(resolver, path, item, method, where)
            if tool["name"] in tools:
                raise _fail(
                    where,
                    f"tool name {tool['name']!r} is already taken by {origin[tool['name']]}",
                )
            tools[tool["name"]] = tool
            origin[tool["name"]] = tag
    return tools, origin


# --- the overlay ----------------------------------------------------------------------------------


def _load_overlay(overlay: Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(overlay, Mapping):
        return copy.deepcopy(dict(overlay))
    try:
        parsed = yaml.safe_load(Path(overlay).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise _fail(str(overlay), f"overlay is missing or not valid YAML ({error})") from error
    if not isinstance(parsed, dict):
        raise _fail(str(overlay), "the overlay must be a mapping")
    return parsed


def _patch(twin: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    merged = dict(twin)
    for key, value in entry.items():
        current = merged.get(key)
        if key == "response" and isinstance(value, dict):
            merged[key] = {**(current or {}), **value}
        elif key == "arguments" and isinstance(value, dict):
            merged[key] = {
                **(current or {}),
                **{
                    a: {**(current or {}).get(a, {}), **spec} if isinstance(spec, dict) else spec
                    for a, spec in value.items()
                },
            }
        else:
            merged[key] = value
    return merged


def _overlay_onto(
    generated: dict[str, dict[str, Any]], origin: Mapping[str, str], overlay: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    unknown = sorted(set(overlay) - _OVERLAY_KEYS)
    if unknown:
        raise _fail("overlay", f"unknown key {unknown[0]!r} (allowed: api_prefix, defaults, tools)")
    prefix = str(overlay.get("api_prefix", "")).rstrip("/")
    tools = dict(generated)
    for raw in overlay.get("tools", []):
        entry = dict(raw)
        name = str(entry.get("name"))
        pin = entry.pop("operation", None)
        where = f"overlay tool {name!r}"
        if name in origin:
            if pin is not None and pin != origin[name]:
                raise _fail(
                    where,
                    f"is pinned to {pin!r} but the document's operation is {origin[name]!r}",
                )
            tools[name] = _patch(tools[name], entry)
        elif name in tools:
            raise _fail(where, "is listed twice in the overlay")
        elif pin is None and "route" in entry:
            entry["route"] = prefix + str(entry["route"])
            tools[name] = entry
        else:
            marked = ", ".join(sorted(origin)) or "none"
            raise _fail(
                where,
                "patches no operation marked x-agent-tool: true "
                f"(the document marks: {marked}); give a full manual tool or fix the name",
            )
    return tools, overlay.get("defaults", {})


# --- entry point ----------------------------------------------------------------------------------


def load_openapi_manifest(
    source: OpenApiSource,
    *,
    overlay: Path | Mapping[str, Any] | None = None,
    expect: Sequence[str] = (),
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> BridgeManifest:
    """The tools of an OpenAPI 3.x document (a mapping, a file path or an http(s) URL).

    A URL is fetched once, here, with `timeout`. `overlay` is a manual manifest (file or mapping)
    patched over / added to the generated tools. `expect` names tools that must exist after the
    merge. Raises `OpenApiContractError` naming the operation or tool.
    """
    document, label = _load_document(source, timeout, transport)
    tools, origin = _generate(document, label)
    defaults: dict[str, Any] = {}
    if overlay is not None:
        tools, defaults = _overlay_onto(tools, origin, _load_overlay(overlay))
    for name in expect:
        if name not in tools:
            marked = ", ".join(sorted(origin)) or "none"
            raise _fail(
                label,
                f"expected tool {name!r} is not provided: no operation marked x-agent-tool: true "
                f"has that name (the document marks: {marked})",
            )
    if not tools:
        raise _fail(label, "no operation is marked x-agent-tool: true")
    try:
        return parse_bridge_manifest(
            {"defaults": defaults, "tools": list(tools.values())}, source=label
        )
    except BridgeManifestError as error:
        raise OpenApiContractError(str(error)) from error
