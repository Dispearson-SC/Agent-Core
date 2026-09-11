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
    carries no channel credentials, so the registry under test only ever has `http`
    wired - which is exactly the case that must raise instead of quietly resolving to
    nothing for every OTHER channel id.
    The one positive-wiring test below constructs `Settings` directly with a fake token,
    never through the environment, so it needs no real Telegram credential either.
"""

from __future__ import annotations

import pickle

import pytest

from agent_core.adapters.driving.channels.registry import (
    ChannelRegistry,
    DuplicateChannelError,
    UnknownChannelError,
)
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

    With no push-channel credentials configured, only `http` is registered - it is
    always here (composition.py), the push channels are conditional - and asking the
    wired registry for an UNCONFIGURED channel id must raise loudly rather than a later
    delivery step discovering a miss and quietly swallowing the answer.
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


def test_unknown_channel_error_round_trips_through_pickle_like_dbos_does() -> None:
    """t-f3-18: a channel error must survive the durable boundary as ITSELF.

    DBOS pickles a workflow's exception to hand it to `get_result()`. Before this fix,
    `BaseException.__reduce__` handed back only `self.args` - the already-formatted
    message string - and reconstructing called `UnknownChannelError(that_string)`,
    which is one positional argument short of the real `__init__(channel_id, known)`.
    The caller of `get_result()` saw `TypeError: UnknownChannelError.__init__()
    missing 1 required positional argument: 'known'` - a failure about our own
    constructor, with the channel id nowhere in it.

    This asserts the PROPERTY the fix owes, not its shape (a defaulted parameter and a
    custom `__reduce__` both satisfy this): pickle it, unpickle it, and the result must
    still be an `UnknownChannelError` naming the channel - never a `TypeError`.
    """
    original = UnknownChannelError("telegram", ("http",))

    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, UnknownChannelError), (
        f"unpickling produced {type(restored)!r} instead of UnknownChannelError - this "
        "is the TypeError-from-our-own-constructor a get_result() caller would see "
        "instead of the real delivery failure."
    )
    assert "telegram" in str(restored), (
        "the channel id must survive the round trip; a caller three layers past the "
        "durable boundary needs to know WHICH channel had no wiring."
    )
    assert restored.channel_id == "telegram"
    assert restored.known == ("http",)


def test_duplicate_channel_error_round_trips_through_pickle_like_dbos_does() -> None:
    """Same trap, identical shape: a second positional-only constructor argument.

    `DuplicateChannelError.__init__` takes only `channel_id`, so it happens to survive
    today's `BaseException.__reduce__` - but only because it has one required argument,
    not zero. Pinning the round trip here too means nobody "fixes" `UnknownChannelError`
    in a way that reintroduces the trap for its sibling.
    """
    original = DuplicateChannelError("whatsapp")

    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, DuplicateChannelError)
    assert "whatsapp" in str(restored)
    assert restored.channel_id == "whatsapp"
