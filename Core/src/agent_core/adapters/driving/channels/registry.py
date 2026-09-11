"""The channel shape and the registry that keys it - D23 Shape A, not a sixteenth port.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-13
Status:  DONE - Protocol frozen and registry enforced; t-f3-08, t-f3-09, t-f3-06 build on it
Tests:   Core/tests/unit/test_channel_registry.py

WHY THIS IS AN ADAPTER AND NOT A PORT
    D23 settled it: outbound delivery of a finished `TurnResult` rides a
    composition-level registry plus one generic delivery step, and adds NO port. Two
    independent reasons, either one sufficient.

    `HumanGateway` cannot absorb it - that port answers "who do I ask, and how do I wait
    for the answer?", and delivering a finished result is a different question. Folding
    it in would be the two-questions-one-port mistake CLAUDE.md calls a wrong cut.

    And a new port would not clear D15's bar, because it adds no question the DOMAIN
    asks. It is dispatch over a value the domain already carries:
    `CallerIdentity.channel`. D10 set the precedent when MCP composed into
    `ToolProvider` rather than becoming its own port.

    So `Channel` is a structural Protocol living under `adapters/driving/channels/`.
    `application/` never names a channel and must not learn how to. If a use case ever
    genuinely needs to, that is D23 being reopened deliberately - not this file being
    moved.

WHY THE PROTOCOL IS STRUCTURAL
    `t-f3-08` (WhatsApp Cloud API) and `t-f3-09` (Telegram Bot API) register by HAVING
    an async `send`, never by importing and subclassing anything from here. That keeps
    each channel adapter a leaf: it depends on its platform SDK and on the domain
    vocabulary, and on nothing else in the repository.

WHY A DUPLICATE ID IS FATAL AT CONSTRUCTION
    The registry is built from pairs in `composition.py` (`t-f3-06`). Feeding pairs to a
    `dict` keeps the LAST entry for a repeated key and discards the other in silence -
    no error, no log line, nothing to grep. The consequence appears much later and far
    away: answers delivered to the wrong platform, or to nobody, on a code path a
    reviewer already approved.

    Construction is the last moment where that mistake is still cheap, so it raises
    there rather than at the first `get`. Same reasoning as `TurnRequest` having no
    defaults - fail where the mistake was made, not three layers down.

WHY `send` IS A COROUTINE (D13)
    Sending means calling the WhatsApp or Telegram send API over the network. A sync
    `def` here would block the event loop for the whole platform round-trip and stall
    every other turn in the process. Every member of this shape is awaitable, and the
    unit test pins it so a future channel adapter cannot quietly introduce a sync one.

WHO USES IT
    - `t-f3-07`'s `_step_deliver` in `adapters/driving/workflow/turn_workflow.py`,
      dispatching a finished turn to the channel it arrived on.
    - `ChannelHumanGateway` (`adapters/driven/human/gateway.py`), for the mid-turn
      approval or evidence prompt. One lookup table, both paths - that shared use is the
      point of D23's Shape A.

THE PICKLE TRAP (t-f3-18)
    DBOS pickles a workflow's raised exception to hand it back to a caller of
    `get_result()`. `BaseException.__reduce__` reconstructs the instance by calling
    `type(self)(*self.args)` - and `self.args` is whatever was passed to
    `Exception.__init__`, which here is the ALREADY-FORMATTED MESSAGE STRING, not the
    structured `channel_id` (and `known`) the real `__init__` requires. An `__init__`
    with more than one required positional parameter beyond that message is one argument
    short at unpickle time, and the caller sees a `TypeError` about this class's own
    constructor instead of the delivery failure that was actually raised - exactly the
    "mangled, not loud" failure `t-f3-07` exists to prevent.

    Both errors below fix this with an explicit `__reduce__` that hands back the
    STRUCTURED constructor arguments instead of the formatted string, so unpickling calls
    `__init__` the same way the original raise site did. ANY exception raised inside a
    DBOS workflow with a required constructor argument beyond a single message has this
    same trap - give it the same treatment (a defaulted extra parameter, or its own
    `__reduce__`) before it crosses a workflow boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_core.domain.media import MediaRef
from agent_core.domain.turn import CallerIdentity, TurnResult


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """What leaves the system on a channel.

    Deliberately NOT a `TurnResult`. Two paths share this registry and only one of them
    has a finished turn to send: `ChannelHumanGateway` publishes an approval prompt
    mid-turn, and there is no result yet. A shape carrying only what every channel can
    actually put on the wire keeps both callers honest, and keeps `usage` and
    `finished_at` - which belong to the audit trail, not to a WhatsApp message - out of
    a channel adapter's reach.
    """

    text: str
    media: tuple[MediaRef, ...] = ()

    @classmethod
    def from_turn_result(cls, result: TurnResult) -> OutboundMessage:
        """The `t-f3-07` seam: a finished turn reduced to what a platform can send."""
        return cls(text=result.text, media=result.media)


class ChannelRegistryError(Exception):
    """Base for every wiring mistake this module refuses to accept silently."""


class DuplicateChannelError(ChannelRegistryError):
    """Two entries claimed the same channel id. A `dict` would have kept one of them."""

    def __init__(self, channel_id: str) -> None:
        super().__init__(
            f"channel id {channel_id!r} was registered more than once; "
            "one of the two adapters would have been discarded silently"
        )
        self.channel_id = channel_id

    def __reduce__(self) -> tuple[type[DuplicateChannelError], tuple[str]]:
        """Round-trip through pickle with the STRUCTURED arg, not the formatted message.

        See the module docstring's "THE PICKLE TRAP" - without this, DBOS unpickling
        this exception at `get_result()` would call `DuplicateChannelError(message)`,
        which happens to type-check (one required parameter) but would then re-format a
        message that quotes the ALREADY-FORMATTED message as the channel id.
        """
        return (self.__class__, (self.channel_id,))


class UnknownChannelError(ChannelRegistryError):
    """Nothing is registered under that id.

    This raises rather than returning `None` on purpose. A miss means an answer has
    nowhere to go, and the one outcome worse than a loud failure here is a turn that
    completes, is audited as delivered, and reaches nobody.
    """

    def __init__(self, channel_id: str, known: tuple[str, ...]) -> None:
        super().__init__(f"no channel registered under {channel_id!r}; registered: {list(known)}")
        self.channel_id = channel_id
        self.known = known

    def __reduce__(self) -> tuple[type[UnknownChannelError], tuple[str, tuple[str, ...]]]:
        """Round-trip through pickle with the STRUCTURED args, not the formatted message.

        See the module docstring's "THE PICKLE TRAP" (t-f3-18). Without this,
        `BaseException.__reduce__` hands back only `self.args` - the already-formatted
        message string - and DBOS unpickling this at `get_result()` would call
        `UnknownChannelError(message)`, one positional argument short of `known`. That
        surfaces to the caller as `TypeError: UnknownChannelError.__init__() missing 1
        required positional argument: 'known'`, with the channel id nowhere in it.
        """
        return (self.__class__, (self.channel_id, self.known))


@runtime_checkable
class Channel(Protocol):
    """How do I put this message on that platform? One question, one member.

    Anything else a channel adapter does - verifying a webhook signature, parsing an
    inbound payload, mapping a platform id onto a `SessionRef` - belongs to the adapter
    and stays off this shape. Those are inbound concerns; this is the outbound half, and
    it is the only half the workflow and the human gateway need to know about.
    """

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        """Deliver `message` to `caller` on this channel."""
        ...


class ChannelRegistry:
    """channel id -> `Channel`, fixed once built.

    Built from PAIRS rather than from a mapping, and that is the whole design: a mapping
    cannot express the mistake this type exists to reject, because the caller's own
    `dict` literal has already resolved it silently.
    """

    __slots__ = ("_channels",)

    def __init__(self, channels: Iterable[tuple[str, Channel]]) -> None:
        registered: dict[str, Channel] = {}
        for channel_id, channel in channels:
            if channel_id in registered:
                raise DuplicateChannelError(channel_id)
            registered[channel_id] = channel
        self._channels = registered

    def channel_ids(self) -> tuple[str, ...]:
        """Registered ids, in registration order."""
        return tuple(self._channels)

    def get(self, channel_id: str) -> Channel:
        """The channel registered under `channel_id`, or `UnknownChannelError`."""
        try:
            return self._channels[channel_id]
        except KeyError:
            raise UnknownChannelError(channel_id, self.channel_ids()) from None

    async def deliver(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        """Route by the channel the caller arrived on.

        `CallerIdentity.channel` is the key. No routing field is invented for delivery -
        D23's second argument against a port was precisely that the value already exists
        on a domain type every turn carries.
        """
        await self.get(caller.channel).send(caller, message)

    def as_mapping(self) -> Mapping[str, Channel]:
        """A read-only snapshot, for a caller that wants to iterate rather than look up."""
        return dict(self._channels)

    def __contains__(self, channel_id: object) -> bool:
        return channel_id in self._channels

    def __iter__(self) -> Iterator[str]:
        return iter(self._channels)

    def __len__(self) -> int:
        return len(self._channels)
