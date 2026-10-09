"""`SpeechPort` synthesis is unreachable unless a profile explicitly granted it.

Phase:   D2 - speech synthesis
Tasks:   docs/TASKS.md#t-d2-02

WHY THIS TEST EXISTS
    Speech output is the capability whose absence nobody notices and whose presence
    nobody sees until the invoice arrives. Generating audio for every reply multiplies
    cost and latency for a feature one channel asked for, and a run that produced audio
    nobody wanted is indistinguishable, in every log this system keeps, from a run that
    did not. It is not in `CLAUDE.md`'s silent-bug table, but it is the same shape.

    So the grant is locked STRUCTURALLY, on the port, rather than left to a rule about
    where the check belongs. Two properties:

    1. `synthesize` CANNOT BE CALLED WITHOUT THE PROFILE'S GRANT. `MediaPolicy` is a
       required, keyword-only, defaultless parameter, so producing the profile's media
       policy is a precondition of reaching the provider at all. There is no call shape
       in which the grant was simply forgotten, and the omission case - `MediaPolicy()`,
       the policy of a profile whose YAML has no `media:` block - carries
       `allow_speech_output=False`. Off unless explicitly granted, by construction.

    2. AN IMPLEMENTATION REFUSES A POLICY THAT DID NOT GRANT IT, BEFORE THE PROVIDER
       CALL. Passing the policy in is worth nothing if the adapter reads it after
       spending the money. `_CountingSpeech` is the reference conformance - it counts
       provider calls, and the refusal path must leave that count at zero.

    Both are applied to deliberate counterexamples, because a rule that has never
    rejected anything is not known to reject anything: a `synthesize` with no grant
    parameter at all, and one whose grant carries a default. The second is the subtle
    one. A default moves the decision out of the profile and into the port, where the
    day somebody edits it to a permissive value every profile in the system changes
    behaviour and no profile file changes.

WHY THE GRANT IS ON SYNTHESIS ONLY
    `transcribe` is input handling, already gated upstream: `MediaPolicy.accepts` decides
    whether AUDIO reaches the system at all, and `IngestMedia` enforces it before a byte
    is stored. Gating it twice would put the same decision in two places, which is how
    the two places drift apart.

The port is checked from `ports/` alone. `_CountingSpeech` is also the type-level half:
mypy proves it satisfies the port at the annotated assignment below, so the port cannot
drift from the shape asserted here without the project-wide mypy run failing too.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Protocol, get_type_hints

import pytest

from agent_core.domain.media import MediaId, MediaKind, MediaPolicy, MediaRef
from agent_core.ports.speech_port import SpeechPort


def _declared_members(protocol: type) -> dict[str, Callable[..., object]]:
    """The members the Protocol itself declares, without object/Protocol noise."""
    return {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def _speech_grant(member: Callable[..., object]) -> inspect.Parameter | None:
    """The parameter through which a profile's grant reaches this call, if there is one.

    All three conditions are the lock, not decoration:

    - the annotation IS `MediaPolicy`, so the grant is the profile's own policy object
      rather than a bare `allow=True` a call site can invent;
    - KEYWORD_ONLY, so it is named at every call site and cannot be supplied by position
      by a caller who has stopped reading the signature;
    - no default, so a call that did not think about speech does not compile.
    """
    signature = inspect.signature(member)
    hints = get_type_hints(member)
    for name, parameter in signature.parameters.items():
        if hints.get(name) is not MediaPolicy:
            continue
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        return parameter
    return None


class _UngatedSpeech(Protocol):
    """The first counterexample: synthesis a call site reaches with nothing but text."""

    async def synthesize(self, text: str, *, voice: str | None = None) -> bytes: ...


_ASSUMED_GRANT = MediaPolicy(allow_speech_output=True)
"""The counterexample's default. One edit here would flip every profile at once, which
is exactly why the lock below refuses a grant that carries a default at all."""


class _DefaultedSpeech(Protocol):
    """The second counterexample: the grant is present but the port answers it itself."""

    async def synthesize(
        self,
        text: str,
        *,
        policy: MediaPolicy = _ASSUMED_GRANT,
        voice: str | None = None,
    ) -> bytes: ...


class _CountingSpeech:
    """A conforming implementation that counts what it would have paid a provider for."""

    def __init__(self) -> None:
        self.provider_calls = 0

    async def transcribe(self, audio: MediaRef) -> str:
        self.provider_calls += 1
        return f"transcript of {audio.media_id}"

    async def synthesize(
        self, text: str, *, policy: MediaPolicy, voice: str | None = None
    ) -> bytes:
        if not policy.allow_speech_output:
            raise PermissionError(
                "this profile did not grant allow_speech_output; synthesis is opt-in"
            )
        self.provider_calls += 1
        return text.encode("utf-8")


_SPEECH: SpeechPort = _CountingSpeech()

_AUDIO = MediaRef(
    media_id=MediaId("m-1"),
    kind=MediaKind.AUDIO,
    mime_type="audio/wav",
    size_bytes=44,
    sha256="0" * 64,
)


@pytest.mark.phase("D2")
def test_synthesis_cannot_be_called_without_the_profile_grant() -> None:
    """The grant travels in the signature, so no call site can omit it.

    This is the half of the lock that does not depend on an implementation behaving. A
    `synthesize(text)` leaves the gate to a rule about call sites, and a rule about call
    sites is enforced by whoever remembers it.
    """
    members = _declared_members(SpeechPort)

    assert sorted(members) == ["synthesize", "transcribe"], (
        "SpeechPort answers one question - how do I convert between audio and text. "
        f"Found: {sorted(members)}."
    )

    grant = _speech_grant(members["synthesize"])
    assert grant is not None, (
        "synthesize takes no MediaPolicy, so nothing at the call site has to prove the "
        "profile granted speech output. Off-by-default becomes a rule somebody has to "
        "remember, and forgetting it costs money silently."
    )
    assert grant.name == "policy"

    signature = inspect.signature(members["synthesize"])
    assert [name for name in signature.parameters if name != "self"] == [
        "text",
        "policy",
        "voice",
    ]
    assert signature.parameters["voice"].default is None

    assert MediaPolicy().allow_speech_output is False, (
        "a profile with no media block must not be able to synthesize; omission is "
        "refusal, which is what makes the required parameter above a real gate"
    )


@pytest.mark.phase("D2")
def test_the_lock_rejects_an_ungated_or_self_answering_synthesize() -> None:
    """A rule that has never rejected anything is not known to reject anything."""
    assert _speech_grant(_declared_members(_UngatedSpeech)["synthesize"]) is None

    assert _speech_grant(_declared_members(_DefaultedSpeech)["synthesize"]) is None, (
        "a defaulted grant is the port deciding for every profile at once: the default "
        "is edited in one file and every agent in the system starts or stops speaking, "
        "with no profile changed and no review triggered"
    )


@pytest.mark.phase("D2")
def test_an_implementation_refuses_a_profile_that_did_not_grant_speech() -> None:
    """And refuses BEFORE the provider call, which is the whole point of refusing."""
    speech = _CountingSpeech()

    for policy in (MediaPolicy(), MediaPolicy(allow_speech_output=False)):
        with pytest.raises(PermissionError):
            asyncio.run(speech.synthesize("say this out loud", policy=policy))

    assert speech.provider_calls == 0, (
        "a refusal that happens after the provider call has already been paid for is "
        "not a refusal; it is the bill plus an exception"
    )

    audio = asyncio.run(
        speech.synthesize("say this out loud", policy=MediaPolicy(allow_speech_output=True))
    )
    assert isinstance(audio, bytes)
    assert speech.provider_calls == 1


@pytest.mark.phase("D2")
def test_transcription_is_not_gated_by_the_synthesis_grant() -> None:
    """Input is gated upstream by `MediaPolicy.accepts`; gating it twice invites drift."""
    members = _declared_members(SpeechPort)

    assert _speech_grant(members["transcribe"]) is None, (
        "allow_speech_output is about producing audio. Reusing it to gate transcription "
        "would silently disable audio input for every profile that only declined to talk."
    )
    assert get_type_hints(members["transcribe"]).get("return") is str

    speech = _CountingSpeech()
    assert asyncio.run(speech.transcribe(_AUDIO)) == "transcript of m-1"
    assert asyncio.run(_SPEECH.transcribe(_AUDIO)) == "transcript of m-1"


@pytest.mark.phase("D2")
def test_every_member_is_awaitable() -> None:
    """D13: both members are a network round trip to a speech provider. A sync `def`
    here blocks the event loop for the whole of it and stalls every concurrent turn."""
    for name, member in sorted(_declared_members(SpeechPort).items()):
        assert inspect.iscoroutinefunction(member), (
            f"SpeechPort.{name} is sync; ports/ is async end to end (D13). Speech is a "
            "provider call measured in seconds, not a computation."
        )
