"""Telegram Bot API channel adapter — inbound mapping and outbound send.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-09
Status:  RED FIRST - written before adapters/driving/channels/telegram.py had behaviour

WHY THIS TEST EXISTS
    Two things this adapter does, and both are pinned here rather than proved by hand:

    1. ONE INBOUND UPDATE MAPS TO EXACTLY ONE `TurnRequest`. Telegram's webhook (and
       `getUpdates`) deliver one `Update` object per message; this adapter's job is to
       turn that JSON into the one domain type `StartTurn` accepts, with no channel
       vocabulary leaking past this file.

    2. OUTBOUND SEND BUILDS THE DOCUMENTED `sendMessage` BODY. No bot token exists yet
       (see docs/TASKS.md#t-f3-09), so this test never dials out - the network call is
       injected as a `poster`, and the assertion is on the body and URL the adapter
       would have sent, matching https://core.telegram.org/bots/api#sendmessage exactly:
       `{"chat_id": ..., "text": ...}`. Wiring a real token in later only swaps what is
       injected; nothing here changes to prove it live, which is cheap on Telegram
       (`getUpdates` needs no public URL) unlike WhatsApp Cloud API's inbound webhook.

FIXTURE, NOT A LIVE CALL
    The Telegram update below is a private-chat text message, shaped exactly as Telegram
    documents it: https://core.telegram.org/bots/api#update /
    https://core.telegram.org/bots/api#message. No token, no HTTP, no skipif needed -
    this adapter touches no infrastructure.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from agent_core.adapters.driving.channels.registry import Channel, OutboundMessage
from agent_core.adapters.driving.channels.telegram import (
    TELEGRAM_CHANNEL_ID,
    TelegramChannel,
    TelegramUpdateError,
)
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId, TurnRequest, UserInput

_TENANT = TenantId("acme")
_PROFILE_ID = "support-bot"


def _telegram_text_update(*, chat_id: int = 987654321, user_id: int = 111222333) -> dict[str, Any]:
    """A private-chat text message, shaped exactly as Telegram's webhook/`getUpdates` send it."""
    return {
        "update_id": 10000,
        "message": {
            "message_id": 1365,
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "John",
                "username": "john_doe",
            },
            "chat": {
                "id": chat_id,
                "first_name": "John",
                "username": "john_doe",
                "type": "private",
            },
            "date": 1699999999,
            "text": "Hello there",
        },
    }


class _RecordingPoster:
    """Stands in for `httpx.AsyncClient`: records instead of dialing out."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> None:
        self.calls.append((url, json))


def _channel(poster: _RecordingPoster | None = None) -> TelegramChannel:
    return TelegramChannel(
        bot_token="123456:FAKE-TEST-TOKEN",
        tenant_id=_TENANT,
        profile_id=_PROFILE_ID,
        poster=poster or _RecordingPoster(),
    )


@pytest.mark.phase("F3")
def test_an_inbound_text_update_maps_to_exactly_one_turn_request() -> None:
    """The whole point: one Telegram `Update` in, one `TurnRequest` out."""
    channel = _channel()
    update = _telegram_text_update(chat_id=987654321, user_id=111222333)

    request = channel.to_turn_request(update)

    assert isinstance(request, TurnRequest)
    assert request == TurnRequest(
        session=SessionRef(session_id="987654321", tenant_id=_TENANT),  # type: ignore[arg-type]
        caller=CallerIdentity(
            subject_id="987654321", channel=TELEGRAM_CHANNEL_ID, tenant_id=_TENANT
        ),
        profile_id=_PROFILE_ID,
        input=UserInput(text="Hello there"),
    )


@pytest.mark.phase("F3")
def test_the_reply_address_is_the_chat_not_the_sending_user() -> None:
    """A group chat's `chat.id` differs from `from.id`; replying to the user id would
    silently DM the wrong conversation (or fail outright), since `Channel.send` is only
    ever handed the `CallerIdentity` this file builds - never the raw update again."""
    channel = _channel()
    update = _telegram_text_update(chat_id=-100999, user_id=555)

    request = channel.to_turn_request(update)

    assert request.session.session_id == "-100999"
    assert request.caller.subject_id == "-100999"


@pytest.mark.phase("F3")
def test_an_update_with_no_text_message_is_refused_rather_than_guessed() -> None:
    """A non-text update (e.g. `edited_message`, a sticker) has no `UserInput.text` to
    build. Silently building an empty-text turn would start a turn nobody sent."""
    channel = _channel()

    with pytest.raises(TelegramUpdateError):
        channel.to_turn_request({"update_id": 1, "edited_message": {"chat": {"id": 1}}})


@pytest.mark.phase("F3")
def test_outbound_send_builds_the_documented_sendmessage_body() -> None:
    """https://core.telegram.org/bots/api#sendmessage - `chat_id` and `text`, nothing
    invented and nothing dropped."""
    poster = _RecordingPoster()
    channel = _channel(poster)
    caller = CallerIdentity(subject_id="987654321", channel=TELEGRAM_CHANNEL_ID, tenant_id=_TENANT)

    asyncio.run(channel.send(caller, OutboundMessage(text="reply text")))

    assert len(poster.calls) == 1
    url, body = poster.calls[0]
    assert url == "https://api.telegram.org/bot123456:FAKE-TEST-TOKEN/sendMessage"
    assert body == {"chat_id": "987654321", "text": "reply text"}


@pytest.mark.phase("F3")
def test_send_is_a_coroutine_and_registers_as_a_channel() -> None:
    """D13/D23: `Channel` is structural and every member is awaitable (t-f3-13)."""
    channel = _channel()
    assert inspect.iscoroutinefunction(channel.send)
    assert isinstance(channel, Channel)
