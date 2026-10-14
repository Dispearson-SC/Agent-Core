"""Build pydantic-ai tools from a bridge manifest (Phase 3, G1, D83).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_builder.py
Implements: no port - the consumer-facing API of the HTTP tool bridge.

THE CONSUMER API
    A consumer addon's `toolsets/<name>.py` calls, from its zero-argument `build_toolset()`:

        build_bridge_toolset(Path(__file__).parent.parent / "tools.yaml")

    and gets a `FunctionToolset` with one tool per manifest entry. Settings default to the enabled
    `http_tool_bridge` addon's (`settings_from_environment`); tests pass them and a transport.

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
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.http_bridge.client import Reply, call_backend
from agent_core.adapters.driven.tools.http_bridge.manifest import (
    ArgumentSpec,
    ResponseSpec,
    ToolSpec,
    load_bridge_manifest,
)
from agent_core.adapters.driven.tools.http_bridge.settings import (
    BridgeSettings,
    settings_from_environment,
)
from agent_core.domain.tool_replay import safe_tool_metadata

__all__ = ["build_bridge_toolset"]


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
        if spec.minimum is not None:
            number = max(number, type(number)(spec.minimum))
        if spec.maximum is not None:
            number = min(number, type(number)(spec.maximum))
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
        if spec.minimum is not None:
            entry["minimum"] = spec.minimum
        if spec.maximum is not None:
            entry["maximum"] = spec.maximum
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


def _pick(item: Any, keys: tuple[str, ...] | None) -> Any:
    if keys is None or not isinstance(item, dict):
        return item
    return {k: item[k] for k in keys if k in item}


def _project(data: Any, response: ResponseSpec, default_limit: int) -> Any:
    limit = response.row_limit or default_limit
    if response.rows_key is not None:
        if isinstance(data, dict) and isinstance(data.get(response.rows_key), list):
            rows = [_pick(r, response.pick) for r in data[response.rows_key][:limit]]
            return {**data, response.rows_key: rows}
        return data
    if isinstance(data, list):
        return [_pick(r, response.pick) for r in data[:limit]]
    return _pick(data, response.pick)


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
        projected = _project(reply.data, tool.response, settings.default_row_limit)
        return _surface(projected, tool.response, reply.warning)

    run.__name__ = tool.name
    built: Tool[Any] = Tool.from_schema(
        run, tool.name, tool.description, _schema(tool), takes_ctx=True
    )
    if tool.replay == "safe":
        built.metadata = safe_tool_metadata()
    return built


def build_bridge_toolset(
    manifest_path: Path,
    settings: BridgeSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FunctionToolset[Any]:
    """One tool per manifest entry. Raises `BridgeManifestError` / `ValueError` at build time."""
    manifest = load_bridge_manifest(manifest_path)
    resolved = settings if settings is not None else settings_from_environment()
    prefix = manifest.api_prefix.rstrip("/")
    return FunctionToolset([_make_tool(t, prefix, resolved, transport) for t in manifest.tools])
