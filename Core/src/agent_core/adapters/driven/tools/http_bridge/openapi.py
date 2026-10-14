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

import asyncio
import copy
import json
import threading
from collections.abc import Coroutine, Mapping, Sequence
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

__all__ = ["OpenApiContractError", "OpenApiSource", "clear_openapi_cache", "load_openapi_manifest"]

OpenApiSource = Path | str | Mapping[str, Any]
Transport = httpx.BaseTransport | httpx.AsyncBaseTransport

# (URL, transport) -> the document the backend answered, once per process (G1c): a consumer with
# several toolsets builds the same bridge several times and the backend is asked once. The
# transport is part of the key (G2b): a document fetched through one transport (a test double, a
# second backend's own route) is never served to a build that uses another. A failed fetch is never
# stored. Builds racing for one key wait for the one fetch in flight (`_Flight`), so the backend is
# still asked once (slice 29); other keys are not held up. Tests call `clear_openapi_cache()`, which
# forgets the stored documents but never a fetch in flight (slice 30): its builds still share it.
_FETCHED: dict[tuple[str, Transport | None], dict[str, Any]] = {}
_IN_FLIGHT: dict[tuple[str, Transport | None], _Flight] = {}
_FETCH_LOCK = threading.Lock()


class _Flight:
    """One fetch in progress: the builds that find it wait for its outcome instead of fetching."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self.document: dict[str, Any] | None = None

    def wait(self) -> None:
        self._done.wait()

    def finish(self, document: dict[str, Any] | None) -> None:
        self.document = document
        self._done.set()


_METHODS = ("get", "post", "put", "patch", "delete")
_TYPES = ("string", "integer", "number", "boolean", "array", "object")
_OVERLAY_KEYS = {"api_prefix", "defaults", "tools"}


def clear_openapi_cache() -> None:
    """Forget every fetched document, so the next build asks the backend again.

    A fetch still in flight is not forgotten: the builds waiting for it, and any that starts before
    it ends, are answered by that one fetch (slice 30 advisory).
    """
    with _FETCH_LOCK:
        _FETCHED.clear()


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
    source: OpenApiSource, timeout: float, transport: Transport | None
) -> tuple[dict[str, Any], str]:
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source)), "OpenAPI document"
    text = str(source)
    if "://" in text:
        return copy.deepcopy(_fetched(text, timeout, transport)), text
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


def _fetched(url: str, timeout: float, transport: Transport | None) -> dict[str, Any]:
    """The document at `url` through `transport`, asked for once per process (G1c)."""
    key = (url, transport)
    while True:
        with _FETCH_LOCK:
            cached = _FETCHED.get(key)
            if cached is not None:
                return cached
            flight = _IN_FLIGHT.get(key)
            if flight is None:
                flight = _IN_FLIGHT[key] = _Flight()
                break  # this build fetches
        flight.wait()  # a build racing for the same key waits here instead of fetching again
        if flight.document is not None:
            return flight.document
        # that fetch failed (and raised in its own build): this build tries on its own
    try:
        document = _fetch(url, timeout, transport)
    except BaseException:
        with _FETCH_LOCK:
            _IN_FLIGHT.pop(key, None)
        flight.finish(None)
        raise
    with _FETCH_LOCK:
        stored = _FETCHED.setdefault(key, document)
        _IN_FLIGHT.pop(key, None)
    flight.finish(stored)
    return stored


def _fetch(url: str, timeout: float, transport: Transport | None) -> dict[str, Any]:
    if not url.lower().startswith(("http://", "https://")):
        scheme = url.split("://", 1)[0]
        raise _fail(url, f"only http and https URLs are fetched, not {scheme!r}")
    try:
        if isinstance(transport, httpx.AsyncBaseTransport) and not isinstance(
            transport, httpx.BaseTransport
        ):
            response = _blocking(_get_async(url, timeout, transport))
        else:
            with httpx.Client(
                timeout=timeout,
                transport=transport if isinstance(transport, httpx.BaseTransport) else None,
            ) as http:
                response = http.get(url)
    except httpx.HTTPError as error:
        raise _fail(
            url,
            f"OpenAPI document could not be fetched ({type(error).__name__}); the backend must "
            "answer at startup, so check that it is running and that base_url is right",
        ) from error
    if response.status_code >= 400:
        raise _fail(url, f"OpenAPI document could not be fetched (HTTP {response.status_code})")
    try:
        parsed = response.json()
    except ValueError as error:
        raise _fail(url, "the reply is not a JSON OpenAPI document") from error
    return _as_document(parsed, url)


async def _get_async(
    url: str, timeout: float, transport: httpx.AsyncBaseTransport
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
        return await http.get(url)


def _blocking[T](work: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to its end from synchronous code, even inside a running event loop.

    A toolset is built synchronously at startup, which may itself run on a loop: then the work
    gets its own loop on a helper thread instead of failing with "asyncio.run() inside a loop".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(work)
    outcome: list[T] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            outcome.append(asyncio.run(work))
        except BaseException as error:  # noqa: BLE001 - re-raised on the calling thread
            failure.append(error)

    thread = threading.Thread(target=target, name="openapi-fetch")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return outcome[0]


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
        if not isinstance(source, list):
            raise _fail(label, "parameters must be a list")
        for raw in source:
            param = resolver.resolve(raw, label)
            if not isinstance(param, Mapping):
                raise _fail(label, f"a parameter must be an object, not {type(param).__name__}")
            if not isinstance(param.get("name"), str) or not isinstance(param.get("in"), str):
                raise _fail(label, "a parameter needs a text 'name' and a text 'in'")
            merged[(param["name"], param["in"])] = param
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
    if not isinstance(content, Mapping):
        raise _fail(label, "the request body 'content' must be an object")
    media = next(
        (v for k, v in content.items() if k.split(";")[0].strip().lower() == "application/json"),
        None,
    )
    if media is None:
        raise _fail(label, "the request body needs an application/json content type")
    if not isinstance(media, Mapping):
        raise _fail(label, "the request body's application/json entry must be an object")
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


def _check_overlay(overlay: dict[str, Any]) -> None:
    """The overlay has the shape a manual manifest has; anything else is named, not raised raw."""
    unknown = sorted(set(overlay) - _OVERLAY_KEYS)
    if unknown:
        raise _fail("overlay", f"unknown key {unknown[0]!r} (allowed: api_prefix, defaults, tools)")
    if "api_prefix" in overlay and not isinstance(overlay["api_prefix"], str):
        raise _fail("overlay", "api_prefix must be text")
    if "defaults" in overlay and not isinstance(overlay["defaults"], Mapping):
        raise _fail("overlay", "defaults must be an object")
    tools = overlay.get("tools", [])
    if not isinstance(tools, list):
        raise _fail("overlay", "tools must be a list")
    for position, entry in enumerate(tools):
        if not isinstance(entry, Mapping):
            raise _fail(f"overlay tool #{position + 1}", "must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise _fail(f"overlay tool #{position + 1}", "needs a text 'name'")
        where = f"overlay tool {name!r}"
        if "operation" in entry and not isinstance(entry["operation"], str):
            raise _fail(where, "operation must be text like 'GET /path'")
        if "response" in entry and not isinstance(entry["response"], Mapping):
            raise _fail(where, "response must be an object")
        arguments = entry.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, Mapping):
                raise _fail(where, "arguments must be an object")
            for argument, spec in arguments.items():
                if spec is not None and not isinstance(spec, Mapping):
                    raise _fail(where, f"argument {argument!r} must be an object (or null)")


def _patch(twin: dict[str, Any], entry: dict[str, Any], where: str) -> dict[str, Any]:
    merged = dict(twin)
    for key, value in entry.items():
        current = merged.get(key)
        if key == "response" and isinstance(value, dict):
            merged[key] = {**(current or {}), **value}
        elif key == "arguments" and isinstance(value, dict):
            arguments = dict(current or {})
            for argument, spec in value.items():
                if spec is None:  # hidden from the model and never sent
                    if argument not in arguments:
                        raise _fail(
                            where,
                            f"removes argument {argument!r}, which the document's operation "
                            "does not have",
                        )
                    del arguments[argument]
                elif spec.get("fixed") is not None:
                    # a fixed value replaces the document's constraints (bounds, default, required)
                    base = arguments.get(argument, {})
                    kept = {k: base[k] for k in ("type", "in", "description") if k in base}
                    arguments[argument] = {**kept, **spec}
                else:
                    arguments[argument] = {**arguments.get(argument, {}), **spec}
            merged[key] = arguments
        else:
            merged[key] = value
    return merged


def _declared(document: Mapping[str, Any]) -> frozenset[str]:
    """Every `METHOD /path` the document declares, marked as a tool or not."""
    found: set[str] = set()
    for path, item in (document.get("paths") or {}).items():
        if isinstance(item, Mapping):
            found.update(
                f"{m.upper()} {path}" for m in _METHODS if isinstance(item.get(m), Mapping)
            )
    return frozenset(found)


def _pinned_manual_tool(
    entry: dict[str, Any], pin: str, declared: frozenset[str], where: str
) -> None:
    """A manual tool pinned to `METHOD /path` must be that operation, and the document must have it.

    This is how an addon builds a second tool over an operation (a fixed argument, another name)
    and still fails the startup when the backend moves or drops the operation under it.
    """
    own = f"{str(entry.get('method', '')).upper()} {entry['route']}"
    if pin != own:
        raise _fail(where, f"is pinned to {pin!r} but its own method and route are {own!r}")
    if pin not in declared:
        raise _fail(where, f"is pinned to {pin!r}, which the document does not declare")


def _overlay_onto(
    generated: dict[str, dict[str, Any]],
    origin: Mapping[str, str],
    overlay: dict[str, Any],
    declared: frozenset[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    _check_overlay(overlay)
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
            tools[name] = _patch(tools[name], entry, where)
        elif name in tools:
            raise _fail(where, "is listed twice in the overlay")
        elif "route" in entry:
            entry["route"] = prefix + str(entry["route"])
            if pin is not None:
                _pinned_manual_tool(entry, pin, declared, where)
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
    transport: Transport | None = None,
) -> BridgeManifest:
    """The tools of an OpenAPI 3.x document (a mapping, a file path or an http(s) URL).

    A URL is fetched once per process (cached by URL, `clear_openapi_cache()` forgets it) with
    `timeout`, through `transport` (sync or async) when one is given. `overlay` is a manual
    manifest (file or mapping) patched over / added to the generated tools. `expect` names tools
    that must exist after the merge. Raises `OpenApiContractError` naming the operation or tool.
    """
    document, label = _load_document(source, timeout, transport)
    tools, origin = _generate(document, label)
    defaults: dict[str, Any] = {}
    if overlay is not None:
        tools, defaults = _overlay_onto(tools, origin, _load_overlay(overlay), _declared(document))
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
