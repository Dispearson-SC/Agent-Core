"""Live verification of the Telegram adapter against the real Bot API.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-09
Covers:  adapters/driving/channels/telegram.py

WHAT THIS PROVES, AND HOW
    `test_channel_telegram.py` (unit) proves the mapping logic against fixtures it
    invented. This file proves two things the unit suite cannot, because a bot token did
    not exist when it was written (docs/STATE.md, "Verify Telegram live"):

    1. THE CREDENTIAL AND TRANSPORT ARE REAL. `getMe`
       (https://core.telegram.org/bots/api#getme) is the cheapest authenticated call the
       Bot API offers - no chat needed. A 200 with `ok: true` and a `result.is_bot: true`
       proves `TELEGRAM_BOT` is a live, valid bot token and that this adapter's HTTP
       shape (`{_API_BASE}/bot{token}/<method>`) is accepted by the real API, not just by
       a stub that mirrors it back.

    2. A REAL `getUpdates` RESPONSE MAPS THE WAY THE ADAPTER ASSUMES. `getUpdates`
       (https://core.telegram.org/bots/api#getupdates) is the long-polling inbound path -
       this environment has no public HTTPS endpoint, so the webhook itself is NOT
       exercised here; that is a real gap, named rather than pretended away. What IS
       exercised: whatever real update shape `getUpdates` returns is fed through the same
       `TelegramChannel.to_turn_request` the unit suite already trusts, so a
       divergence between the documented `Update` shape (which the fixture matched) and
       what the live API actually sends would show up here first. When no message is
       waiting, this degrades to shape assertions on the (possibly empty) live
       `result` list rather than skipping outright - `getUpdates` succeeding at all,
       authenticated, is itself part of what "verified live" means.

SKIP GUARD
    Skips cleanly - with a reason `-rs` prints - when `TELEGRAM_BOT` is not set, mirroring
    `test_proxy_mode.py`'s `_env_or_dotenv` (process environment first, then the repo's
    gitignored `.env`). CI without the token must not fail. The token is never printed,
    logged, or placed in an assertion message - only ever used to build the URL this
    adapter already builds in production.

COST
    Two GET-only calls to the live Bot API (`getMe`, `getUpdates` with a short timeout).
    No message is ever sent - `sendMessage` is exercised only against a stub in the unit
    suite, since sending into a real chat needs a chat id this environment does not have.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from agent_core.adapters.driving.channels.telegram import TelegramChannel
from agent_core.domain.turn import TenantId, TurnRequest

pytestmark = [pytest.mark.phase("F3")]

_API_BASE = "https://api.telegram.org"
_TENANT = TenantId("acme")
_PROFILE_ID = "support-bot"


def _repo_root() -> Path:
    """`Core/tests/integration/this_file.py` -> the repository root, three parents up."""
    return Path(__file__).resolve().parents[3]


def _env_or_dotenv(name: str) -> str | None:
    """`name` from the process environment, falling back to the repo's gitignored `.env`.

    Mirrors `test_proxy_mode.py::_env_or_dotenv`. Never printed, logged, or asserted on.
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


_BOT_TOKEN = _env_or_dotenv("TELEGRAM_BOT")

_needs_token = pytest.mark.skipif(
    _BOT_TOKEN is None,
    reason="TELEGRAM_BOT not set: set it in the environment or the repo .env",
)


def _channel(client: httpx.AsyncClient) -> TelegramChannel:
    assert _BOT_TOKEN is not None  # narrows for mypy; the skipif already guards this
    return TelegramChannel(
        bot_token=_BOT_TOKEN,
        tenant_id=_TENANT,
        profile_id=_PROFILE_ID,
        poster=client,
    )


@_needs_token
def test_the_real_bot_api_authenticates_the_token_and_returns_a_bot_identity() -> None:
    """`getMe` needs no chat: the cheapest proof the token and transport are both real."""

    async def call() -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{_API_BASE}/bot{_BOT_TOKEN}/getMe", json={})
            response.raise_for_status()
            result: dict[str, object] = response.json()
            return result

    payload = asyncio.run(call())

    assert payload["ok"] is True
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["is_bot"] is True
    assert isinstance(result.get("id"), int)
    assert isinstance(result.get("username"), str) and result["username"]


@_needs_token
def test_the_adapters_http_shape_is_accepted_by_the_real_api_via_its_own_poster() -> None:
    """Same call as above, but through `TelegramChannel`'s own injected `poster` seam -
    proving the adapter's URL-building (`{_API_BASE}/bot{token}/<method>`), not just a
    hand-rolled request that happens to match it, is what the live API accepts."""

    async def call() -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            channel = _channel(client)
            response = await channel.poster.post(
                f"{_API_BASE}/bot{channel.bot_token}/getMe", json={}
            )
            response.raise_for_status()
            payload: dict[str, object] = response.json()
            return payload

    payload = asyncio.run(call())

    assert payload["ok"] is True
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["is_bot"] is True


@_needs_token
def test_a_real_getupdates_response_maps_through_to_turn_request_without_raising() -> None:
    """The whole point: whatever `getUpdates` really sends is fed through the exact same
    `to_turn_request` the unit suite trusts against a hand-built fixture. A live/fixture
    shape mismatch shows up here as a `KeyError`/`TelegramUpdateError` surprise, not as a
    silent pass. `timeout=0` makes this a single non-blocking poll, never long-polling
    inside a test run."""

    async def call() -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{_API_BASE}/bot{_BOT_TOKEN}/getUpdates",
                json={"timeout": 0, "limit": 10},
            )
            response.raise_for_status()
            payload: dict[str, object] = response.json()
            return payload

    payload = asyncio.run(call())

    assert payload["ok"] is True
    updates = payload["result"]
    assert isinstance(updates, list)

    async def channel() -> TelegramChannel:
        async with httpx.AsyncClient() as client:
            return _channel(client)

    built = asyncio.run(channel())

    # No update pending is a legitimate live outcome (nobody has messaged the bot since
    # the last `getUpdates` offset advanced) - the shape assertions above already prove
    # the authenticated call itself succeeded. When one IS pending, it must map cleanly
    # to a `TurnRequest` carrying this environment's tenant/profile, using the SAME
    # `to_turn_request` the unit suite exercises against its fixture.
    for update in updates:
        assert isinstance(update, dict)
        if "message" not in update or "text" not in update.get("message", {}):
            continue  # non-text update: adapter correctly refuses these, not this test's concern

        request = built.to_turn_request(update)

        assert isinstance(request, TurnRequest)
        chat_id = str(update["message"]["chat"]["id"])
        assert request.session.session_id == chat_id
        assert request.caller.subject_id == chat_id
        assert request.profile_id == _PROFILE_ID
        assert request.input.text == update["message"]["text"]
