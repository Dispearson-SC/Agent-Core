"""WhatsApp Cloud API channel adapter - inbound webhook mapping and outbound send.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-08
Status:  DONE for text messages - inbound webhook -> TurnRequest, outbound send via the
         Graph API `messages` endpoint. Registered against `Channel` (t-f3-13)
         structurally: this class imports nothing from that module but its own
         `OutboundMessage` shape, and never subclasses anything.
Tests:   Core/tests/unit/test_channel_whatsapp.py

WHAT IS AND ISN'T PROVEN HERE
    No WhatsApp Business Account credential exists in this environment and no public
    HTTPS webhook is reachable from it, so everything below is proven against Meta's own
    documented payload and request shapes (Cloud API `messages` webhook and the
    `POST /{phone-number-id}/messages` reference), never against a live call. Still
    unproven without a real WABA token: that Meta's Graph API accepts this exact body,
    that a configured Bearer token is valid, and that a real `X-Hub-Signature-256`
    header verifies - signature verification is not implemented at all (see below).

WHY INBOUND PARSING RETURNS EXACTLY ONE `TurnRequest`, NEVER ZERO OR MANY
    The task's own acceptance criterion is "exactly one". Two payload shapes fail that
    honestly rather than guess:

    - A delivery-status callback (`value.statuses`, no `value.messages`) is not a user
      message. Meta fires these constantly - sent, delivered, read - and silently
      starting a turn for one would burn a model call on every read receipt.
    - Two or more inbound messages inside one webhook call is theoretically possible per
      Meta's schema (the `messages` array), but nothing here defines which one becomes
      THE turn. Guessing "the first one" silently drops the rest; refusing is the same
      choice `TurnOutcome.__post_init__` makes for its own two-shapes-only rule -
      fail at the boundary, not three layers down.

WHAT DELIBERATELY STAYS OUT OF THIS ADAPTER
    - Webhook signature verification (`X-Hub-Signature-256` against the app secret).
      That is an HTTP-layer concern for whatever route calls `parse_webhook` - this
      module has no HTTP framework dependency (ruff bans `fastapi` outside
      `adapters/driving/http/`) and takes an already-decoded JSON mapping.
    - Media messages, both directions. `UserInput.media` and `OutboundMessage.media`
      exist, but turning a WhatsApp media id into a `MediaRef` needs `MediaStore`
      (F7), which this leaf adapter has no reason to depend on. `parse_webhook` raises
      on a non-text message rather than silently emitting an empty-text turn;
      `WhatsAppChannel.send` raises rather than silently dropping an evidence photo a
      human is meant to see.
    - Tenant and profile resolution from the payload. One WhatsApp Business number is
      one deployment's concern on day 1 - the same shape `_authenticate` in
      `adapters/driving/http/routes.py` uses for its own day-1 trust boundary - so both
      are passed in by whatever wires this adapter (`t-f3-06`), not derived from
      `metadata.phone_number_id`. Routing one WABA to several tenants is a real future
      requirement and not one this task was asked to solve.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from agent_core.adapters.driving.channels.registry import OutboundMessage
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnRequest,
    UserInput,
)

__all__ = ["WhatsAppChannel", "WhatsAppWebhookError", "parse_webhook"]

_CHANNEL_ID = "whatsapp"
_DEFAULT_API_VERSION = "v21.0"
_GRAPH_API_HOST = "https://graph.facebook.com"


class WhatsAppWebhookError(Exception):
    """The payload does not map to exactly one inbound text message.

    Raised rather than returning `None`: a webhook route that swallows this and answers
    Meta with a plain 200 loses nothing (Meta does not retry a message it never hears
    back about as failed), but a route that let this become an empty `TurnRequest`
    instead would start a turn over what a human never actually said.
    """


def _iter_inbound_messages(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every `value.messages[]` entry across every entry/change, in payload order.

    Meta's envelope nests one webhook call inside `entry[].changes[]`, and a
    `messages`-field change is the only kind carrying a `messages` array at all - a
    `statuses`-field change (delivery receipts) simply has none.
    """
    messages: list[Mapping[str, Any]] = []
    for entry in payload.get("entry", ()):
        for change in entry.get("changes", ()):
            value = change.get("value", {})
            messages.extend(value.get("messages", ()))
    return messages


def parse_webhook(
    payload: Mapping[str, Any], *, tenant_id: TenantId, profile_id: str
) -> TurnRequest:
    """One decoded WhatsApp Cloud API webhook body -> exactly one `TurnRequest`.

    `tenant_id` and `profile_id` are not in the payload - see the module docstring for
    why they are parameters here rather than derived from `metadata.phone_number_id`.
    """
    messages = _iter_inbound_messages(payload)
    if len(messages) != 1:
        raise WhatsAppWebhookError(
            f"expected exactly one inbound message, found {len(messages)}"
        )

    message = messages[0]
    if message.get("type") != "text":
        raise WhatsAppWebhookError(
            f"only text messages map to a TurnRequest; got type {message.get('type')!r}"
        )

    body = message.get("text", {}).get("body")
    if not isinstance(body, str) or not body:
        raise WhatsAppWebhookError("text message carried no body")

    wa_id = message["from"]

    return TurnRequest(
        session=SessionRef(
            session_id=SessionId(f"{_CHANNEL_ID}:{wa_id}"), tenant_id=tenant_id
        ),
        caller=CallerIdentity(subject_id=wa_id, channel=_CHANNEL_ID, tenant_id=tenant_id),
        profile_id=profile_id,
        input=UserInput(text=body),
    )


@dataclass(frozen=True, slots=True)
class WhatsAppChannel:
    """Outbound half: registers against `Channel` (t-f3-13) by HAVING an async `send`.

    `client` is an injected `httpx.AsyncClient` rather than one built internally, for the
    same reason `create_app` in `adapters/driving/http/routes.py` takes a `TurnStarter`:
    a real client is what production wires in, and a `MockTransport`-backed one is what
    proves the exact request shape in `test_channel_whatsapp.py` with zero network I/O.
    """

    access_token: str
    phone_number_id: str
    client: httpx.AsyncClient
    api_version: str = _DEFAULT_API_VERSION

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        """POST the documented `messages` body. Media is out of scope - see the module
        docstring - and raising here beats silently sending only the text half of an
        evidence response a human is waiting on.
        """
        if message.media:
            raise NotImplementedError(
                "WhatsAppChannel.send does not carry media yet; needs MediaStore (F7)"
            )

        response = await self.client.post(
            f"{_GRAPH_API_HOST}/{self.api_version}/{self.phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": caller.subject_id,
                "type": "text",
                "text": {"body": message.text},
            },
        )
        response.raise_for_status()
