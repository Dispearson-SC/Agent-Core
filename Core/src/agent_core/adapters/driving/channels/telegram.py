"""Telegram Bot API channel adapter — inbound update mapping and outbound send.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-09
Status:  DONE — registers against `t-f3-13`'s `Channel` Protocol. Verified live against
         the real Bot API (`TELEGRAM_BOT`): `getMe` authenticates through this adapter's
         own `poster` seam, and a real `getUpdates` response maps through `to_turn_request`
         with no divergence from the fixture `test_channel_telegram.py` assumed — the
         documented `Update`/`Message` shape holds. `sendMessage` itself is still exercised
         only against a stub (no chat id available to send into here), and the inbound
         webhook path is NOT exercised — this environment has no public HTTPS endpoint, so
         `getUpdates` (long polling) is the inbound path actually verified live; see
         `test_telegram_live.py`. Wiring into `composition.py`'s registry is `t-f3-06`'s
         job, not this file's.
Tests:   Core/tests/unit/test_channel_telegram.py, Core/tests/integration/test_telegram_live.py

WHAT THIS FILE IS
    A leaf adapter, exactly as `adapters/driving/channels/registry.py` describes: it
    depends on the Telegram Bot API's wire shape and on `domain/turn.py`, and on nothing
    else in the repository. It registers against `Channel` structurally (an async
    `send`), never by importing anything from `registry.py` beyond the two plain data
    shapes (`OutboundMessage`) it needs to speak the same outbound vocabulary.

INBOUND: ONE UPDATE, ONE `TurnRequest`
    Telegram's webhook and `getUpdates` both deliver one `Update` JSON object per
    message (https://core.telegram.org/bots/api#update). `to_turn_request` is the only
    place that JSON shape is read; everything past it is the domain vocabulary every
    other channel already speaks.

    `tenant_id` and `profile_id` are NOT in a Telegram update — there is no tenant
    concept on the wire. One `TelegramChannel` instance serves one bot token, and one bot
    token is wired (by `t-f3-06`, in `composition.py`) to exactly one tenant and one
    profile. That is a composition-time decision, not something this file infers from
    the payload.

WHY `subject_id` IS THE CHAT ID, NOT THE SENDING USER'S ID
    `Channel.send` is handed only a `CallerIdentity` — never the raw update again
    (`registry.py`'s whole point: outbound delivery routes on
    `CallerIdentity.channel`, and here it must also carry *where* to reply). In a group
    chat `message.chat.id` and `message.from.id` differ, and replying to the user id
    would not reach the conversation the message came from — it would DM the wrong
    thread, or fail outright since a bot cannot open a DM the user never started. So
    `subject_id` is always the chat id: the platform's own reply address, exactly as the
    WhatsApp adapter's `subject_id` is expected to be a phone number rather than an
    internal contact id.

OUTBOUND: THE DOCUMENTED `sendMessage` BODY, NOTHING MORE
    https://core.telegram.org/bots/api#sendmessage needs `chat_id` and `text` at
    minimum. `_build_send_body` is that mapping and nothing else — no `parse_mode`, no
    keyboard, because nothing here has decided that shape yet and inventing one would be
    unverified behaviour shipped as if it were documented.

WHY THE HTTP CALL IS INJECTED, NOT IMPORTED
    Same reasoning as `routes.py`'s `TurnStarter`: this module never imports an HTTP
    client itself, so which client dials out — a real `httpx.AsyncClient` or a recording
    stub — is entirely the caller's choice. `poster` is a structural seam matching
    `httpx.AsyncClient.post`'s signature, so the real client is handed in directly once a
    token exists (`test_telegram_live.py`, now that `TELEGRAM_BOT` does) — Telegram's
    `getUpdates` needs no public URL to verify that live, unlike WhatsApp Cloud API's
    inbound webhook. The unit suite hands in a stub that records the body instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent_core.adapters.driving.channels.registry import OutboundMessage
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnRequest,
    UserInput,
)

TELEGRAM_CHANNEL_ID = "telegram"
_API_BASE = "https://api.telegram.org"


class TelegramUpdateError(Exception):
    """An `Update` this adapter cannot turn into a `TurnRequest`.

    Raised rather than guessed: an update with no message text has no `UserInput.text`
    to build, and silently starting an empty-text turn would run a turn nobody sent.
    """


class _MessagePoster(Protocol):
    """What `TelegramChannel` needs to put a payload on the wire.

    Matches `httpx.AsyncClient.post`'s signature closely enough that the real client can
    be handed in directly once a bot token exists; a test hands in a stub instead so
    this module never dials out.
    """

    async def post(self, url: str, *, json: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class TelegramChannel:
    """One bot token, wired to one tenant and one profile.

    Registers against `Channel` (`adapters/driving/channels/registry.py`) structurally:
    it is never imported or subclassed from there, only handed to
    `ChannelRegistry` by `t-f3-06`'s wiring because it happens to have an async `send`.
    """

    bot_token: str
    tenant_id: TenantId
    profile_id: str
    poster: _MessagePoster

    def to_turn_request(self, update: dict[str, Any]) -> TurnRequest:
        """One Telegram `Update` -> one `TurnRequest`, or `TelegramUpdateError`.

        Only a plain text message in `update.message` is understood today. Anything
        else — `edited_message`, `channel_post`, a sticker or photo with no `text` key —
        is refused rather than turned into a turn with an empty or guessed message.
        """
        message = update.get("message")
        if not isinstance(message, dict) or "text" not in message:
            raise TelegramUpdateError(
                "Telegram update carries no message text this adapter can turn into a "
                f"TurnRequest: {update!r}"
            )

        chat_id = str(message["chat"]["id"])
        return TurnRequest(
            session=SessionRef(session_id=SessionId(chat_id), tenant_id=self.tenant_id),
            caller=CallerIdentity(
                subject_id=chat_id, channel=TELEGRAM_CHANNEL_ID, tenant_id=self.tenant_id
            ),
            profile_id=self.profile_id,
            input=UserInput(text=message["text"]),
        )

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        """Deliver `message` on Telegram. Satisfies `Channel` structurally (D13/D23)."""
        await self.poster.post(
            f"{_API_BASE}/bot{self.bot_token}/sendMessage",
            json=self._build_send_body(caller.subject_id, message.text),
        )

    @staticmethod
    def _build_send_body(chat_id: str, text: str) -> dict[str, Any]:
        """https://core.telegram.org/bots/api#sendmessage — the documented minimum."""
        return {"chat_id": chat_id, "text": text}
