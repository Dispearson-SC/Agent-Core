"""Composition-root wiring of the channel registry.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-06
Status:  RED FIRST - written before composition.py builds a ChannelRegistry
Tests:   this file

WHY THIS TEST EXISTS
    `t-f3-13`'s `ChannelRegistry` already refuses a duplicate id at construction and an
    unknown id at `get` (Core/tests/unit/test_channel_registry.py). Neither guard means
    anything if `composition.py` hands the rest of the system something that is NOT a
    `ChannelRegistry` - a plain `dict` built here with `.get(channel_id)` returning
    `None` on a miss would pass every existing test while silently dropping a delivery
    the moment `_step_deliver` (t-f3-07) or `ChannelHumanGateway` reached for a channel
    nothing was wired against.

    So this pins the composition-root SHAPE, not the registry's own behaviour again:
    the container must hand back the real `ChannelRegistry` type, and asking it for a
    channel id nothing is wired against must raise `UnknownChannelError` - loudly, at
    the moment the lookup happens - never resolve to `None` for a caller three layers up
    to mistake for "delivered".

NO INFRASTRUCTURE NEEDED
    `build_container` already does no I/O (composition.py's own "NOTHING HERE CONNECTS"
    guarantee), and this test never opens the pools it returns. Default `Settings`
    carries no channel credentials, so the registry under test is legitimately empty -
    which is exactly the case that must raise instead of quietly resolving to nothing.
    The one positive-wiring test below constructs `Settings` directly with a fake token,
    never through the environment, so it needs no real Telegram credential either.
"""

from __future__ import annotations

import pytest

from agent_core.adapters.driving.channels.registry import ChannelRegistry, UnknownChannelError
from agent_core.composition import Settings, build_container
from agent_core.domain.turn import TenantId


def _settings_with_no_channels() -> Settings:
    """Default settings: no bot token, no WABA credential, no channel profile."""
    return Settings()


def test_container_exposes_a_real_channel_registry() -> None:
    """`composition.py` must hand back t-f3-13's type, not a stand-in mapping.

    Only `ChannelRegistry` carries the raise-on-miss guarantee. A field merely typed
    `Mapping[str, Channel]` could be satisfied by a plain `dict` and would defeat D23's
    Shape A the moment someone reaches for `.get(channel_id)` instead of `.get` the
    registry's own raising method of the same name.
    """
    container = build_container(_settings_with_no_channels())

    assert hasattr(container, "channels"), (
        "Container has no 'channels' seat. t-f3-06 wires t-f3-13's ChannelRegistry here "
        "so ChannelHumanGateway and turn delivery share one lookup table (D23)."
    )
    assert isinstance(container.channels, ChannelRegistry), (
        f"container.channels is {type(container.channels)!r}, not ChannelRegistry - a "
        "plain dict or Mapping would let an unknown channel id resolve to None instead "
        "of raising."
    )


def test_an_unknown_channel_id_raises_at_wiring_time_rather_than_dropping_delivery() -> None:
    """The behaviour the task names directly.

    With no channel credentials configured, nothing is registered - and asking the
    wired registry for ANY channel id must raise loudly rather than a later delivery
    step discovering a miss and quietly swallowing the answer.
    """
    container = build_container(_settings_with_no_channels())

    with pytest.raises(UnknownChannelError) as raised:
        container.channels.get("telegram")

    assert "telegram" in str(raised.value)


def test_a_configured_channel_registers_and_an_unconfigured_one_still_raises() -> None:
    """Proves wiring actually happens, not just that an empty registry is safe.

    A fake bot token and a channel profile are enough to prove the registration path -
    no real Telegram credential or network call is involved, since nothing here ever
    calls `TelegramChannel.send`.
    """
    settings = Settings(
        telegram_bot_token="test-token-not-a-real-credential",
        channel_tenant_id=TenantId("tenant-under-test"),
        channel_profile_id="delivery_optimizer",
    )

    container = build_container(settings)

    assert "telegram" in container.channels
    with pytest.raises(UnknownChannelError):
        container.channels.get("whatsapp")
