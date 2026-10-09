"""The channel registry is the shape D23 chose INSTEAD of a sixteenth port.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-13
Status:  RED FIRST - written before adapters/driving/channels/registry.py had behaviour

WHY THIS TEST EXISTS
    Two properties of this module are load-bearing for three later anchors, and neither
    of them fails anywhere else in the suite.

    1. `send` IS A COROUTINE. Delivering a finished turn means calling out to the
       WhatsApp Cloud API or the Telegram Bot API over the network (D22, D23). A sync
       `def send` type-checks, satisfies a structural Protocol, and passes a unit test
       against an in-process fake - and then blocks the event loop for the whole
       platform round-trip, stalling every other turn in the process. D13 makes every
       adapter member awaitable; the only place that can be pinned before `t-f3-08` and
       `t-f3-09` are written is here, on the shape they register against.

    2. A DUPLICATE CHANNEL ID RAISES AT CONSTRUCTION. A registry built from pairs can
       silently keep exactly one of two entries claiming the same id, and a dict literal
       does precisely that: last one wins, no error, no log line. The failure surfaces
       as answers delivered to the wrong platform - or to nobody - long after the wiring
       in `composition.py` (`t-f3-06`) was reviewed and approved. Construction is the
       only moment where the mistake is still cheap, so the error belongs there rather
       than at the first `get`.

    WHERE THIS LIVES IS ALSO PART OF THE CONTRACT. `Channel` is a Protocol under
    `adapters/`, not under `ports/`. D23 settled that outbound delivery adds no port,
    and `application/` never names a channel. A test asserting the module path is the
    cheapest guard against someone "tidying" it into `ports/` and reopening D23 by
    accident.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import get_type_hints

import pytest

from agent_core.adapters.driving.channels.registry import (
    Channel,
    ChannelRegistry,
    DuplicateChannelError,
    OutboundMessage,
    UnknownChannelError,
)
from agent_core.domain.turn import CallerIdentity, TenantId


class _RecordingChannel:
    """A channel that records instead of calling a platform.

    It is also the type-level conformance check: mypy proves this satisfies `Channel` at
    the annotated assignment in `test_a_plain_object_with_an_async_send_is_a_channel`,
    so the Protocol cannot drift away from the shape asserted here without the
    project-wide mypy run failing too.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


def _caller(channel: str) -> CallerIdentity:
    return CallerIdentity(subject_id="+34600000000", channel=channel, tenant_id=TenantId("t1"))


def _protocol_members() -> frozenset[str]:
    """The names `Channel` itself declares, without object/Protocol noise."""
    declared = getattr(Channel, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(name for name in vars(Channel) if not name.startswith("_"))


@pytest.mark.phase("F3")
def test_channel_declares_one_member_and_it_is_an_async_send() -> None:
    """One question, one member, and it is awaitable (D13).

    A second member here would mean the shape answers two questions and the cut is
    wrong; a sync `send` would mean the event loop pays for the platform round-trip.
    """
    members = sorted(_protocol_members())
    assert members == ["send"], (
        "Channel answers one question - how do I put this message on that platform - "
        f"through exactly send. Found: {members}."
    )

    assert inspect.iscoroutinefunction(Channel.send), (
        "Channel.send must be `async def`. A sync send blocks the event loop for the "
        "whole WhatsApp/Telegram round-trip and stalls every other turn (D13)."
    )
    assert get_type_hints(Channel.send).get("return") is type(None), (
        "send delivers; it does not hand a value back to the caller."
    )


@pytest.mark.phase("F3")
def test_a_plain_object_with_an_async_send_is_a_channel() -> None:
    """Structural, not nominal: `t-f3-08` and `t-f3-09` must not inherit anything."""
    recording = _RecordingChannel()
    conforming: Channel = recording
    assert isinstance(recording, Channel), (
        "Channel must be runtime_checkable and structural: a WhatsApp or Telegram "
        "adapter registers by having an async send, never by subclassing."
    )
    assert conforming is recording


@pytest.mark.phase("F3")
def test_duplicate_channel_id_raises_at_construction() -> None:
    """The whole point of the type: two entries claiming one id is a wiring bug.

    A dict literal keeps the last one silently. This asserts the opposite - the registry
    refuses to exist, and names the offending id so `composition.py` is greppable.
    """
    first = _RecordingChannel()
    second = _RecordingChannel()

    with pytest.raises(DuplicateChannelError) as raised:
        ChannelRegistry(
            (("whatsapp", first), ("telegram", _RecordingChannel()), ("whatsapp", second))
        )

    assert "whatsapp" in str(raised.value), (
        "The error must name the duplicated channel id; a bare 'duplicate' sends the "
        "reader back to composition.py to find which one."
    )


@pytest.mark.phase("F3")
def test_a_duplicate_is_never_silently_resolved_to_one_survivor() -> None:
    """The failure mode being prevented, stated as behaviour rather than as a type.

    Building the same pairs without the last duplicate must succeed, so the refusal
    above is caused by the duplicate id and not by the registry rejecting two channels.
    """
    whatsapp = _RecordingChannel()
    telegram = _RecordingChannel()

    registry = ChannelRegistry((("whatsapp", whatsapp), ("telegram", telegram)))

    assert sorted(registry.channel_ids()) == ["telegram", "whatsapp"]
    assert registry.get("whatsapp") is whatsapp
    assert registry.get("telegram") is telegram


@pytest.mark.phase("F3")
def test_registry_routes_by_the_channel_the_caller_arrived_on() -> None:
    """`CallerIdentity.channel` is the key; no new field is invented for routing (D23)."""
    whatsapp = _RecordingChannel()
    telegram = _RecordingChannel()
    registry = ChannelRegistry((("whatsapp", whatsapp), ("telegram", telegram)))
    caller = _caller("telegram")

    asyncio.run(registry.deliver(caller, OutboundMessage(text="done")))

    assert whatsapp.sent == []
    assert [message.text for _, message in telegram.sent] == ["done"]


@pytest.mark.phase("F3")
def test_an_unknown_channel_id_raises_rather_than_dropping_the_message() -> None:
    """Silently dropping an answer is the one outcome worse than a loud failure."""
    registry = ChannelRegistry((("whatsapp", _RecordingChannel()),))

    with pytest.raises(UnknownChannelError) as raised:
        registry.get("telegram")

    assert "telegram" in str(raised.value)


@pytest.mark.phase("F3")
def test_the_channel_shape_lives_in_the_adapter_layer_not_in_ports() -> None:
    """D23: outbound delivery adds no port. Moving this file would reopen that."""
    assert Channel.__module__ == "agent_core.adapters.driving.channels.registry", (
        "Channel is a structural Protocol under adapters/, not a port. application/ "
        "never names a channel; if it ever must, D23 is reopened deliberately."
    )
