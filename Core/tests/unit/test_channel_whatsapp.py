"""WhatsApp Cloud API channel adapter - inbound mapping and outbound send.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-08
Status:  RED FIRST - written before adapters/driving/channels/whatsapp.py had behaviour
Tests:   this file

WHY THIS TEST EXISTS
    Two things must hold, mirroring the task's own acceptance language:

    1. AN INBOUND WEBHOOK PAYLOAD MAPS TO EXACTLY ONE `TurnRequest`. The fixture below is
       Meta's own documented example payload shape for `messages`. No live WABA credential
       and no reachable HTTPS webhook exist in this environment, so this is the only kind
       of proof available here - payload-in, `TurnRequest`-out, against a payload shaped
       exactly like the one Meta's docs publish.

    2. THE OUTBOUND SEND BUILDS THE DOCUMENTED REQUEST BODY. `httpx.MockTransport` proves
       the exact method, URL, headers and JSON body the adapter would put on the wire,
       with zero network I/O - `httpx` is already a hard dependency here (LiteLLM pins
       it), so this is not a new dependency, and no real call is ever made.

WHAT STAYS UNPROVEN WITHOUT A REAL WABA TOKEN
    Whether Meta's Graph API actually accepts this exact body, whether the configured
    Bearer token is valid, and whether the webhook signature Meta sends
    (`X-Hub-Signature-256`) verifies against a real app secret. None of that can be proven
    from this repository today.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from agent_core.adapters.driving.channels.registry import Channel, OutboundMessage
from agent_core.adapters.driving.channels.whatsapp import (
    WhatsAppChannel,
    WhatsAppWebhookError,
    parse_webhook,
)
from agent_core.domain.turn import CallerIdentity, TenantId, TurnRequest

# Meta's own documented example shape for a `messages` webhook change, field-for-field.
# https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
_INBOUND_TEXT_PAYLOAD: dict[str, Any] = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "102290129340398",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "16505551111",
                            "phone_number_id": "106540352242922",
                        },
                        "contacts": [
                            {"profile": {"name": "Kerry Fisher"}, "wa_id": "16315551181"}
                        ],
                        "messages": [
                            {
                                "from": "16315551181",
                                "id": "wamid.HBgLMTYzMTU1NTExODEVAgARGBI1RjQyNjMxRTQ2RjBFNzUxRTQA",
                                "timestamp": "1603059201",
                                "text": {"body": "Hello this is an answer"},
                                "type": "text",
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}

# A delivery-status callback: same envelope shape, no inbound message to answer. Meta
# fires these constantly (sent/delivered/read) and none of them starts a turn.
_STATUS_ONLY_PAYLOAD: dict[str, Any] = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "102290129340398",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "16505551111",
                            "phone_number_id": "106540352242922",
                        },
                        "statuses": [
                            {
                                "id": "wamid.HBgLMTYzMTU1NTExODEVAgARGBI1RjQyNjMxRTQ2RjBFNzUxRTQA",
                                "status": "delivered",
                                "timestamp": "1603059202",
                                "recipient_id": "16315551181",
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}

_TENANT_ID = TenantId("t1")
_PROFILE_ID = "fraud-triage"


@pytest.mark.phase("F3")
def test_inbound_webhook_maps_to_exactly_one_turn_request() -> None:
    """The whole acceptance criterion for the inbound half."""
    request = parse_webhook(_INBOUND_TEXT_PAYLOAD, tenant_id=_TENANT_ID, profile_id=_PROFILE_ID)

    assert isinstance(request, TurnRequest)
    assert request.input.text == "Hello this is an answer"
    assert request.profile_id == _PROFILE_ID
    assert request.caller.subject_id == "16315551181"
    assert request.caller.channel == "whatsapp"
    assert request.caller.tenant_id == _TENANT_ID
    assert request.session.tenant_id == _TENANT_ID


@pytest.mark.phase("F3")
def test_a_status_only_payload_is_not_a_turn_request() -> None:
    """Delivery receipts must not silently start a turn nobody sent a message for."""
    with pytest.raises(WhatsAppWebhookError):
        parse_webhook(_STATUS_ONLY_PAYLOAD, tenant_id=_TENANT_ID, profile_id=_PROFILE_ID)


@pytest.mark.phase("F3")
def test_a_multi_message_payload_is_rejected_rather_than_guessed() -> None:
    """More than one inbound message is not 'exactly one TurnRequest' - refuse, don't pick."""
    doubled = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [
                    _INBOUND_TEXT_PAYLOAD["entry"][0]["changes"][0],
                    _INBOUND_TEXT_PAYLOAD["entry"][0]["changes"][0],
                ],
            }
        ],
    }

    with pytest.raises(WhatsAppWebhookError):
        parse_webhook(doubled, tenant_id=_TENANT_ID, profile_id=_PROFILE_ID)


@pytest.mark.phase("F3")
def test_outbound_send_builds_the_documented_request_body() -> None:
    """https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages -
    exact method, URL, headers and JSON body, with zero network I/O."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = WhatsAppChannel(
        access_token="EAA-test-token",
        phone_number_id="106540352242922",
        client=client,
    )
    caller = CallerIdentity(subject_id="16315551181", channel="whatsapp", tenant_id=_TENANT_ID)

    asyncio.run(channel.send(caller, OutboundMessage(text="Your case was approved.")))
    asyncio.run(client.aclose())

    sent = captured["request"]
    assert sent.method == "POST"
    assert str(sent.url) == "https://graph.facebook.com/v21.0/106540352242922/messages"
    assert sent.headers["Authorization"] == "Bearer EAA-test-token"

    import json

    body = json.loads(sent.content)
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "16315551181",
        "type": "text",
        "text": {"body": "Your case was approved."},
    }


@pytest.mark.phase("F3")
def test_whatsapp_channel_registers_structurally_against_channel() -> None:
    """`t-f3-08` registers by HAVING an async send, never by subclassing (D23/t-f3-13)."""
    channel = WhatsAppChannel(
        access_token="tok",
        phone_number_id="123",
        client=httpx.AsyncClient(),
    )
    assert isinstance(channel, Channel)
