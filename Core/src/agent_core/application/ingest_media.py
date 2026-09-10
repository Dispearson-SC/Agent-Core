"""Use case: IngestMedia - accept a file from a human and turn it into a MediaRef.

Phase:   F7
Tasks:   docs/TASKS.md#t-f7-03
Status:  IMPLEMENTED (t-f7-03)

WHERE THIS SITS IN THE EVIDENCE FLOW
    1. Agent calls `request_evidence(kind="photo", reason=...)` - an externally-executed
       deferred tool, so the run ENDS and returns DeferredToolRequests.
    2. HumanGateway publishes the ask to the channel.
    3. DBOS.recv() waits durably. No hung process; the service may restart.
    4. >>> THIS USE CASE <<< validates and stores the upload, returns a MediaRef.
    5. ResumeTurn resumes; the binary arrives as the deferred tool's result.

    To the agent it was simply a tool that returned an image.

THIS IS THE UNTRUSTED-INPUT BOUNDARY
    A file arriving from a channel is the least trusted input the system has. Everything
    below is validation, and every check must happen BEFORE the bytes reach storage.

THE ORDER IS THE CONTRACT, NOT AN IMPLEMENTATION DETAIL
    Size, then sniff, then accept, then store. Reordering these produces an identical
    refusal from the outside - same exception, same message - while having already paid
    for the hostile upload. `tests/unit/test_ingest_media.py` pins the order by counting
    calls rather than by catching the exception, because counting calls is the only thing
    that can tell the two apart.

WHY `execute` IS ASYNC (D13)
    It awaits `MediaStore.put` and `AuditSink.record_media`, both of which are I/O. The
    validation above them is pure and stays sync.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.domain.media import MediaKind, MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.media_store import MediaStore


class MediaRejectedError(ValueError):
    """An upload was refused at the boundary. Nothing was stored and nothing was audited.

    One family, four reasons. A driving adapter answers 4xx for the family without
    string-matching a message, and the subclasses let it say which 4xx when it cares.
    `ValueError` because the bytes are the bad argument.
    """


class MediaTooLargeError(MediaRejectedError):
    """Larger than `MediaPolicy.max_bytes`. Raised before the payload is even inspected."""


class UnrecognisedMediaError(MediaRejectedError):
    """No known signature matched. Refused rather than waved through as a document.

    Failing open here would admit any payload at all behind a mime type the client chose,
    which is exactly the trust this use case exists to withhold.
    """


class DeclaredTypeMismatchError(MediaRejectedError):
    """The bytes contradict `declared_mime`. The declared type never decides anything; it
    is only ever compared against what the bytes actually are."""


class UnacceptedMediaKindError(MediaRejectedError):
    """`MediaPolicy.accepts()` said no. An EMPTY `accepted_kinds` means NOTHING, so an
    unconfigured profile lands here for every upload - deliberately."""


@dataclass(frozen=True, slots=True)
class SniffedType:
    """What the magic bytes say. `mime_type` is the canonical spelling, which is what gets
    stored - never the client's."""

    kind: MediaKind
    mime_type: str


# Every prefix below is unambiguous on its own. The RIFF and ISO-BMFF families need a
# second look at a later offset and are handled separately in `sniff_media`.
_MAGIC_PREFIXES: tuple[tuple[bytes, MediaKind, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", MediaKind.IMAGE, "image/png"),
    (b"\xff\xd8\xff", MediaKind.IMAGE, "image/jpeg"),
    (b"GIF87a", MediaKind.IMAGE, "image/gif"),
    (b"GIF89a", MediaKind.IMAGE, "image/gif"),
    (b"BM", MediaKind.IMAGE, "image/bmp"),
    (b"%PDF-", MediaKind.DOCUMENT, "application/pdf"),
    (b"PK\x03\x04", MediaKind.DOCUMENT, "application/zip"),
    (b"OggS", MediaKind.AUDIO, "audio/ogg"),
    (b"fLaC", MediaKind.AUDIO, "audio/flac"),
    (b"ID3", MediaKind.AUDIO, "audio/mpeg"),
    (b"\xff\xfb", MediaKind.AUDIO, "audio/mpeg"),
    (b"\xff\xf3", MediaKind.AUDIO, "audio/mpeg"),
    (b"\x1a\x45\xdf\xa3", MediaKind.VIDEO, "video/webm"),
)

# A RIFF container is an image, an audio file or a video depending on a tag four bytes in.
# Matching on `RIFF` alone would file a .wav as an image.
_RIFF_FORMS: dict[bytes, tuple[MediaKind, str]] = {
    b"WEBP": (MediaKind.IMAGE, "image/webp"),
    b"WAVE": (MediaKind.AUDIO, "audio/wav"),
    b"AVI ": (MediaKind.VIDEO, "video/x-msvideo"),
}

# ISO base media: the brand decides. An .m4a and an .mp4 share a container, and calling
# an audio file a video would admit it under a profile that never accepted video.
_ISO_BRANDS: dict[bytes, tuple[MediaKind, str]] = {
    b"M4A ": (MediaKind.AUDIO, "audio/mp4"),
    b"M4B ": (MediaKind.AUDIO, "audio/mp4"),
}

# What a channel sends when it does not know. The absence of a claim, not a claim of
# "binary": there is nothing here for the bytes to contradict.
_NO_CLAIM = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# Spellings clients actually send. Deliberately short: this is a compatibility list for
# names that are unambiguous, not a place to make a genuine mismatch forgivable.
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-flac": "audio/flac",
    "application/x-pdf": "application/pdf",
    "application/x-zip-compressed": "application/zip",
}


def sniff_media(data: bytes) -> SniffedType | None:
    """What the bytes are, or None when no signature matched.

    A module-level function rather than a method so the ordering test can watch it: the
    anchor assertion in `tests/unit/test_ingest_media.py` is that an oversized upload never
    reaches this call. Sniffing costs little on its own, but every check after the size one
    is work done on behalf of a payload that was already too big to accept.
    """
    if data.startswith(b"RIFF") and len(data) >= 12:
        riff = _RIFF_FORMS.get(data[8:12])
        if riff is None:
            return None
        return SniffedType(riff[0], riff[1])

    if len(data) >= 12 and data[4:8] == b"ftyp":
        kind, mime_type = _ISO_BRANDS.get(data[8:12], (MediaKind.VIDEO, "video/mp4"))
        return SniffedType(kind, mime_type)

    for prefix, kind, mime_type in _MAGIC_PREFIXES:
        if data.startswith(prefix):
            return SniffedType(kind, mime_type)

    return None


def _normalise_mime(declared: str) -> str:
    """Lowercased, parameters dropped, known misspellings folded to their real name."""
    bare = declared.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(bare, bare)


def _safe_filename(filename: str | None) -> str | None:
    """The last path component, or None when nothing survives.

    `filename` is attacker-controlled and `MediaStore` names files by id, so this is belt
    and braces rather than the defence - but it costs one line and it means a careless
    adapter downstream cannot be steered into `../../etc/anything`.
    """
    if filename is None:
        return None
    tail = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not tail or tail in {".", ".."}:
        return None
    return tail


class IngestMedia:
    def __init__(self, *, media: MediaStore, audit: AuditSink) -> None:
        self._media = media
        self._audit = audit

    async def execute(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        data: bytes,
        *,
        declared_mime: str,
        filename: str | None = None,
    ) -> MediaRef:
        """Validate an upload and store it. Every refusal raises a `MediaRejectedError`.

        WHAT THIS USE CASE MUST NEVER DO
            - Return a ref for a file it did not fully validate.
            - Let `filename` reach a filesystem path. It is attacker-controlled;
              `MediaStore` names files by id, and the original name is metadata only.
            - Decide BYTES vs SIGNED_URL. That is `MediaPolicy.delivery`, read at the
              moment the file is handed to the model, not at ingestion.
        """
        policy = profile.media

        # STEP 1 - SIZE, BEFORE ANYTHING ELSE. `>` and not `>=`: max_bytes is a maximum.
        # Validating after storing means a hostile upload has already consumed the disk,
        # and validating after sniffing means it has already consumed the CPU.
        if len(data) > policy.max_bytes:
            raise MediaTooLargeError(
                f"Upload is {len(data)} bytes and the profile allows {policy.max_bytes}. "
                "Refused before the payload was inspected or stored."
            )

        # STEP 2 - SNIFF, DO NOT TRUST. The declared type is a hint from an untrusted
        # source: it is compared against the bytes and then discarded.
        sniffed = sniff_media(data)
        if sniffed is None:
            raise UnrecognisedMediaError(
                "No known signature matched the uploaded bytes. Refusing rather than "
                f"trusting the declared type {declared_mime!r}."
            )

        declared = _normalise_mime(declared_mime)
        if declared not in _NO_CLAIM and declared != sniffed.mime_type:
            raise DeclaredTypeMismatchError(
                f"Declared {declared_mime!r} but the bytes are {sniffed.mime_type!r}. "
                "A file that lies about its type is refused, not relabelled."
            )

        # STEP 3 - CHECK THE PROFILE ACCEPTS IT. Empty `accepted_kinds` means NOTHING; the
        # asymmetry with PolicyRule.subject_roles is deliberate and pinned in
        # tests/unit/test_media.py.
        if not policy.accepts(sniffed.kind):
            raise UnacceptedMediaKindError(
                f"Profile {profile.id!r} does not accept {sniffed.kind.value!r} media. "
                "An empty accepted_kinds accepts nothing, by design."
            )

        # STEP 4 - STORE. The mime that goes in is the SNIFFED one, never the declared one,
        # so nothing downstream ever inherits the client's spelling. `put` deduplicates by
        # sha256, so the same evidence uploaded twice is stored once.
        ref = await self._media.put(
            data,
            kind=sniffed.kind,
            mime_type=sniffed.mime_type,
            filename=_safe_filename(filename),
        )

        # STEP 5 - AUDIT, outside any domain transaction (CLAUDE.md #6). The hash goes in
        # the record, never the bytes: that hash is how an auditor later proves the file
        # attached to a case is the file that was uploaded.
        await self._audit.record_media(turn_id, ref, "inbound")

        # STEP 6 - RETURN THE REF.
        return ref
