"""Build pydantic-ai tools from a bridge manifest (Phase 3, G1, D83).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_builder.py
Implements: no port - the consumer-facing API of the HTTP tool bridge.

THE CONSUMER API
    A consumer addon's `toolsets/<name>.py` calls, from its zero-argument `build_toolset()`:

        def build_toolset(settings):
            return build_bridge_toolset(settings, manifest=HERE.parent / "tools.yaml")

    and gets a `FunctionToolset` with one tool per manifest entry (or, with `openapi=...`, per
    `x-agent-tool` operation of the backend's OpenAPI document, D85). `settings` is the consumer
    addon's own validated settings (the loader hands them to a one-parameter `build_toolset`,
    D84); a `BridgeSettings` is accepted too. The environment is never read.

WHAT EACH TOOL DOES
    1. Validates the model's arguments against the manifest (required, type, enum, numeric clamp).
       A problem is returned as `{"error": ...}`; no call is made.
    2. Calls the backend (client.py) with the turn's identity headers and, for a write that
       declares `idempotency`, a deterministic key.
    3. Projects the reply (pick allowlist, row limit), surfaces the warning header, and returns it.
    Reads are replay-safe; writes are `interrupted` after a crash unless the manifest says `safe`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.http_bridge.client import Reply, call_backend
from agent_core.adapters.driven.tools.http_bridge.manifest import (
    ArgumentSpec,
    BridgeManifestError,
    ResponseSpec,
    ToolSpec,
    load_bridge_manifest,
)
from agent_core.adapters.driven.tools.http_bridge.openapi import (
    OpenApiSource,
    load_openapi_manifest,
)
from agent_core.adapters.driven.tools.http_bridge.settings import BridgeSettings
from agent_core.domain.tool_replay import safe_tool_metadata

__all__ = ["build_bridge_toolset"]


def _bounds(spec: ArgumentSpec) -> tuple[Any, Any]:
    """The clamp bounds as the argument's type holds them (D84).

    An integer argument rounds a fractional bound INWARD (`minimum: 1.5` -> 2, `maximum: 9.5` -> 9),
    so a clamped value is never outside what the manifest wrote; a number keeps the bound as is.
    """
    low, high = spec.minimum, spec.maximum
    if spec.type == "integer":
        low = None if low is None else math.ceil(low)
        high = None if high is None else math.floor(high)
    return low, high


def _coerce(name: str, spec: ArgumentSpec, value: Any) -> Any:
    """The value as the manifest types it, clamped; a junk number falls back to the default.

    Raises `ValueError(message)` for a value the model must correct.
    """
    kind = spec.type
    if kind in ("integer", "number"):
        try:
            number = float(value) if kind == "number" else int(value)
            if isinstance(number, float) and not math.isfinite(number):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            # Junk from the model must not raise: the default stands in for it.
            if spec.default is not None:
                return spec.default
            raise ValueError(f"{name} must be a number") from None
        low, high = _bounds(spec)
        if low is not None:
            number = max(number, low)
        if high is not None:
            number = min(number, high)
        return number
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value
    if kind == "string":
        return value if isinstance(value, str) else str(value)
    if kind == "array" and not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    if kind == "object" and not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _check_segment(name: str, value: Any) -> None:
    """A path argument is ONE segment of the route: empty and dot-segments could leave it (D84).

    Slashes and every other reserved character are percent-encoded where the path is built; `.`
    and `..` survive encoding and a server or proxy may resolve them, so they are refused.
    """
    text = str(value)
    if text.strip() == "" or text in (".", ".."):
        raise ValueError(f"{name} is not a valid identifier")


def _prepare(tool: ToolSpec, raw: dict[str, Any]) -> dict[str, Any]:
    """The arguments to send, defaults applied. Raises `ValueError` with a message for the model."""
    values: dict[str, Any] = {}
    for name, spec in tool.arguments.items():
        value = raw.get(name)
        if value is None:
            value = spec.default
        if value is None:
            if spec.required:
                raise ValueError(f"{name} is required")
            continue
        value = _coerce(name, spec, value)
        if spec.where == "path":
            _check_segment(name, value)
        if spec.enum is not None and value not in spec.enum:
            raise ValueError(f"{name} must be one of {', '.join(str(e) for e in spec.enum)}")
        values[name] = value
    return values


def _schema(tool: ToolSpec) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, spec in tool.arguments.items():
        entry: dict[str, Any] = {"type": spec.type}
        if spec.description:
            entry["description"] = spec.description
        if spec.enum is not None:
            entry["enum"] = list(spec.enum)
        low, high = _bounds(spec)
        if low is not None:
            entry["minimum"] = low
        if high is not None:
            entry["maximum"] = high
        if spec.default is not None:
            entry["default"] = spec.default
        properties[name] = entry
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    required = [n for n, s in tool.arguments.items() if s.required]
    if required:
        schema["required"] = required
    return schema


class _Shape(Exception):
    """The reply does not have the shape the manifest describes."""


def _pick(item: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: item[k] for k in keys if k in item}


def _rows(rows: Any, pick: tuple[str, ...] | None, limit: int) -> list[Any]:
    """The first `limit` rows; with a `pick`, only the picked keys of the object rows (D84).

    Fail closed: a projection that cannot apply to a row (not an object) drops that row, so only
    picked fields ever reach the model.
    """
    taken: list[Any] = rows[:limit]
    if pick is None:
        return taken
    return [_pick(r, pick) for r in taken if isinstance(r, dict)]


def _project(data: Any, response: ResponseSpec, default_limit: int) -> Any:
    """The reply as the model may see it. Raises `_Shape` when the reply is not what was declared.

    `rows_key` replies keep their envelope (`total`, cursors) and project only the rows.
    """
    limit = response.row_limit or default_limit
    if response.rows_key is not None:
        if isinstance(data, dict) and isinstance(data.get(response.rows_key), list):
            rows = _rows(data[response.rows_key], response.pick, limit)
            return {**data, response.rows_key: rows}
        raise _Shape
    if isinstance(data, list):
        return _rows(data, response.pick, limit)
    if response.pick is None:
        return data
    if isinstance(data, dict):
        return _pick(data, response.pick)
    raise _Shape


def _surface(data: Any, response: ResponseSpec, warning: str | None) -> Any:
    if response.wrap_as is not None:
        data = {response.wrap_as: data}
    if warning:
        if isinstance(data, dict):
            return {**data, "warning": warning}
        return {"data": data, "warning": warning}
    return data


def _make_tool(
    tool: ToolSpec,
    prefix: str,
    settings: BridgeSettings,
    transport: httpx.AsyncBaseTransport | None,
) -> Tool[Any]:
    async def run(ctx: Any, **raw: Any) -> Any:
        try:
            values = _prepare(tool, raw)
        except ValueError as problem:
            return {"error": str(problem)}
        path = prefix + tool.route
        for name, spec in tool.arguments.items():
            if spec.where == "path":
                path = path.replace("{" + name + "}", quote(str(values[name]), safe=""))
        params = {n: v for n, v in values.items() if tool.arguments[n].where == "query"}
        body = {n: v for n, v in values.items() if tool.arguments[n].where == "body"}
        parts = tuple(str(values.get(p, "")) for p in tool.idempotency) or None
        reply: Reply = await call_backend(
            ctx,
            settings,
            tool.method,
            path,
            params=params,
            body=body or None,
            context=tool.context or (),
            idempotency_parts=parts,
            want_warning=tool.warning,
            transport=transport,
        )
        if reply.error is not None:
            return reply.error
        try:
            projected = _project(reply.data, tool.response, settings.default_row_limit)
        except _Shape:
            return {
                "error": f"The {settings.service_name} reply did not have the expected shape, "
                "so nothing was returned."
            }
        return _surface(projected, tool.response, reply.warning)

    run.__name__ = tool.name
    built: Tool[Any] = Tool.from_schema(
        run, tool.name, tool.description, _schema(tool), takes_ctx=True
    )
    if tool.replay == "safe":
        built.metadata = safe_tool_metadata()
    return built


def build_bridge_toolset(
    settings: BridgeSettings | Mapping[str, Any],
    *,
    manifest: Path | None = None,
    openapi: OpenApiSource | None = None,
    overlay: Path | Mapping[str, Any] | None = None,
    expect: Sequence[str] = (),
    transport: httpx.AsyncBaseTransport | None = None,
) -> FunctionToolset[Any]:
    """One tool per manifest entry or per `x-agent-tool` operation (D85).

    Exactly one source: `manifest` (a YAML file) or `openapi` (a document mapping, a file path or an
    http(s) URL fetched once now). `overlay` (a manual manifest, file or mapping) patches or extends
    the generated tools and needs `openapi`. `expect` lists tool names that must exist, so a backend
    that dropped an operation stops the startup instead of one tool. `settings` is a
    `BridgeSettings` or the consumer addon's validated mapping (its bridge keys are used).
    Raises `BridgeManifestError` / `OpenApiContractError` / `ValueError` at build time.
    """
    if (manifest is None) == (openapi is None):
        raise ValueError("pass exactly one of manifest= (a YAML file) or openapi= (a document)")
    if overlay is not None and openapi is None:
        raise ValueError("overlay= applies to an openapi= document, not to a manifest file")
    resolved = (
        settings if isinstance(settings, BridgeSettings) else BridgeSettings.from_consumer(settings)
    )
    if openapi is not None:
        loaded = load_openapi_manifest(
            openapi,
            overlay=overlay,
            expect=expect,
            timeout=resolved.timeout_seconds,
            transport=transport if isinstance(transport, httpx.BaseTransport) else None,
        )
    else:
        assert manifest is not None
        loaded = load_bridge_manifest(manifest)
        missing = [name for name in expect if name not in {t.name for t in loaded.tools}]
        if missing:
            raise BridgeManifestError(f"{manifest}: expected tool {missing[0]!r} is not declared")
    prefix = loaded.api_prefix.rstrip("/")
    return FunctionToolset([_make_tool(t, prefix, resolved, transport) for t in loaded.tools])
