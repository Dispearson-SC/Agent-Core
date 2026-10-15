"""The caller's data version: `X-Data-Version` on `POST /turns` (Phase 5, S10, roadmap 5.8).

Subject: domain/turn.py (`TurnRequest.data_version`, its pickled state) and the HTTP route.

THE RULES UNDER TEST. The route carries a trimmed `X-Data-Version` onto the request (absent or
blank: None); a value longer than 128 characters, or with control characters, is refused with 422
before any turn starts. A request pickled by an older build reads back with no data version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.routes import create_app
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)


@dataclass
class _Starter:
    started: list[TurnRequest] = field(default_factory=list)

    async def __call__(self, request: TurnRequest) -> Any:
        self.started.append(request)
        return type("H", (), {"turn_id": TurnId("t-1")})()


def _post(version: str | None) -> tuple[int, _Starter]:
    starter = _Starter()
    headers = {"X-Subject-Id": "u", "X-Tenant-Id": "t"}
    if version is not None:
        headers["X-Data-Version"] = version
    response = TestClient(create_app(start_turn=starter)).post(
        "/turns", json={"session_id": "case-1", "profile_id": "p", "text": "hola"}, headers=headers
    )
    return response.status_code, starter


def test_the_route_carries_the_data_version_onto_the_request() -> None:
    status, starter = _post(" 2026-10-15 ")
    assert status == 202
    assert starter.started[0].data_version == "2026-10-15"


def test_no_or_a_blank_data_version_is_none() -> None:
    assert _post(None)[1].started[0].data_version is None
    assert _post("  ")[1].started[0].data_version is None


def test_an_oversized_data_version_is_refused_before_any_turn_starts() -> None:
    status, starter = _post("x" * 129)
    assert status == 422 and starter.started == []


def test_a_request_pickled_before_the_data_version_reads_back_without_one() -> None:
    request = TurnRequest(
        session=SessionRef(SessionId("s"), TenantId("t")),
        caller=CallerIdentity("u", "http", TenantId("t")),
        profile_id="p",
        input=UserInput("hola"),
    )
    old = TurnRequest.__new__(TurnRequest)
    old.__setstate__([request.session, request.caller, "p", request.input, 0, None])
    assert old == request and old.data_version is None
