"""The toolset the bridge builds from a manifest: schema, requests, projection, errors as values."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.http_bridge import BridgeSettings, build_bridge_toolset
from agent_core.domain.tool_replay import ReplayPolicy, replay_policy
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId

EXAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "http_bridge" / "example_tools.yaml"
SETTINGS = BridgeSettings(base_url="http://svc.test:9", default_row_limit=20)


def _ctx(session_id: str = "case-1") -> Any:
    deps = TurnContext(
        caller=CallerIdentity("m-1", "web", TenantId("T9"), frozenset()),
        session=SessionRef(session_id=session_id, tenant_id=TenantId("T9")),  # type: ignore[arg-type]
        agent_id="clerk",
    )
    return SimpleNamespace(deps=deps)


class Backend:
    def __init__(self, body: Any = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.body, self.status = {} if body is None else body, status
        self.headers: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def _toolset(backend: Backend, settings: BridgeSettings = SETTINGS) -> Any:
    return build_bridge_toolset(settings, manifest=EXAMPLE, transport=httpx.MockTransport(backend))


def _run(toolset: Any, name: str, **arguments: Any) -> Any:
    return asyncio.run(toolset.tools[name].function(_ctx(), **arguments))


def test_one_tool_per_manifest_entry_with_name_description_and_schema() -> None:
    toolset = _toolset(Backend())

    assert set(toolset.tools) == {
        "list_books",
        "get_book",
        "recall_reviews",
        "reserve_book",
        "ping",
    }
    tool = toolset.tools["list_books"]
    assert tool.description.startswith("List books")
    schema = tool.function_schema.json_schema
    assert set(schema["properties"]) == {"limit", "genre"}  # never ctx, never a context header
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["genre"]["enum"] == ["fiction", "science", "history"]
    assert schema.get("required", []) == []
    assert toolset.tools["get_book"].function_schema.json_schema["required"] == ["book_id"]
    assert schema["additionalProperties"] is False


def test_reads_are_replay_safe_and_writes_are_not() -> None:
    toolset = _toolset(Backend())

    assert replay_policy(toolset.tools["get_book"].metadata) is ReplayPolicy.SAFE
    assert replay_policy(toolset.tools["reserve_book"].metadata) is ReplayPolicy.INTERRUPTED


def test_path_query_and_context_headers_reach_the_request() -> None:
    backend = Backend({"id": "b1", "title": "T", "secret": "x"})
    toolset = _toolset(backend)

    _run(toolset, "get_book", book_id="a b/c")
    _run(toolset, "list_books", genre="fiction", limit=5)

    first, second = backend.requests
    assert first.url.raw_path == b"/api/v1/books/a%20b%2Fc"
    assert first.headers["X-Agent-Session"] == "case-1"
    assert first.headers["X-Agent-Id"] == "clerk" and first.headers["X-Agent-Tenant"] == "T9"
    assert dict(second.url.params) == {"genre": "fiction", "limit": "5"}


def test_a_tool_with_an_empty_context_list_sends_no_context_headers() -> None:
    backend = Backend({"ok": True})

    _run(_toolset(backend), "ping")

    assert "x-agent-session" not in backend.requests[0].headers


def test_integers_are_clamped_defaults_applied_and_junk_never_raises() -> None:
    backend = Backend([])
    toolset = _toolset(backend)

    _run(toolset, "list_books", limit=999)
    _run(toolset, "list_books", limit=-4)
    _run(toolset, "list_books", limit="junk")
    _run(toolset, "list_books")

    limits = [dict(r.url.params)["limit"] for r in backend.requests]
    assert limits == ["20", "1", "10", "10"]  # clamp high, clamp low, junk -> default, default


def test_an_argument_outside_its_enum_is_an_error_value_and_no_call_is_made() -> None:
    backend = Backend()

    result = _run(_toolset(backend), "list_books", genre="poetry")

    assert "genre must be one of fiction, science, history" in result["error"]
    assert backend.requests == []


def test_a_missing_required_argument_is_an_error_value() -> None:
    backend = Backend()

    result = _run(_toolset(backend), "get_book")
    blank = _run(_toolset(backend), "get_book", book_id=None)

    assert "book_id" in result["error"] and "book_id" in blank["error"]
    assert backend.requests == []


def test_pick_keeps_only_the_allowlisted_keys_of_an_object() -> None:
    backend = Backend({"id": "b1", "title": "T", "author": "A", "year": 1999, "internal": "no"})

    result = _run(_toolset(backend), "get_book", book_id="b1")

    assert result == {"id": "b1", "title": "T", "author": "A", "year": 1999}


def test_pick_and_the_row_limit_apply_to_the_rows_under_rows_key() -> None:
    rows = [{"id": i, "title": f"t{i}", "author": "a", "isbn": "x"} for i in range(8)]
    backend = Backend({"items": rows, "total": 8})

    result = _run(_toolset(backend), "list_books")

    assert result["total"] == 8  # the envelope is kept
    assert [r["id"] for r in result["items"]] == [0, 1, 2]  # the tool's own row_limit is 3
    assert all(set(r) == {"id", "title", "author"} for r in result["items"])


def test_the_default_row_limit_is_a_setting_for_a_tool_that_declares_none() -> None:
    backend = Backend([{"a": i} for i in range(9)])
    settings = BridgeSettings(base_url="http://svc.test:9", default_row_limit=4)

    result = _run(_toolset(backend, settings), "recall_reviews", query="q")

    assert len(result["reviews"]) == 4


def test_an_unconfigured_tool_returns_the_body_untouched() -> None:
    backend = Backend({"a": 1, "b": [1, 2]})

    assert _run(_toolset(backend), "ping") == {"a": 1, "b": [1, 2]}


def test_the_warning_header_is_surfaced_to_the_model() -> None:
    backend = Backend([{"text": "good"}])
    backend.headers = {"X-Warning": "memory degraded"}
    toolset = _toolset(backend)

    degraded = _run(toolset, "recall_reviews", query="q")
    backend.headers = {}
    healthy = _run(toolset, "recall_reviews", query="q")

    assert degraded == {"reviews": [{"text": "good"}], "warning": "memory degraded"}
    assert healthy == {"reviews": [{"text": "good"}]}


def test_a_write_sends_a_json_body_and_a_deterministic_idempotency_key() -> None:
    backend = Backend({"reservation_id": "r1"})
    toolset = _toolset(backend)

    _run(toolset, "reserve_book", book_id="b1", slot="am")
    _run(toolset, "reserve_book", book_id="b1", slot="am", note="hi")
    _run(toolset, "reserve_book", book_id="b1", slot="pm")

    one, two, three = backend.requests
    assert one.method == "POST" and one.url.path == "/api/v1/reservations"
    assert one.read() == b'{"book_id":"b1","slot":"am","note":""}'
    assert two.read() == b'{"book_id":"b1","slot":"am","note":"hi"}'
    assert one.headers["Idempotency-Key"] == two.headers["Idempotency-Key"]  # note is not a part
    assert three.headers["Idempotency-Key"] != one.headers["Idempotency-Key"]


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_http_errors_come_back_as_values_with_the_status(status: int) -> None:
    result = _run(_toolset(Backend({"detail": "no"}, status=status)), "get_book", book_id="b1")

    assert result["status"] == status and f"HTTP {status}" in result["error"]


def test_a_transport_failure_comes_back_as_a_value() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    toolset = build_bridge_toolset(SETTINGS, manifest=EXAMPLE, transport=httpx.MockTransport(boom))

    assert "unreachable" in _run(toolset, "get_book", book_id="b1")["error"]


def test_settings_may_be_the_consumer_addons_validated_mapping_and_the_environment_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Backend({"id": 1})
    monkeypatch.setenv("AGENT_CORE_ADDONS", "http_tool_bridge")
    monkeypatch.setenv("AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__BASE_URL", "http://from-env.test:5")
    mapping = {"base_url": "http://from-consumer.test:7/", "label": "a key of the consumer"}
    toolset = build_bridge_toolset(
        mapping, manifest=EXAMPLE, transport=httpx.MockTransport(backend)
    )

    _run(toolset, "get_book", book_id="b1")

    assert backend.requests[0].url.host == "from-consumer.test"


# --- slice 26 advisories (G1a, D84) ------------------------------------------------------------


def _yaml_toolset(tmp_path: Path, tools: str, backend: Backend, **kwargs: Any) -> Any:
    path = tmp_path / "tools.yaml"
    path.write_text("tools:\n" + tools, encoding="utf-8")
    return build_bridge_toolset(
        SETTINGS, manifest=path, transport=httpx.MockTransport(backend), **kwargs
    )


@pytest.mark.parametrize("value", ["..", ".", ""])
def test_a_dot_segment_or_empty_path_argument_is_an_error_value_and_no_call_is_made(
    value: str,
) -> None:
    backend = Backend({"id": 1})

    result = _run(_toolset(backend), "get_book", book_id=value)

    assert "book_id" in result["error"]
    assert backend.requests == []


@pytest.mark.parametrize(
    ("value", "raw_segment"),
    [
        ("a/b", b"a%2Fb"),
        ("../admin", b"..%2Fadmin"),
        ("..\\x", b"..%5Cx"),
        ("%2e%2e", b"%252e%252e"),
        ("%2e%2e/x", b"%252e%252e%2Fx"),
        ("a?x=1#f", b"a%3Fx%3D1%23f"),
        (" ..", b"%20.."),
    ],
)
def test_a_path_argument_stays_one_percent_encoded_segment_of_the_route(
    value: str, raw_segment: bytes
) -> None:
    backend = Backend({"id": 1})

    _run(_toolset(backend), "get_book", book_id=value)

    (request,) = backend.requests
    assert request.url.raw_path == b"/api/v1/books/" + raw_segment


def test_pick_with_a_reply_that_is_not_an_object_returns_an_error_never_the_reply() -> None:
    for body in ("secret", 42):
        result = _run(_toolset(Backend(body)), "get_book", book_id="b1")

        assert "secret" not in str(result) and "42" not in str(result)
        assert "expected shape" in result["error"]


def test_pick_on_a_bare_list_of_non_objects_returns_no_rows() -> None:
    assert _run(_toolset(Backend(["secret", 1])), "get_book", book_id="b1") == []


def test_pick_drops_rows_that_are_not_objects_and_keeps_only_picked_keys() -> None:
    rows = [{"id": 1, "title": "t", "isbn": "x"}, "secret", {"isbn": "only"}, ["secret"], None]
    result = _run(_toolset(Backend({"items": rows, "total": 5})), "list_books")

    assert result == {"items": [{"id": 1, "title": "t"}, {}], "total": 5}


def test_pick_is_top_level_only_a_picked_key_carries_its_whole_value_others_nothing() -> None:
    nested = {"id": 1, "title": {"en": "T", "internal": "i"}, "secret": {"x": 1}}

    result = _run(_toolset(Backend(nested)), "get_book", book_id="b1")

    assert result == {"id": 1, "title": {"en": "T", "internal": "i"}}


@pytest.mark.parametrize("body", [{"total": 1}, {"items": {"id": 1}}, {"items": "secret"}, [1]])
def test_a_rows_key_reply_without_the_rows_is_an_error_not_the_whole_body(body: Any) -> None:
    result = _run(_toolset(Backend(body)), "list_books")

    assert "expected shape" in result["error"] and "total" not in result


def test_a_tool_without_pick_still_returns_the_reply_untouched() -> None:
    assert _run(_toolset(Backend("plain")), "ping") == "plain"


FRACTIONAL = """\
  - name: pager
    description: Paged.
    method: GET
    route: /p
    kind: read
    arguments:
      limit: {type: integer, in: query, minimum: 1.5, maximum: 9.5}
      ratio: {type: number, in: query, minimum: 0.25, maximum: 0.75}
"""


def test_fractional_bounds_of_an_integer_round_inward_and_a_number_keeps_them(
    tmp_path: Path,
) -> None:
    backend = Backend([])
    toolset = _yaml_toolset(tmp_path, FRACTIONAL, backend)

    _run(toolset, "pager", limit=1, ratio=0.1)
    _run(toolset, "pager", limit=99, ratio=3)
    _run(toolset, "pager", limit=4, ratio=0.5)

    seen = [dict(r.url.params) for r in backend.requests]
    assert seen == [
        {"limit": "2", "ratio": "0.25"},
        {"limit": "9", "ratio": "0.75"},
        {"limit": "4", "ratio": "0.5"},
    ]
    schema = toolset.tools["pager"].function_schema.json_schema["properties"]["limit"]
    assert (schema["minimum"], schema["maximum"]) == (2, 9)


def test_an_integer_range_without_any_integer_is_refused_at_build_time(tmp_path: Path) -> None:
    tools = FRACTIONAL.replace("minimum: 1.5, maximum: 9.5", "minimum: 1.2, maximum: 1.8")

    with pytest.raises(ValueError, match="no integer"):
        _yaml_toolset(tmp_path, tools, Backend())


TYPED = """\
  - name: typed
    description: One of each argument type.
    method: POST
    route: /t
    kind: write
    arguments:
      ratio: {type: number, in: body, minimum: 0, maximum: 1}
      flag: {type: boolean, in: body}
      tags: {type: array, in: body}
      level: {type: integer, in: body, enum: [1, 2, 3]}
      mode: {type: string, in: body, enum: [fast, slow]}
      count: {type: integer, in: body, default: 7}
"""


def test_each_argument_type_is_passed_typed_and_a_wrong_one_is_an_error_value(
    tmp_path: Path,
) -> None:
    backend = Backend({})
    toolset = _yaml_toolset(tmp_path, TYPED, backend)

    ok = _run(toolset, "typed", ratio="0.5", flag=True, tags=["a", "b"], level=2, mode="fast")
    clamped = _run(toolset, "typed", ratio=7.5)

    assert ok == {}
    assert json.loads(backend.requests[0].read()) == {
        "ratio": 0.5,
        "flag": True,
        "tags": ["a", "b"],
        "level": 2,
        "mode": "fast",
        "count": 7,
    }
    assert clamped == {} and json.loads(backend.requests[1].read())["ratio"] == 1.0
    for bad, message in [
        ({"flag": "yes"}, "flag must be true or false"),
        ({"tags": "a"}, "tags must be a list"),
        ({"level": 4}, "level must be one of 1, 2, 3"),
        ({"mode": "warp"}, "mode must be one of fast, slow"),
        ({"ratio": "x"}, "ratio must be a number"),
        ({"ratio": float("nan")}, "ratio must be a number"),
    ]:
        before = len(backend.requests)
        assert _run(toolset, "typed", **bad) == {"error": message}
        assert len(backend.requests) == before


def test_the_schema_the_model_sees_carries_each_type_its_enum_and_bounds(tmp_path: Path) -> None:
    schema = _yaml_toolset(tmp_path, TYPED, Backend()).tools["typed"].function_schema.json_schema

    props = schema["properties"]
    assert props["ratio"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert props["flag"] == {"type": "boolean"} and props["tags"] == {"type": "array"}
    assert props["level"]["enum"] == [1, 2, 3] and props["mode"]["enum"] == ["fast", "slow"]
    assert props["count"] == {"type": "integer", "default": 7}
