"""The declarative tool manifest the bridge builds tools from (Phase 3, G1, D83).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_manifest.py
Implements: no port - a loader for a consumer addon's YAML file.

SHAPE (see docs/ADDONS.md "The HTTP tool bridge")
    api_prefix: /internal/v1            # optional, prepended to every route
    defaults: {context: [session, agent, tenant]}   # optional
    tools:
      - name: get_book                  # the tool name the model sees
        description: One book by id.
        method: GET                     # GET | POST | PUT | PATCH | DELETE
        route: /books/{book_id}         # `{x}` is a `path` argument
        kind: read                      # read | write
        arguments:                      # what the MODEL may send; nothing else is a parameter
          book_id: {type: string, in: path, required: true}
        context: [session, agent]       # which turn identities become headers
        idempotency: [book_id]          # write only: argument names that key the retry
        warning: true                   # surface the service warning header
        replay: safe                    # safe | interrupted (default: read -> safe)
        response: {pick: [id], rows_key: items, row_limit: 5, wrap_as: reviews}

Everything is validated at load: a bad manifest stops the consumer addon at startup, never mid-turn.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal, Self

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "ArgumentSpec",
    "BridgeManifest",
    "BridgeManifestError",
    "ResponseSpec",
    "ToolSpec",
    "load_bridge_manifest",
    "parse_bridge_manifest",
]

CONTEXT_SOURCES = ("session", "agent", "tenant", "subject")
DEFAULT_CONTEXT = ("session", "agent", "tenant")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME = r"^[a-z][a-z0-9_]*$"


class BridgeManifestError(ValueError):
    """The manifest cannot be used. The message names the file and the field."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ArgumentSpec(_Strict):
    type: Literal["string", "integer", "number", "boolean", "array", "object"]
    where: Literal["path", "query", "body"] = Field(alias="in")
    description: str = ""
    required: bool = False
    default: Any = None
    enum: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.where == "path" and not self.required:
            raise ValueError("a path argument is always required")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum is above maximum")
        if (self.minimum is not None or self.maximum is not None) and self.type not in (
            "integer",
            "number",
        ):
            raise ValueError("minimum/maximum apply to integer and number only")
        if self.type == "integer" and self.minimum is not None and self.maximum is not None:
            if math.ceil(self.minimum) > math.floor(self.maximum):
                raise ValueError("no integer lies between minimum and maximum")
        return self


class ResponseSpec(_Strict):
    pick: tuple[str, ...] | None = None
    rows_key: str | None = None
    row_limit: int | None = Field(default=None, ge=1)
    wrap_as: str | None = None


class ToolSpec(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=False, populate_by_name=True)

    name: str = Field(pattern=_NAME)
    description: str = Field(min_length=1)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    route: str = Field(pattern=r"^/")
    kind: Literal["read", "write"]
    arguments: dict[str, ArgumentSpec] = Field(default_factory=dict)
    # None in the file = the manifest default; always a tuple once the manifest is validated.
    context: tuple[str, ...] | None = None
    idempotency: tuple[str, ...] = ()
    warning: bool = False
    # None in the file = `safe` for a read, `interrupted` for a write; resolved with the manifest.
    replay: Literal["safe", "interrupted"] | None = None
    response: ResponseSpec = ResponseSpec()

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        placeholders = set(_PLACEHOLDER.findall(self.route))
        path_args = {n for n, a in self.arguments.items() if a.where == "path"}
        for missing in sorted(placeholders - path_args):
            raise ValueError(f"route placeholder {missing!r} has no path argument")
        for extra in sorted(path_args - placeholders):
            raise ValueError(f"path argument {extra!r} is not a placeholder of the route")
        if self.method in ("GET", "DELETE") and any(
            a.where == "body" for a in self.arguments.values()
        ):
            raise ValueError(f"a {self.method} tool cannot take body arguments")
        if self.kind == "write" and self.method == "GET":
            raise ValueError("a write tool cannot use GET")
        if self.idempotency and self.kind != "write":
            raise ValueError("idempotency applies to write tools only")
        for part in self.idempotency:
            if part not in self.arguments:
                raise ValueError(f"idempotency part {part!r} is not an argument")
        for source in self.context or ():
            if source not in CONTEXT_SOURCES:
                raise ValueError(f"context {source!r} is not one of {', '.join(CONTEXT_SOURCES)}")
        for arg_name in self.arguments:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", arg_name) or arg_name == "ctx":
                raise ValueError(f"argument name {arg_name!r} is not usable")
        return self


class ManifestDefaults(_Strict):
    context: tuple[str, ...] = DEFAULT_CONTEXT

    @model_validator(mode="after")
    def _known(self) -> Self:
        for source in self.context:
            if source not in CONTEXT_SOURCES:
                raise ValueError(f"context {source!r} is not one of {', '.join(CONTEXT_SOURCES)}")
        return self


class BridgeManifest(_Strict):
    api_prefix: str = ""
    defaults: ManifestDefaults = ManifestDefaults()
    tools: tuple[ToolSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [t.name for t in self.tools]
        for name in names:
            if names.count(name) > 1:
                raise ValueError(f"duplicate tool name {name!r}")
        for tool in self.tools:
            if tool.context is None:
                tool.context = self.defaults.context
            if tool.replay is None:
                tool.replay = "safe" if tool.kind == "read" else "interrupted"
        return self


def parse_bridge_manifest(document: Any, *, source: str = "manifest") -> BridgeManifest:
    """Validate a plain mapping into the manifest model, whatever produced it.

    The YAML file (`load_bridge_manifest`) is one source; any other loader (an OpenAPI reader, for
    example) builds the same mapping and enters here, so validation and defaults live in one place.
    """
    try:
        return BridgeManifest.model_validate(document)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        raise BridgeManifestError(f"{source}: {problems}") from error


def load_bridge_manifest(path: Path) -> BridgeManifest:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise BridgeManifestError(f"{path}: manifest is missing or unreadable ({error})") from error
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise BridgeManifestError(f"{path}: not valid YAML ({error})") from error
    return parse_bridge_manifest(document, source=str(path))
