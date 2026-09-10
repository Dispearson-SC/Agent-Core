"""D2's done-when criterion, minus the budget cutoff: swap `LiteLLMGateway`'s `base_url`
and nothing else, and two tenants' spend stays separate.

Phase:   D2
Tasks:   docs/TASKS.md#t-d2-01
Covers:  adapters/driven/llm_litellm/gateway.py

WHAT THIS PROVES, AND HOW
    docs/ROADMAP.md's D2 "done when" is: two tenants run in parallel and the only code
    change was the adapter's `base_url`. This file proves the two halves of that:

      a. `LiteLLMGateway(proxy_base_url=...)` is the WHOLE day-1/day-2 switch - no second
         constructor argument, and `model_id_for` reshapes the SAME profile model string
         it took in library mode, never a second knob a caller has to also flip.
      b. two tenants' spend is tracked SEPARATELY by the live proxy - verified the same
         way this was already checked by hand: `GET /team/info?team_id=...` before and
         after one real call, on both teams.

    `model_id_for`'s day-2 branch is new in this revision. Verified against the live
    proxy (`GET /team/info`): a team's `models` list is the bare alias - `["MiniMax-M3",
    "MiniMax-Speech"]` - never the provider-qualified `minimax/MiniMax-M3` a profile's
    `model:` field carries. So day 2 strips the provider segment here; a profile does not
    change (docs/ROADMAP.md's D2 table: "Agent profiles" is in the "does not change"
    column).

WHY THIS TEST TALKS TO THE PROXY DIRECTLY, NOT THROUGH `models.model_for`
    `models.py::proxy_model` builds `LiteLLMProvider(api_base=base_url)` with no
    `api_key`. Verified against the installed `pydantic_ai`: `LiteLLMProvider.__init__`
    then hard-codes the literal string `"litellm-placeholder"` as the bearer token - it
    does NOT fall back to an environment variable, despite what its own docstring claims.
    Verified against the live proxy: that placeholder is rejected with 401 ("LiteLLM
    Virtual Key expected... expected to start with 'sk-'"). So today there is no path
    from a tenant's virtual key to the client `models.model_for` builds - `ModelGateway`
    exposes no credential, and `proxy_model`'s signature (frozen at t-f0-04, not owned
    here) exposes nowhere to put one. That gap is reported in this task's `found`, not
    fixed here (`gateway.py` and this file are the only files this task may write).

    So the real call below is made with a plain HTTP request built from EXACTLY
    `LiteLLMGateway`'s two outputs - `base_url()` and `model_id_for()` - plus one tenant's
    virtual key, which is what a working client would need too. That is enough to prove
    (a) and (b) without touching the frozen adapter that cannot yet carry the key.

SKIP GUARD
    Skips cleanly - with a reason `-rs` prints - when the proxy or both tenant
    credentials are unavailable, mirroring tests/integration/test_f0_end_to_end.py. CI
    without the proxy must not fail. The credentials are read the same way: the process
    environment first, then the repository's gitignored `.env`. Neither key is ever
    printed, logged, or placed in an assertion message.

COST
    One paid call (5 output tokens) plus a bounded poll of two read-only management
    endpoints. Spend lands on the proxy asynchronously - observed here to take between
    roughly 25 and 65 seconds - so the poll allows up to `_SPEND_POLL_TIMEOUT_SECONDS`.

    THE CALL MUST BE UNIQUE. Verified against the live proxy: an identical, repeated
    prompt comes back in well under a second and spends nothing at all - a proxy-level
    response cache, not a live model call. Two prior identical calls in this file's own
    development left ZERO trace in spend for over three minutes each; a call carrying a
    fresh nonce updated spend within a minute. So the one paid call embeds a random nonce
    the model is asked to echo back, which also makes the response itself unmemorisable
    and therefore an actual proof that this specific call reached the model - not merely
    that some call, once, did.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway

pytestmark = [pytest.mark.phase("D2")]

_PROFILE_MODEL = "minimax/MiniMax-M3"
_SPEND_POLL_TIMEOUT_SECONDS = 150.0
_SPEND_POLL_INTERVAL_SECONDS = 10.0


def _repo_root() -> Path:
    """`Core/tests/integration/this_file.py` -> the repository root, three parents up."""
    return Path(__file__).resolve().parents[3]


def _env_or_dotenv(name: str) -> str | None:
    """`name` from the process environment, falling back to the repo's gitignored `.env`.

    Mirrors `test_f0_end_to_end.py::_minimax_api_key`. The value is a secret in two of
    the three callers below: it goes into an HTTP header and into no assertion, message,
    or file.
    """
    from_environment = os.environ.get(name)
    if from_environment:
        return from_environment

    env_file = _repo_root() / ".env"
    if not env_file.exists():
        return None

    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def _proxy_url() -> str | None:
    url = _env_or_dotenv("LITELLM_PROXY_URL")
    return url.rstrip("/") if url else None


def _get(base_url: str, path: str, key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result: dict[str, Any] = json.loads(response.read().decode())
        return result


def _post_chat_completion(base_url: str, key: str, model: str) -> None:
    """The one paid call. A fresh nonce defeats the proxy's response cache - see COST in
    the module docstring - so this call, specifically, is what moves spend."""
    nonce = uuid.uuid4().hex
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "user", "content": f"nonce {nonce}: reply with the single word pong"}
            ],
            "max_tokens": 5,
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read()


def _team_id_for(base_url: str, key: str) -> str:
    """The team a virtual key belongs to. `/key/info` with no `key` query param defaults
    to the key in the Authorization header - a key looking up its own team."""
    info = _get(base_url, "/key/info", key)["info"]
    team_id = info.get("team_id")
    assert team_id, "the tenant key carries no team_id; per-team spend cannot be checked"
    return str(team_id)


def _team_spend(base_url: str, key: str, team_id: str) -> float:
    info = _get(base_url, f"/team/info?team_id={team_id}", key)
    spend: float = info["team_info"]["spend"]
    return spend


def _proxy_reachable(base_url: str | None, key: str | None) -> bool:
    if base_url is None or key is None:
        return False
    try:
        _get(base_url, "/key/info", key)
    except (urllib.error.URLError, OSError):
        return False
    return True


_PROXY_URL = _proxy_url()
_TENANT_A_KEY = _env_or_dotenv("LITELLM_KEY_TENANT_A")
_TENANT_B_KEY = _env_or_dotenv("LITELLM_KEY_TENANT_B")

_needs_proxy = pytest.mark.skipif(
    _TENANT_B_KEY is None or not _proxy_reachable(_PROXY_URL, _TENANT_A_KEY),
    reason=(
        "no reachable LiteLLM proxy with both tenants: set LITELLM_PROXY_URL, "
        "LITELLM_KEY_TENANT_A and LITELLM_KEY_TENANT_B, or add them to the repo .env"
    ),
)


def test_library_mode_gateway_needs_no_proxy_configuration() -> None:
    """Day 1 stays what t-f0-04 built. No skip guard: needs no network and no credential,
    so it must keep passing unconditionally - it is the "before" half of (a)."""
    gateway = LiteLLMGateway()
    assert gateway.base_url() is None
    assert gateway.model_id_for(_PROFILE_MODEL) == _PROFILE_MODEL


@_needs_proxy
def test_setting_base_url_is_the_only_change_and_tenant_spend_stays_separate() -> None:
    base_url = _PROXY_URL
    key_a = _TENANT_A_KEY
    key_b = _TENANT_B_KEY
    assert base_url is not None and key_a is not None and key_b is not None, (
        "the skip guard should have prevented this"
    )

    library_gateway = LiteLLMGateway()
    proxy_gateway = LiteLLMGateway(proxy_base_url=base_url)

    # (a) THE ONLY CHANGE IS base_url. `proxy_base_url` is the sole constructor argument
    # `LiteLLMGateway` takes to move a caller from day 1 to day 2.
    assert library_gateway.base_url() is None
    assert proxy_gateway.base_url() == base_url

    # `model_id_for` reshapes the SAME profile string library mode was handed - never a
    # second field a caller must also change. Verified fact, in the module docstring
    # above: the live proxy's team is granted the bare alias, not the qualified id.
    wire_model = proxy_gateway.model_id_for(_PROFILE_MODEL)
    assert wire_model == _PROFILE_MODEL.split("/", 1)[1], (
        "day 2 must strip the provider segment to match what the live proxy's team was "
        "actually granted - see GET /team/info in the module docstring"
    )

    team_a = _team_id_for(base_url, key_a)
    team_b = _team_id_for(base_url, key_b)
    assert team_a != team_b, "both tenant keys resolved to the same team; fixture is wrong"
    baseline_a = _team_spend(base_url, key_a, team_a)
    baseline_b = _team_spend(base_url, key_b, team_b)

    # THE ONE PAID CALL. Routed with exactly proxy_gateway's two outputs plus tenant A's
    # virtual key - see WHY THIS TEST TALKS TO THE PROXY DIRECTLY, above.
    _post_chat_completion(base_url, key_a, wire_model)

    # (b) SPEND IS TRACKED PER TEAM. It lands asynchronously, so poll team A with a bound;
    # team B is read once, after the poll, and must not have moved at all.
    deadline = time.monotonic() + _SPEND_POLL_TIMEOUT_SECONDS
    spend_a = baseline_a
    while spend_a <= baseline_a and time.monotonic() < deadline:
        time.sleep(_SPEND_POLL_INTERVAL_SECONDS)
        spend_a = _team_spend(base_url, key_a, team_a)
    spend_b = _team_spend(base_url, key_b, team_b)

    assert spend_a > baseline_a, (
        "tenant A's team spend never moved after a real call routed through the proxy "
        "with proxy_gateway's own base_url and model id"
    )
    assert spend_b == baseline_b, (
        "tenant B's team spend moved from a call tenant A made: spend is not separated "
        "per tenant"
    )
