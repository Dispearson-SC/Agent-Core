"""HTTP access to the Glazed backend's internal API, bound to the running turn.

HOW THE CASE AND THE STORE REACH A CALL WITHOUT THE MODEL CHOOSING THEM
    The runner passes `TurnContext` as `ctx.deps` (tools/context.py). From it:

      - store  = `caller.tenant_id`  -> header `X-Glazed-Store`
      - case   = the session id      -> header `X-Glazed-Case`
                 (the orchestrator's session id IS the case's core conversation id; a peer
                 turn's id is `peer~<asker session id>~<uuid>`, built by the peer worker
                 from the claimed row, so the specialist resolves to the asking case)
      - agent  = the running profile -> header `X-Glazed-Agent`

    `as_of` is never sent: the backend derives it from the case (its simulated today).
    No tool declares a store, case or as_of parameter.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from agent_core.adapters.driven.tools.context import TurnContext

__all__ = ["TRANSPORT", "backend_url", "call", "case_id_for"]

DEFAULT_BACKEND_URL = "http://backend:8080"
# Replaced in tests with an `httpx.MockTransport`; None means the real network.
TRANSPORT: httpx.AsyncBaseTransport | None = None
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_PEER_PREFIX = "peer~"


def backend_url() -> str:
    return os.environ.get("GLAZED_BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")


def case_id_for(session_id: str) -> str:
    """The case a session belongs to: itself, or the asker's id inside a peer session."""
    sid = str(session_id)
    while sid.startswith(_PEER_PREFIX):
        sid = sid[len(_PEER_PREFIX) :].rsplit("~", 1)[0]
    return sid


def _turn_context(ctx: Any) -> TurnContext | None:
    deps = getattr(ctx, "deps", None)
    return deps if isinstance(deps, TurnContext) else None


async def call(
    ctx: Any,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    warning_header: bool = False,
) -> Any:
    """One backend call. Always returns data or `{"error": ..., "status"?: ...}`; never raises.

    With `warning_header`, a successful body is returned as `{"experiences"...}`-style by the
    caller; here it yields `(data, warning)` so the tool can surface `X-Glazed-Warning`.
    """
    turn = _turn_context(ctx)
    if turn is None:
        return {"error": "No turn context is available, so the backend call was not made."}
    headers = {
        "X-Glazed-Case": case_id_for(turn.session.session_id),
        "X-Glazed-Agent": turn.agent_id,
        "X-Glazed-Store": str(turn.caller.tenant_id),
    }
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        async with httpx.AsyncClient(
            base_url=backend_url(), timeout=_TIMEOUT, transport=TRANSPORT
        ) as http:
            response = await http.request(
                method, path, params=clean or None, json=body, headers=headers
            )
    except httpx.TimeoutException:
        return {"error": "The Glazed backend timed out. Say the data could not be fetched."}
    except httpx.HTTPError as exc:
        return {"error": f"The Glazed backend is unreachable ({type(exc).__name__})."}
    except Exception as exc:  # noqa: BLE001 - a raising tool strands the whole turn as running
        return {"error": f"The Glazed backend call failed ({type(exc).__name__})."}
    if response.status_code >= 400:
        return {
            "error": f"The Glazed backend refused the request: HTTP {response.status_code} "
            f"{response.text[:300]}",
            "status": response.status_code,
        }
    try:
        data = response.json()
    except ValueError:
        return {"error": "The Glazed backend returned a non-JSON response."}
    if warning_header:
        return data, response.headers.get("X-Glazed-Warning")
    return data
