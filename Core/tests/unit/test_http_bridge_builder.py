"""The toolset the bridge builds from a manifest: schema, requests, projection, errors as values."""

from __future__ import annotations

import asyncio
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
    return build_bridge_toolset(EXAMPLE, settings, transport=httpx.MockTransport(backend))


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

    toolset = build_bridge_toolset(EXAMPLE, SETTINGS, transport=httpx.MockTransport(boom))

    assert "unreachable" in _run(toolset, "get_book", book_id="b1")["error"]


def test_settings_default_to_the_enabled_addon_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Backend({"id": 1})
    monkeypatch.setenv("AGENT_CORE_ADDONS", "http_tool_bridge")
    monkeypatch.setenv("AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__BASE_URL", "http://from-env.test:5")
    toolset = build_bridge_toolset(EXAMPLE, transport=httpx.MockTransport(backend))

    _run(toolset, "get_book", book_id="b1")

    assert backend.requests[0].url.host == "from-env.test"


def test_building_without_settings_and_without_the_addon_fails_at_build_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_CORE_ADDONS", raising=False)

    with pytest.raises(ValueError, match="http_tool_bridge"):
        build_bridge_toolset(EXAMPLE)
