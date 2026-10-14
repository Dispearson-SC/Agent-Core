"""OpenAPI loader of the HTTP tool bridge: a backend's own document becomes the same manifest (G1b).

Subject: adapters/driven/tools/http_bridge/openapi.py and the `openapi=` / `overlay=` / `expect=`
arguments of `build_bridge_toolset`. Only operations marked `x-agent-tool: true` become tools.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.http_bridge import (
    BridgeManifest,
    BridgeSettings,
    build_bridge_toolset,
)
from agent_core.adapters.driven.tools.http_bridge.openapi import (
    OpenApiContractError,
    clear_openapi_cache,
    load_openapi_manifest,
)
from agent_core.domain.tool_replay import ReplayPolicy, replay_policy
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http_bridge" / "library_openapi.json"
SETTINGS = BridgeSettings(base_url="http://svc.test:9")


@pytest.fixture(autouse=True)
def _fresh_document_cache() -> None:
    """The fetched document is cached per URL (G1c): a test must not see another test's fetch."""
    clear_openapi_cache()


def _spec() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _tools(manifest: BridgeManifest) -> dict[str, Any]:
    return {t.name: t for t in manifest.tools}


def _operation(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation: dict[str, Any] = spec["paths"][path][method]
    return operation


def _ctx() -> Any:
    deps = TurnContext(
        caller=CallerIdentity("m-1", "web", TenantId("T9"), frozenset()),
        session=SessionRef(session_id="case-1", tenant_id=TenantId("T9")),  # type: ignore[arg-type]
        agent_id="clerk",
    )
    return SimpleNamespace(deps=deps)


def _marked(operation: dict[str, Any]) -> dict[str, Any]:
    return {"x-agent-tool": True, "summary": "Does a thing.", **operation}


def _one(operation: dict[str, Any], method: str = "get", path: str = "/x") -> dict[str, Any]:
    """A document with a single marked operation (operationId `x_op` unless given)."""
    return {
        "openapi": "3.0.3",
        "paths": {path: {method: _marked({"operationId": "x_op", **operation})}},
    }


# --- which operations become tools ---


def test_only_operations_marked_x_agent_tool_become_tools() -> None:
    manifest = load_openapi_manifest(_spec())

    assert set(_tools(manifest)) == {
        "list_books",
        "get_book",
        "cancel_book",
        "recall_reviews",
        "reserve_book",
        "search_books",
    }  # admin_stats is not marked


def test_the_tool_name_is_x_agent_name_else_the_operation_id() -> None:
    tools = _tools(load_openapi_manifest(_spec()))

    assert "get_book" in tools and "books_get_one_internal_v1_books__book_id__get" not in tools
    assert tools["list_books"].route == "/internal/v1/books"


def test_effect_defaults_from_the_method_and_x_agent_effect_overrides_it() -> None:
    tools = _tools(load_openapi_manifest(_spec()))

    assert (tools["list_books"].kind, tools["list_books"].replay) == ("read", "safe")
    assert (tools["reserve_book"].kind, tools["reserve_book"].replay) == ("write", "interrupted")
    assert tools["cancel_book"].kind == "write" and tools["cancel_book"].method == "DELETE"
    assert tools["search_books"].method == "POST" and tools["search_books"].kind == "read"


def test_description_is_x_agent_description_then_the_summary_then_the_description() -> None:
    spec = _spec()
    _operation(spec, "/internal/v1/books", "get")["x-agent-description"] = "Newest books."
    tools = _tools(load_openapi_manifest(spec))

    assert tools["list_books"].description == "Newest books."
    assert tools["recall_reviews"].description.startswith("Reviews of a book")
    (tool,) = load_openapi_manifest(
        {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "get": {
                        "x-agent-tool": True,
                        "operationId": "x_op",
                        "description": "From the description.",
                    }
                }
            },
        }
    ).tools
    assert tool.description == "From the description."


def test_the_x_agent_marks_map_to_the_manifest_fields() -> None:
    tools = _tools(load_openapi_manifest(_spec()))

    listing = tools["list_books"].response
    assert listing.pick == ("id", "title", "author")
    assert (listing.rows_key, listing.row_limit) == ("items", 3)
    assert tools["recall_reviews"].warning is True and tools["list_books"].warning is False
    assert tools["recall_reviews"].context == ("session", "agent")
    assert tools["list_books"].context == ("session", "agent", "tenant")  # the manifest default
    assert tools["reserve_book"].idempotency == ("book_id", "slot")


def test_x_agent_replay_overrides_the_default() -> None:
    spec = _spec()
    _operation(spec, "/internal/v1/reservations", "post")["x-agent-replay"] = "safe"

    assert _tools(load_openapi_manifest(spec))["reserve_book"].replay == "safe"


# --- arguments ---


def test_parameters_and_refs_become_typed_arguments_and_header_parameters_are_left_out() -> None:
    args = _tools(load_openapi_manifest(_spec()))["list_books"].arguments

    assert set(args) == {"limit", "genre"}  # X-Agent-Tenant is the bridge's, not the model's
    assert (args["limit"].type, args["limit"].where) == ("integer", "query")
    assert (args["limit"].minimum, args["limit"].maximum, args["limit"].default) == (1, 20, 10)
    assert args["limit"].description == "Rows wanted."
    assert args["genre"].enum == ("fiction", "science", "history")  # through a $ref


def test_a_path_parameter_comes_from_the_path_item_through_a_ref_and_is_required() -> None:
    tools = _tools(load_openapi_manifest(_spec()))

    for name in ("get_book", "cancel_book"):
        arg = tools[name].arguments["book_id"]
        assert (arg.where, arg.required, arg.type) == ("path", True, "string")
        assert tools[name].route == "/internal/v1/books/{book_id}"


def test_an_operation_parameter_overrides_the_path_item_one_of_the_same_name() -> None:
    spec = _spec()
    _operation(spec, "/internal/v1/books/{book_id}", "get")["parameters"] = [
        {"name": "book_id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ]
    tools = _tools(load_openapi_manifest(spec))

    assert tools["get_book"].arguments["book_id"].type == "integer"
    assert tools["cancel_book"].arguments["book_id"].type == "string"


def test_a_json_object_body_becomes_body_arguments_with_their_types() -> None:
    args = _tools(load_openapi_manifest(_spec()))["reserve_book"].arguments

    assert {n: (a.type, a.where, a.required) for n, a in args.items()} == {
        "book_id": ("string", "body", True),
        "slot": ("string", "body", True),
        "note": ("string", "body", False),  # anyOf [string, null] is an optional string
        "notify": ("boolean", "body", False),
        "seats": ("integer", "body", False),
        "ratio": ("number", "body", False),
        "tags": ("array", "body", False),
    }
    assert args["slot"].enum == ("am", "pm") and args["note"].default == ""
    assert (args["seats"].minimum, args["seats"].maximum) == (1, 6)
    assert args["notify"].description == "Send a confirmation."


def test_a_type_list_with_null_is_the_non_null_type() -> None:
    spec = _one(
        {"parameters": [{"name": "q", "in": "query", "schema": {"type": ["string", "null"]}}]}
    )

    assert _tools(load_openapi_manifest(spec))["x_op"].arguments["q"].type == "string"


# --- the contract check: unusable documents fail at load, naming the operation ---


def _refused(spec: dict[str, Any], match: str) -> OpenApiContractError:
    with pytest.raises(OpenApiContractError, match=match) as caught:
        load_openapi_manifest(spec)
    return caught.value


def test_a_document_that_is_not_openapi_3_is_refused() -> None:
    _refused({"swagger": "2.0", "paths": {}}, "OpenAPI 3")
    _refused({"openapi": "3.0.0"}, "paths")


def test_no_marked_operation_is_an_error_not_an_empty_toolset() -> None:
    _refused({"openapi": "3.0.0", "paths": {"/x": {"get": {"summary": "s"}}}}, "x-agent-tool")


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        (
            {"parameters": [{"name": "c", "in": "cookie", "schema": {"type": "string"}}]},
            "cookie",
        ),
        (
            {
                "parameters": [
                    {
                        "name": "a",
                        "in": "query",
                        "schema": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                    }
                ]
            },
            "oneOf",
        ),
        (
            {
                "parameters": [
                    {
                        "name": "a",
                        "in": "query",
                        "schema": {"type": "integer", "exclusiveMinimum": 1},
                    }
                ]
            },
            "exclusive",
        ),
        ({"parameters": [{"name": "a", "in": "query", "schema": {}}]}, "no type"),
        (
            {
                "parameters": [
                    {
                        "name": "a",
                        "in": "query",
                        "schema": {"type": "string", "format": "x", "$ref": "http://e.test/s.json"},
                    }
                ]
            },
            "remote",
        ),
        (
            {
                "parameters": [
                    {"name": "a", "in": "query", "schema": {"$ref": "#/components/schemas/Missing"}}
                ]
            },
            "Missing",
        ),
        ({"x-agent-effect": "delete"}, "x-agent-effect"),
        ({"x-agent-name": "Not-A-Name"}, "name"),
        ({"x-agent-tool": "yes"}, "x-agent-tool"),
        ({"summary": ""}, "description"),
    ],
)
def test_an_operation_that_cannot_be_mapped_fails_naming_the_operation(
    operation: dict[str, Any], match: str
) -> None:
    spec = {
        "openapi": "3.0.0",
        "paths": {"/things": {"get": {"operationId": "the_op", **operation}}},
    }
    spec["paths"]["/things"]["get"].setdefault("x-agent-tool", True)
    spec["paths"]["/things"]["get"].setdefault("summary", "Things.")

    error = _refused(spec, match)

    assert "GET /things" in str(error) and "the_op" in str(error)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"content": {"text/plain": {"schema": {"type": "string"}}}}, "application/json"),
        ({"content": {"application/json": {"schema": {"type": "array"}}}}, "object"),
        ({"content": {"application/json": {"schema": {"type": "object"}}}}, "properties"),
        (
            {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}, "q": {"type": "string"}},
                        }
                    }
                }
            },
            "q",
        ),
    ],
)
def test_a_request_body_that_is_not_a_flat_json_object_fails_naming_the_operation(
    body: dict[str, Any], match: str
) -> None:
    spec = _one(
        {
            "operationId": "make",
            "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
            "requestBody": body,
        },
        method="post",
        path="/things",
    )

    error = _refused(spec, match)

    assert "POST /things" in str(error)


def test_a_write_marked_get_is_refused_with_the_operation_named() -> None:
    spec = _one({"operationId": "poke", "x-agent-effect": "write"}, path="/poke")

    error = _refused(spec, "cannot use GET")

    assert "GET /poke" in str(error)


def test_duplicate_tool_names_fail_naming_both_operations() -> None:
    spec = _spec()
    _operation(spec, "/internal/v1/search", "post")["x-agent-name"] = "list_books"

    error = _refused(spec, "list_books")

    assert "POST /internal/v1/search" in str(error) and "GET /internal/v1/books" in str(error)


def test_expected_tools_must_exist() -> None:
    with pytest.raises(OpenApiContractError, match="nope_tool.*not.*x-agent-tool"):
        load_openapi_manifest(_spec(), expect=["get_book", "nope_tool"])

    assert load_openapi_manifest(_spec(), expect=["get_book"]).tools


# --- the manual manifest as an overlay ---


def test_an_overlay_entry_with_a_generated_name_patches_it_field_by_field() -> None:
    overlay = {
        "tools": [
            {
                "name": "list_books",
                "description": "Overridden text.",
                "arguments": {"limit": {"type": "integer", "in": "query", "default": 5}},
                "response": {"pick": ["id"]},
            }
        ]
    }

    tool = _tools(load_openapi_manifest(_spec(), overlay=overlay))["list_books"]

    assert tool.description == "Overridden text."
    assert tool.arguments["limit"].default == 5
    assert "genre" in tool.arguments  # arguments the overlay did not name are kept
    assert tool.response.pick == ("id",) and tool.response.rows_key == "items"  # merged by key
    assert tool.route == "/internal/v1/books"


def test_an_overlay_entry_with_no_generated_twin_is_a_manual_tool_and_gets_the_overlay_prefix() -> (
    None
):
    overlay = {
        "api_prefix": "/internal/v1",
        "tools": [
            {
                "name": "ping",
                "description": "Health probe.",
                "method": "GET",
                "route": "/ping",
                "kind": "read",
                "context": [],
            }
        ],
    }

    tools = _tools(load_openapi_manifest(_spec(), overlay=overlay))

    assert tools["ping"].route == "/internal/v1/ping" and "get_book" in tools


def test_an_overlay_patch_for_an_operation_that_is_not_marked_fails_naming_the_tool() -> None:
    overlay = {"tools": [{"name": "gone_tool", "description": "Patch of nothing."}]}

    with pytest.raises(OpenApiContractError, match="gone_tool") as caught:
        load_openapi_manifest(_spec(), overlay=overlay)

    assert "list_books" in str(caught.value)  # what the document does mark is listed


def test_an_overlay_operation_pin_that_no_longer_matches_fails() -> None:
    pinned = {"tools": [{"name": "get_book", "operation": "GET /internal/v1/books/{book_id}"}]}
    stale = {"tools": [{"name": "get_book", "operation": "GET /internal/v1/book/{id}"}]}

    assert load_openapi_manifest(_spec(), overlay=pinned).tools
    with pytest.raises(OpenApiContractError, match="get_book.*GET /internal/v1/book/"):
        load_openapi_manifest(_spec(), overlay=stale)


def test_an_overlay_may_be_a_yaml_file_and_may_set_manifest_defaults(tmp_path: Path) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        "defaults: {context: [session]}\ntools:\n  - name: get_book\n    warning: true\n",
        encoding="utf-8",
    )

    tools = _tools(load_openapi_manifest(_spec(), overlay=path))

    assert tools["get_book"].warning is True
    assert tools["get_book"].context == ("session",) == tools["list_books"].context


# --- where the document comes from ---


def test_the_document_loads_from_a_json_or_yaml_file(tmp_path: Path) -> None:
    yaml_path = tmp_path / "api.yaml"
    yaml_path.write_text(
        "openapi: 3.0.3\npaths:\n  /x:\n    get:\n      operationId: only_one\n"
        "      summary: One.\n      x-agent-tool: true\n",
        encoding="utf-8",
    )

    assert "get_book" in _tools(load_openapi_manifest(FIXTURE))
    assert set(_tools(load_openapi_manifest(yaml_path))) == {"only_one"}
    assert set(_tools(load_openapi_manifest(str(yaml_path)))) == {"only_one"}


def test_a_missing_or_malformed_file_is_a_manifest_error_naming_it(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    for source, name in ((tmp_path / "absent.json", "absent.json"), (bad, "bad.json")):
        with pytest.raises(OpenApiContractError, match=name):
            load_openapi_manifest(source)


def test_a_url_is_fetched_once_with_the_given_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_spec())

    manifest = load_openapi_manifest(
        "http://svc.test:9/openapi.json", transport=httpx.MockTransport(handler)
    )

    assert "get_book" in _tools(manifest)
    assert [str(r.url) for r in seen] == ["http://svc.test:9/openapi.json"]


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(404, text="no"),
        lambda r: httpx.Response(200, text="<html>"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    ],
)
def test_a_url_that_cannot_be_fetched_or_parsed_is_a_manifest_error_naming_the_url(
    handler: Any,
) -> None:
    with pytest.raises(OpenApiContractError, match="svc.test"):
        load_openapi_manifest(
            "http://svc.test:9/openapi.json", transport=httpx.MockTransport(handler)
        )


def test_only_http_urls_are_fetched() -> None:
    with pytest.raises(OpenApiContractError, match="ftp"):
        load_openapi_manifest("ftp://svc.test/openapi.json")


# --- the consumer API and a round trip ---


class Backend:
    def __init__(self, body: Any = None, status: int = 200, headers: dict[str, str] | None = None):
        self.requests: list[httpx.Request] = []
        self.body, self.status, self.headers = body, status, headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def _run(toolset: Any, name: str, **arguments: Any) -> Any:
    return asyncio.run(toolset.tools[name].function(_ctx(), **arguments))


def test_a_generated_tool_round_trips_through_the_bridge() -> None:
    backend = Backend({"id": "b1", "title": "T", "author": "A", "year": 1999, "secret": "no"})
    toolset = build_bridge_toolset(
        SETTINGS, openapi=_spec(), transport=httpx.MockTransport(backend)
    )

    result = _run(toolset, "get_book", book_id="b/1")

    assert result == {"id": "b1", "title": "T", "author": "A", "year": 1999}
    (request,) = backend.requests
    assert request.url.raw_path == b"/internal/v1/books/b%2F1"
    assert request.headers["X-Agent-Session"] == "case-1"
    assert request.headers["X-Agent-Id"] == "clerk" and request.headers["X-Agent-Tenant"] == "T9"


def test_a_generated_write_sends_the_body_and_a_deterministic_idempotency_key() -> None:
    backend = Backend({"reservation_id": "r1"})
    toolset = build_bridge_toolset(
        SETTINGS, openapi=_spec(), transport=httpx.MockTransport(backend)
    )

    _run(toolset, "reserve_book", book_id="b1", slot="am", seats=99)
    _run(toolset, "reserve_book", book_id="b1", slot="am", seats=2)
    bad = _run(toolset, "reserve_book", book_id="b1", slot="night")

    one, two = backend.requests
    assert json.loads(one.read())["seats"] == 6  # clamped by the schema's maximum
    assert one.headers["Idempotency-Key"] == two.headers["Idempotency-Key"]
    assert "slot must be one of am, pm" in bad["error"] and len(backend.requests) == 2


def test_the_model_schema_and_replay_policy_come_from_the_document() -> None:
    toolset = build_bridge_toolset(
        SETTINGS, openapi=_spec(), transport=httpx.MockTransport(Backend({}))
    )

    schema = toolset.tools["list_books"].function_schema.json_schema
    assert schema["properties"]["genre"]["enum"] == ["fiction", "science", "history"]
    assert schema["properties"]["limit"]["maximum"] == 20
    assert "X-Agent-Tenant" not in json.dumps(schema)
    assert replay_policy(toolset.tools["list_books"].metadata) is ReplayPolicy.SAFE
    assert replay_policy(toolset.tools["reserve_book"].metadata) is not ReplayPolicy.SAFE


def test_a_problem_json_error_of_a_generated_tool_stays_an_error_value() -> None:
    backend = Backend(
        {"type": "t", "title": "Not found", "status": 404, "detail": "no such book"},
        status=404,
        headers={"content-type": "application/problem+json"},
    )
    toolset = build_bridge_toolset(
        SETTINGS, openapi=_spec(), transport=httpx.MockTransport(backend)
    )

    result = _run(toolset, "get_book", book_id="zz")

    assert result["status"] == 404 and result["problem"]["title"] == "Not found"


def test_the_document_is_fetched_once_at_build_time_not_per_call() -> None:
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.path)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=_spec())
        return httpx.Response(200, json={"id": "b1"})

    toolset = build_bridge_toolset(
        SETTINGS,
        openapi="http://svc.test:9/openapi.json",
        transport=httpx.MockTransport(handler),
    )
    _run(toolset, "get_book", book_id="b1")
    _run(toolset, "get_book", book_id="b2")

    assert fetched == ["/openapi.json", "/internal/v1/books/b1", "/internal/v1/books/b2"]


def test_overlay_and_expect_reach_the_consumer_api(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("tools:\n  - name: get_book\n    description: Patched.\n", encoding="utf-8")
    toolset = build_bridge_toolset(
        SETTINGS,
        openapi=_spec(),
        overlay=overlay,
        expect=["get_book"],
        transport=httpx.MockTransport(Backend({})),
    )

    assert toolset.tools["get_book"].description == "Patched."
    with pytest.raises(OpenApiContractError, match="absent_tool"):
        build_bridge_toolset(SETTINGS, openapi=_spec(), expect=["absent_tool"])


def test_exactly_one_source_is_required_and_an_overlay_needs_the_document(tmp_path: Path) -> None:
    manual = tmp_path / "tools.yaml"
    manual.write_text("tools: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one"):
        build_bridge_toolset(SETTINGS)
    with pytest.raises(ValueError, match="exactly one"):
        build_bridge_toolset(SETTINGS, manifest=manual, openapi=_spec())
    with pytest.raises(ValueError, match="overlay"):
        build_bridge_toolset(SETTINGS, manifest=manual, overlay=manual)


def test_the_loader_does_not_mutate_the_document_it_is_given() -> None:
    spec = _spec()
    before = copy.deepcopy(spec)

    load_openapi_manifest(spec)

    assert spec == before


def test_x_agent_idempotency_true_keys_the_write_on_all_its_arguments() -> None:
    spec = _one(
        {
            "x-agent-idempotency": True,
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                        }
                    }
                }
            },
        },
        method="post",
    )

    assert _tools(load_openapi_manifest(spec))["x_op"].idempotency == ("a", "b")


# --- G1c: slice 27 advisories ---


class _AsyncOnly(httpx.AsyncBaseTransport):
    """A transport that has no synchronous side (unlike httpx.MockTransport)."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.seen: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return httpx.Response(200, json=self.document)


URL = "http://svc.test:9/openapi.json"


def test_an_async_only_transport_serves_the_openapi_fetch() -> None:
    transport = _AsyncOnly(_spec())

    manifest = load_openapi_manifest(URL, transport=transport)

    assert "get_book" in _tools(manifest)
    assert transport.seen == [URL]


def test_an_async_only_transport_reaches_the_fetch_through_build_bridge_toolset() -> None:
    transport = _AsyncOnly(_spec())

    toolset = build_bridge_toolset(SETTINGS, openapi=URL, transport=transport)

    assert "get_book" in toolset.tools and transport.seen == [URL]


def test_an_async_only_transport_works_when_the_toolset_is_built_inside_a_running_loop() -> None:
    transport = _AsyncOnly(_spec())

    async def build() -> Any:
        return build_bridge_toolset(SETTINGS, openapi=URL, transport=transport)

    assert "get_book" in asyncio.run(build()).tools


def test_the_same_url_is_fetched_once_per_process_across_builds() -> None:
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, json=_spec())

    for _ in range(3):
        build_bridge_toolset(SETTINGS, openapi=URL, transport=httpx.MockTransport(handler))
    build_bridge_toolset(
        SETTINGS, openapi="http://other.test:9/openapi.json", transport=httpx.MockTransport(handler)
    )

    assert fetched == [URL, "http://other.test:9/openapi.json"]


def test_a_cached_document_is_not_shared_mutably_between_builds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_spec())

    transport = httpx.MockTransport(handler)
    overlay = {"tools": [{"name": "get_book", "description": "Patched."}]}
    patched = build_bridge_toolset(SETTINGS, openapi=URL, overlay=overlay, transport=transport)
    plain = build_bridge_toolset(SETTINGS, openapi=URL, transport=transport)

    assert patched.tools["get_book"].description == "Patched."
    assert plain.tools["get_book"].description != "Patched."


def test_a_failed_fetch_is_not_cached_and_the_error_says_the_backend_must_answer_at_startup() -> (
    None
):
    answers: list[Exception | None] = [httpx.ConnectError("down"), None]

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = answers.pop(0)
        if outcome is not None:
            raise outcome
        return httpx.Response(200, json=_spec())

    transport = httpx.MockTransport(handler)
    with pytest.raises(OpenApiContractError, match="svc.test.*startup"):
        load_openapi_manifest(URL, transport=transport)

    assert "get_book" in _tools(load_openapi_manifest(URL, transport=transport))


def test_clearing_the_cache_fetches_again() -> None:
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, json=_spec())

    transport = httpx.MockTransport(handler)
    load_openapi_manifest(URL, transport=transport)
    clear_openapi_cache()
    load_openapi_manifest(URL, transport=transport)

    assert len(fetched) == 2


@pytest.mark.parametrize(
    "parameters",
    [
        "name",
        {"name": "x"},
        [3],
        ["x"],
        [None],
        [{"in": "query", "schema": {"type": "string"}}],
        [{"name": "x", "schema": {"type": "string"}}],
        [{"name": 5, "in": "query", "schema": {"type": "string"}}],
        [{"name": "x", "in": ["query"], "schema": {"type": "string"}}],
        [{"name": "x", "in": "query", "schema": "string"}],
        [{"name": "x", "in": "query", "schema": None}],
    ],
)
def test_malformed_parameters_fail_naming_the_operation(parameters: Any) -> None:
    spec = _one({"parameters": parameters})

    with pytest.raises(OpenApiContractError, match="GET /x .x_op."):
        load_openapi_manifest(spec)


def test_malformed_path_item_parameters_fail_naming_the_operation() -> None:
    spec = _one({})
    spec["paths"]["/x"]["parameters"] = "oops"

    with pytest.raises(OpenApiContractError, match="GET /x .x_op."):
        load_openapi_manifest(spec)


@pytest.mark.parametrize("content", [[], "json", {"application/json": "schema"}])
def test_a_malformed_request_body_content_fails_naming_the_operation(content: Any) -> None:
    spec = _one({"requestBody": {"content": content}}, method="post")

    with pytest.raises(OpenApiContractError, match="POST /x .x_op."):
        load_openapi_manifest(spec)


@pytest.mark.parametrize(
    ("overlay", "needle"),
    [
        ({"tools": "get_book"}, "tools"),
        ({"tools": ["get_book"]}, "overlay tool"),
        ({"tools": [None]}, "overlay tool"),
        ({"tools": [{"description": "no name"}]}, "name"),
        ({"tools": [{"name": 7}]}, "name"),
        ({"tools": [{"name": ""}]}, "name"),
        ({"tools": [{"name": "get_book", "arguments": ["limit"]}]}, "get_book.*arguments"),
        (
            {"tools": [{"name": "get_book", "arguments": {"book_id": "string"}}]},
            "get_book.*book_id",
        ),
        ({"tools": [{"name": "get_book", "response": "pick"}]}, "get_book.*response"),
        ({"tools": [{"name": "get_book", "operation": 5}]}, "get_book.*operation"),
        ({"defaults": "x", "tools": []}, "defaults"),
        ({"api_prefix": 3, "tools": []}, "api_prefix"),
    ],
)
def test_an_overlay_of_the_wrong_shape_is_a_contract_error_naming_what_is_wrong(
    overlay: Any, needle: str
) -> None:
    with pytest.raises(OpenApiContractError, match=needle):
        load_openapi_manifest(_spec(), overlay=overlay)
