"""Generic HTTP access to a backend, bound to the running turn (Phase 3, G1, D83).

Phase:   Phase 3 (white-label addons)
Tests:   Core/tests/unit/test_http_bridge_client.py
Implements: no port - the transport of the HTTP tool bridge.

HOW THE IDENTITY REACHES A CALL WITHOUT THE MODEL CHOOSING IT
    The runner passes `TurnContext` as `ctx.deps` (tools/context.py). From it the client builds the
    headers a tool asked for (`session`, `agent`, `tenant`, `subject`) under the names the
    deployment configured. The model never supplies them: no tool exposes such a parameter.

NEVER RAISES
    A raising tool strands the whole turn as running, so every failure is a `Reply` carrying an
    `{"error": ..., "status"?: ...}` value the model can read and say.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.http_bridge.settings import BridgeSettings

__all__ = ["Reply", "call_backend", "case_id_for"]

_BODY_EXCERPT = 300


@dataclass(frozen=True)
class Reply:
    """Either `data` (with the service warning, when asked for) or an `error` value."""

    data: Any = None
    warning: str | None = None
    error: dict[str, Any] | None = None


def case_id_for(session_id: str, prefix: str = "", separator: str = "~") -> str:
    """The session a call belongs to: itself, or the asker's id inside a peer session id."""
    sid = str(session_id)
    while prefix and sid.startswith(prefix):
        sid = sid[len(prefix) :].rsplit(separator, 1)[0]
    return sid


def _turn(ctx: Any) -> TurnContext | None:
    deps = getattr(ctx, "deps", None)
    return deps if isinstance(deps, TurnContext) else None


def _context_headers(
    turn: TurnContext, settings: BridgeSettings, sources: Sequence[str]
) -> dict[str, str]:
    values = {
        "session": (
            settings.session_header,
            case_id_for(
                turn.session.session_id,
                settings.peer_session_prefix,
                settings.peer_session_separator,
            ),
        ),
        "agent": (settings.agent_header, turn.agent_id),
        "tenant": (settings.tenant_header, str(turn.caller.tenant_id)),
        "subject": (settings.subject_header, str(turn.caller.subject_id)),
    }
    headers: dict[str, str] = {}
    for source in sources:
        name, value = values[source]
        if name:
            headers[name] = value
    return headers


def _failure(
    settings: BridgeSettings, head: str, status: int | None = None, tail: str = ""
) -> Reply:
    """`head` may hold `{service}`; `tail` (service-controlled text) is appended verbatim."""
    error: dict[str, Any] = {"error": head.replace("{service}", settings.service_name) + tail}
    if status is not None:
        error["status"] = status
    return Reply(error=error)


def _refusal(settings: BridgeSettings, response: httpx.Response) -> Reply:
    """An HTTP error as a value; an RFC 9457 `application/problem+json` body is kept structured."""
    head = f"The {{service}} refused the request: HTTP {response.status_code} "
    problem = _problem(response)
    if problem is None:
        return _failure(settings, head, response.status_code, response.text[:_BODY_EXCERPT])
    said = ": ".join(str(problem[k]) for k in ("title", "detail") if k in problem)
    reply = _failure(settings, head, response.status_code, said[:_BODY_EXCERPT])
    assert reply.error is not None
    return Reply(error={**reply.error, "problem": problem})


def _problem(response: httpx.Response) -> dict[str, Any] | None:
    if "application/problem+json" not in response.headers.get("content-type", "").lower():
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    problem = {k: body[k] for k in ("type", "title", "status", "detail") if k in body}
    return problem or None


async def call_backend(
    ctx: Any,
    settings: BridgeSettings,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    context: Sequence[str] = ("session", "agent", "tenant"),
    idempotency_parts: tuple[str, ...] | None = None,
    want_warning: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Reply:
    """One backend call. Always returns a `Reply`; never raises."""
    turn = _turn(ctx)
    if turn is None:
        return _failure(settings, "No turn context is available, so the backend call was not made.")
    headers = _context_headers(turn, settings, context)
    if idempotency_parts is not None and settings.idempotency_header:
        # The call may time out AFTER the backend stored the row; a retried identical call must
        # not create a second one. Same session + same parts => same key.
        case = case_id_for(
            turn.session.session_id, settings.peer_session_prefix, settings.peer_session_separator
        )
        digest = hashlib.sha256("\x1f".join((case, *idempotency_parts)).encode()).hexdigest()
        headers[settings.idempotency_header] = digest[:32]
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    timeout = httpx.Timeout(settings.timeout_seconds, connect=settings.connect_timeout_seconds)
    try:
        async with httpx.AsyncClient(
            base_url=settings.base_url, timeout=timeout, transport=transport
        ) as http:
            response = await http.request(
                method, path, params=clean or None, json=body, headers=headers
            )
    except httpx.TimeoutException:
        return _failure(settings, "The {service} timed out. Say the data could not be fetched.")
    except httpx.HTTPError as exc:
        return _failure(settings, f"The {{service}} is unreachable ({type(exc).__name__}).")
    except Exception as exc:  # noqa: BLE001 - a raising tool strands the whole turn as running
        return _failure(settings, f"The {{service}} call failed ({type(exc).__name__}).")
    if response.status_code >= 400:
        return _refusal(settings, response)
    try:
        data = response.json()
    except ValueError:
        return _failure(settings, "The {service} returned a non-JSON response.")
    warning = None
    if want_warning and settings.warning_header:
        warning = response.headers.get(settings.warning_header)
    return Reply(data=data, warning=warning)
