"""IngestMedia - the untrusted-input boundary, and the ORDER its checks run in.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-03

THE ANCHOR ASSERTION IS AN ORDERING ONE
    `test_oversized_upload_is_rejected_before_any_sniff_or_store` is why this module
    exists. Size is checked FIRST, before the bytes are sniffed and before they reach
    `MediaStore.put`. A use case that sniffs first and then rejects on size is functionally
    identical from the outside - the same exception, the same refusal - and has already
    paid for the hostile upload by the time it says no.

    So the test does not assert "it raised". It asserts that the sniffer was never called
    and the store recorded zero calls. Nothing else can tell the two implementations apart,
    which is exactly the shape of defect CLAUDE.md's silent-bug table is about.

    The oversized payload is a VALID PNG of a kind the profile accepts. If it were garbage,
    a sniff-first implementation would also reject it and the test would prove nothing about
    order.

Fakes only: no database, no network, no model. `FakeMediaStore` is the one shared stand-in
for the port (tests/fakes/ports.py, docs/TASKS.md#t-f7-02) - this module used to hand-roll
its own, and `PutCall` is imported from there too so the equality assertions below compare
against the same dataclass the fake actually appends.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

import pytest

from agent_core.application import ingest_media as ingest_media_module
from agent_core.application.ingest_media import (
    DeclaredTypeMismatchError,
    IngestMedia,
    MediaRejectedError,
    MediaTooLargeError,
    UnacceptedMediaKindError,
    UnrecognisedMediaError,
)
from agent_core.domain.media import MediaDelivery, MediaKind, MediaPolicy, MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId
from tests.fakes.ports import FakeMediaStore
from tests.fakes.ports import MediaPutCall as PutCall

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff\xe0"
PDF_HEADER = b"%PDF-1.7\n"
OGG_HEADER = b"OggS\x00\x02"


def _png(payload_size: int = 32) -> bytes:
    return PNG_HEADER + b"\x00" * payload_size


@dataclass
class RecordedAudit:
    turn_id: TurnId
    media: MediaRef
    direction: str


@dataclass
class FakeAuditSink:
    """Only `record_media` is exercised; the other members exist so the fake still
    satisfies the port and a widened use case fails loudly rather than silently."""

    media: list[RecordedAudit] = field(default_factory=list)

    async def record_tool_call(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("IngestMedia must not record tool calls")

    async def record_human_decision(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("IngestMedia must not record human decisions")

    async def record_rejected_decision(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("IngestMedia must not record refused decisions")

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        self.media.append(RecordedAudit(turn_id, media, direction))

    async def record_turn_end(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("IngestMedia must not close a turn")


def _profile(
    *,
    accepted: frozenset[MediaKind] = frozenset({MediaKind.IMAGE}),
    max_bytes: int = 1024,
) -> AgentProfile:
    return AgentProfile(
        id="p-evidence",
        persona="collects evidence",
        model="MiniMax-M3",
        media=MediaPolicy(
            accepted_kinds=accepted,
            delivery=MediaDelivery.BYTES,
            max_bytes=max_bytes,
            allow_evidence_requests=True,
        ),
    )


TURN = TurnId("turn-1")


@pytest.fixture
def store() -> FakeMediaStore:
    return FakeMediaStore()


@pytest.fixture
def audit() -> FakeAuditSink:
    return FakeAuditSink()


@pytest.fixture
def use_case(store: FakeMediaStore, audit: FakeAuditSink) -> IngestMedia:
    return IngestMedia(media=store, audit=audit)


@pytest.mark.silent
def test_oversized_upload_is_rejected_before_any_sniff_or_store(
    use_case: IngestMedia,
    store: FakeMediaStore,
    audit: FakeAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE ANCHOR. Size first: the sniffer is never reached and the store records nothing.

    The payload is a well-formed PNG and the profile accepts images, so size is the only
    thing wrong with it. A sniff-first implementation raises the same exception and fails
    here on the two call counts."""
    sniffed: list[bytes] = []
    real_sniff = ingest_media_module.sniff_media

    def spy(data: bytes) -> object:
        sniffed.append(data)
        return real_sniff(data)

    monkeypatch.setattr(ingest_media_module, "sniff_media", spy)

    profile = _profile(max_bytes=64)
    oversized = _png(payload_size=4096)
    assert len(oversized) > profile.media.max_bytes

    with pytest.raises(MediaTooLargeError):
        asyncio.run(
            use_case.execute(TURN, profile, oversized, declared_mime="image/png"),
        )

    assert sniffed == [], "size must be checked BEFORE the bytes are sniffed"
    assert store.calls == [], "size must be checked BEFORE the bytes reach the store"
    assert audit.media == [], "a rejected upload is not an ingested one"


def test_a_payload_exactly_at_the_limit_is_accepted(
    use_case: IngestMedia, store: FakeMediaStore
) -> None:
    """`>` not `>=`: max_bytes is a maximum, not an exclusive bound."""
    data = _png(payload_size=8)
    profile = _profile(max_bytes=len(data))

    ref = asyncio.run(use_case.execute(TURN, profile, data, declared_mime="image/png"))

    assert ref.size_bytes == len(data)
    assert len(store.calls) == 1


def test_declared_mime_that_contradicts_the_bytes_is_rejected(
    use_case: IngestMedia, store: FakeMediaStore, audit: FakeAuditSink
) -> None:
    """A PDF announcing itself as a PNG. The declared type is a hint from an untrusted
    source and never decides anything."""
    profile = _profile(accepted=frozenset({MediaKind.IMAGE, MediaKind.DOCUMENT}))

    with pytest.raises(DeclaredTypeMismatchError):
        asyncio.run(
            use_case.execute(TURN, profile, PDF_HEADER + b"x" * 16, declared_mime="image/png")
        )

    assert store.calls == []
    assert audit.media == []


def test_the_stored_mime_is_the_sniffed_one_not_the_declared_one(
    use_case: IngestMedia, store: FakeMediaStore
) -> None:
    """`image/jpg` is not a mime type, but plenty of clients send it. What is stored is what
    the bytes say, so nothing downstream ever trusts the client's spelling."""
    profile = _profile()

    ref = asyncio.run(
        use_case.execute(TURN, profile, JPEG_HEADER + b"x" * 16, declared_mime="IMAGE/JPG")
    )

    assert ref.mime_type == "image/jpeg"
    assert store.calls[0].mime_type == "image/jpeg"
    assert store.calls[0].kind is MediaKind.IMAGE


def test_a_kind_the_profile_does_not_accept_is_rejected_after_sniffing(
    use_case: IngestMedia, store: FakeMediaStore, audit: FakeAuditSink
) -> None:
    profile = _profile(accepted=frozenset({MediaKind.IMAGE}))

    with pytest.raises(UnacceptedMediaKindError):
        asyncio.run(
            use_case.execute(TURN, profile, OGG_HEADER + b"x" * 16, declared_mime="audio/ogg")
        )

    assert store.calls == []
    assert audit.media == []


def test_an_empty_accepted_kinds_set_rejects_a_perfectly_valid_image(
    use_case: IngestMedia, store: FakeMediaStore
) -> None:
    """The asymmetry pinned in tests/unit/test_media.py, enforced at the boundary that
    actually admits bytes: empty means NOTHING, so an unconfigured profile ingests nothing."""
    profile = _profile(accepted=frozenset())

    with pytest.raises(UnacceptedMediaKindError):
        asyncio.run(use_case.execute(TURN, profile, _png(), declared_mime="image/png"))

    assert store.calls == []


def test_bytes_that_match_no_known_signature_are_rejected(
    use_case: IngestMedia, store: FakeMediaStore
) -> None:
    """Unrecognised is refused, never waved through as a document. Failing open here would
    let any payload in behind a mime type the client chose."""
    profile = _profile(accepted=frozenset(MediaKind))

    with pytest.raises(UnrecognisedMediaError):
        asyncio.run(
            use_case.execute(TURN, profile, b"not a file at all", declared_mime="image/png")
        )

    assert store.calls == []


def test_every_rejection_is_one_catchable_family() -> None:
    """A driving adapter answers 4xx for all four without string-matching a message."""
    for error in (
        MediaTooLargeError,
        DeclaredTypeMismatchError,
        UnacceptedMediaKindError,
        UnrecognisedMediaError,
    ):
        assert issubclass(error, MediaRejectedError)
        assert issubclass(error, ValueError)


def test_a_successful_ingest_stores_then_audits_inbound(
    use_case: IngestMedia, store: FakeMediaStore, audit: FakeAuditSink
) -> None:
    """The audit row carries the ref - and therefore the sha256 - never the bytes."""
    data = _png()
    profile = _profile()

    ref = asyncio.run(
        use_case.execute(TURN, profile, data, declared_mime="image/png", filename="proof.png")
    )

    assert store.calls == [PutCall(data, MediaKind.IMAGE, "image/png", "proof.png")]
    assert len(audit.media) == 1
    recorded = audit.media[0]
    assert recorded.turn_id == TURN
    assert recorded.direction == "inbound"
    assert recorded.media == ref
    assert recorded.media.sha256 == hashlib.sha256(data).hexdigest()


def test_an_attacker_controlled_filename_is_metadata_and_never_a_path(
    use_case: IngestMedia, store: FakeMediaStore
) -> None:
    """`MediaStore` names files by id. The original name travels as metadata, stripped of
    every directory component, so it cannot steer a write even if the adapter is careless."""
    profile = _profile()

    ref = asyncio.run(
        use_case.execute(
            TURN,
            profile,
            _png(),
            declared_mime="image/png",
            filename="../../../etc/cron.d/evil.png",
        )
    )

    assert store.calls[0].filename == "evil.png"
    assert ref.filename == "evil.png"


def test_a_declared_mime_that_claims_nothing_does_not_block_a_good_upload(
    use_case: IngestMedia, store: FakeMediaStore
) -> None:
    """`application/octet-stream` is what a channel sends when it does not know. It is the
    absence of a claim, so there is nothing for the bytes to contradict - and the sniffed
    type is still what gets stored."""
    profile = _profile()

    ref = asyncio.run(
        use_case.execute(TURN, profile, _png(), declared_mime="application/octet-stream")
    )

    assert ref.mime_type == "image/png"
    assert store.calls[0].mime_type == "image/png"
