"""The generic HTTP client of the tool bridge (Phase 3, G1, D83).

Behaviour ported from the first vertical's client, with every name made a setting. No network:
`httpx.MockTransport` stands in for the backend.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.http_bridge import (
    BridgeSettings,
    call_backend,
    case_id_for,
)
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId


def _ctx(session_id: str = "case-77", agent: str = "clerk") -> Any:
    deps = TurnContext(
        caller=CallerIdentity("m-1", "web", TenantId("T9"), frozenset()),
        session=SessionRef(session_id=session_id, tenant_id=TenantId("T9")),  # type: ignore[arg-type]
        agent_id=agent,
    )
    return SimpleNamespace(deps=deps)


class Backend:
    def __init__(self, status: int = 200, body: Any = None, error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.status, self.body, self.error = status, {} if body is None else body, error
        self.headers: dict[str, str] = {}
        self.raw: bytes | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def _call(backend: Backend, settings: BridgeSettings | None = None, **kwargs: Any) -> Any:
    kwargs.setdefault("ctx", _ctx())
    ctx = kwargs.pop("ctx")
    method = kwargs.pop("method", "GET")
    path = kwargs.pop("path", "/x")
    return asyncio.run(
        call_backend(
            ctx,
            settings or BridgeSettings(base_url="http://svc.test:9/"),
            method,
            path,
            transport=httpx.MockTransport(backend),
            **kwargs,
        )
    )


def test_headers_carry_session_agent_and_tenant_under_the_configured_names() -> None:
    backend = Backend()
    settings = BridgeSettings(
        base_url="http://svc.test:9/",
        session_header="X-Case",
        agent_header="X-Agent",
        tenant_header="X-Store",
    )

    _call(backend, settings, path="/internal/v1/kpis", params={"a": "1"})

    (req,) = backend.requests
    assert str(req.url) == "http://svc.test:9/internal/v1/kpis?a=1"
    assert req.headers["X-Case"] == "case-77"
    assert req.headers["X-Agent"] == "clerk"
    assert req.headers["X-Store"] == "T9"


def test_a_context_not_listed_is_not_sent_and_an_empty_header_name_disables_it() -> None:
    backend = Backend()
    settings = BridgeSettings(base_url="http://svc.test:9", subject_header="")

    _call(backend, settings, context=("session", "subject"))

    (req,) = backend.requests
    assert "x-agent-session" in req.headers
    assert "x-agent-id" not in req.headers and "x-agent-tenant" not in req.headers
    assert "x-subject-id" not in req.headers  # asked for, but the deployment disabled the name


def test_the_subject_header_is_sent_when_it_is_named() -> None:
    backend = Backend()
    settings = BridgeSettings(base_url="http://svc.test:9", subject_header="X-User")

    _call(backend, settings, context=("subject",))

    assert backend.requests[0].headers["X-User"] == "m-1"


def test_a_peer_session_resolves_to_the_asking_session_when_a_prefix_is_set() -> None:
    assert case_id_for("peer~peer~case-1~aaaa~bbbb", "peer~", "~") == "case-1"
    assert case_id_for("peer~case-1~uuid", "peer~", "~") == "case-1"
    assert case_id_for("case-1", "peer~", "~") == "case-1"
    assert case_id_for("peer~case-1~uuid") == "peer~case-1~uuid"  # no prefix configured: unchanged

    backend = Backend()
    settings = BridgeSettings(base_url="http://svc.test:9", peer_session_prefix="peer~")
    _call(backend, settings, ctx=_ctx("peer~case-9~u-1"))

    assert backend.requests[0].headers["X-Agent-Session"] == "case-9"


def test_none_params_are_dropped_and_a_body_is_sent_as_json() -> None:
    backend = Backend()

    _call(backend, method="POST", params={"a": None, "b": 2}, body={"k": "v"})

    (req,) = backend.requests
    assert dict(req.url.params) == {"b": "2"}
    assert req.content == b'{"k":"v"}'


def test_the_idempotency_key_is_deterministic_per_session_and_parts() -> None:
    first, again, other, other_session = Backend(), Backend(), Backend(), Backend()

    _call(first, method="POST", idempotency_parts=("i1", "o1"))
    _call(again, method="POST", idempotency_parts=("i1", "o1"))
    _call(other, method="POST", idempotency_parts=("i1", "o2"))
    _call(other_session, method="POST", idempotency_parts=("i1", "o1"), ctx=_ctx("case-78"))

    key = first.requests[0].headers["Idempotency-Key"]
    assert len(key) == 32 and int(key, 16) >= 0
    assert again.requests[0].headers["Idempotency-Key"] == key
    assert other.requests[0].headers["Idempotency-Key"] != key
    assert other_session.requests[0].headers["Idempotency-Key"] != key


def test_no_idempotency_header_without_parts_and_its_name_is_a_setting() -> None:
    plain, named = Backend(), Backend()

    _call(plain)
    _call(
        named,
        BridgeSettings(base_url="http://svc.test:9", idempotency_header="X-Dedupe"),
        method="POST",
        idempotency_parts=("a",),
    )

    assert "idempotency-key" not in plain.requests[0].headers
    assert "X-Dedupe" in named.requests[0].headers


def test_the_warning_header_is_returned_beside_the_data() -> None:
    backend = Backend(body={"rows": []})
    backend.headers = {"X-Agent-Warning": "memory degraded"}

    reply = _call(backend, want_warning=True)

    assert reply.error is None and reply.data == {"rows": []}
    assert reply.warning == "memory degraded"
    quiet = _call(Backend(body=[1]), want_warning=True)
    assert quiet.warning is None


def test_a_missing_turn_context_is_an_error_value() -> None:
    reply = _call(Backend(), ctx=SimpleNamespace(deps=None))

    assert reply.error is not None and "turn context" in reply.error["error"]


def test_errors_are_values_never_exceptions() -> None:
    timeout = _call(Backend(error=httpx.ReadTimeout("slow")))
    network = _call(Backend(error=httpx.ConnectError("down")))
    crash = _call(Backend(error=RuntimeError("boom")))
    refused = _call(Backend(status=404, body={"detail": "nope"}))
    big = _call(Backend(status=500, body={"d": "x" * 900}))
    html = Backend()
    html.raw = b"<html>"
    non_json = _call(html)

    assert timeout.error is not None and "timed out" in timeout.error["error"]
    assert network.error is not None and "unreachable" in network.error["error"]
    assert "ConnectError" in network.error["error"]
    assert crash.error is not None and "RuntimeError" in crash.error["error"]
    assert refused.error is not None and refused.error["status"] == 404
    assert "HTTP 404" in refused.error["error"]
    assert big.error is not None and len(big.error["error"]) < 420
    assert non_json.error is not None and "non-JSON" in non_json.error["error"]


def test_error_messages_name_the_service_from_the_settings() -> None:
    settings = BridgeSettings(base_url="http://svc.test:9", service_name="Library")

    reply = _call(Backend(error=httpx.ReadTimeout("slow")), settings)

    assert reply.error is not None and "Library" in reply.error["error"]
    assert "Glazed" not in reply.error["error"]


def test_a_problem_json_body_becomes_a_structured_error_value() -> None:
    backend = Backend(
        status=409,
        body={"type": "urn:x:conflict", "title": "Conflict", "detail": "slot taken", "extra": 1},
    )
    backend.headers = {"content-type": "application/problem+json"}

    reply = _call(backend)

    assert reply.error is not None and reply.error["status"] == 409
    assert "Conflict: slot taken" in reply.error["error"]
    assert reply.error["problem"] == {
        "type": "urn:x:conflict",
        "title": "Conflict",
        "detail": "slot taken",
    }


def test_a_plain_error_body_has_no_problem_key() -> None:
    reply = _call(Backend(status=500, body={"title": "not a problem document"}))

    assert reply.error is not None and "problem" not in reply.error


def test_an_empty_peer_separator_is_refused_when_a_prefix_is_set_and_harmless_otherwise() -> None:
    with pytest.raises(ValueError, match="peer_session_separator"):
        BridgeSettings(peer_session_prefix="peer~", peer_session_separator="")
    with pytest.raises(ValueError, match="peer_session_separator"):
        BridgeSettings.from_mapping({"peer_session_prefix": "peer~", "peer_session_separator": ""})

    assert BridgeSettings(peer_session_separator="").peer_session_prefix == ""


def test_case_id_for_never_raises_on_an_empty_separator() -> None:
    assert case_id_for("peer~case-1", "peer~", "") == "case-1"
    assert case_id_for("case-1", "peer~", "") == "case-1"
