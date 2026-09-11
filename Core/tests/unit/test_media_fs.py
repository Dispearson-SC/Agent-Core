"""The filesystem `MediaStore`: bytes are named by their content, and a signed URL
refuses to exist while the profile says BYTES.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-04

WHY THIS TEST EXISTS
    Two failures this adapter can produce are invisible to a green suite, which is the
    class `CLAUDE.md`'s silent-bug table is about.

    1. A USER-SUPPLIED FILENAME ON DISK IS A TRAVERSAL AND AN OVERWRITE IN ONE. Join an
       uploaded name onto the media root and `../../../etc/passwd` writes outside it,
       while two people uploading `photo.png` silently overwrite each other's evidence.
       Content addressing removes both at once: the path is the sha256 of the bytes, the
       name the uploader chose is pure metadata, and the second upload of the same bytes
       is the same file rather than a collision. Nothing goes red the day a filename
       reaches a path - the file simply lands somewhere it should not.

    2. `signed_url` HANDS A PROVIDER-REACHABLE URL TO A THIRD PARTY. `MediaPolicy.delivery`
       chooses, BYTES is the default and the private answer, and this method is the last
       place to catch a caller that ignored the policy. A store that issues a URL anyway
       leaks incident evidence to the model provider's infrastructure and returns a
       perfectly valid string while doing it.

    The adapter is reached through the module rather than through a direct `from ... import`
    so that this file fails on its assertions - naming what is missing - instead of dying
    at collection with an ImportError, which certifies nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_core.adapters.driven.media_fs import store as store_module
from agent_core.domain.media import MediaDelivery, MediaId, MediaKind
from agent_core.ports.media_store import MediaStore

if TYPE_CHECKING:  # pragma: no cover - a type-level conformance check, never executed
    from agent_core.adapters.driven.media_fs.store import FilesystemMediaStore

    def _the_adapter_satisfies_the_port(store: FilesystemMediaStore) -> MediaStore:
        """mypy proves the adapter still matches the port it implements. A signature that
        drifts from `ports/media_store.py` fails the project-wide mypy run rather than
        being discovered by whichever call site broke first."""
        return store


PNG = b"\x89PNG\r\n\x1a\n" + b"the payload"
PNG_SHA256 = hashlib.sha256(PNG).hexdigest()

HOSTILE_FILENAME = "../../../etc/passwd"


def _adapter(name: str) -> Any:
    """Resolve a name the adapter is supposed to export, and say what is missing when it
    is not there yet."""
    attribute = getattr(store_module, name, None)
    assert attribute is not None, (
        f"adapters/driven/media_fs/store.py exports no {name}. t-f7-04 implements "
        "MediaStore over the filesystem: content-addressed bytes, and a signed_url that "
        "refuses while delivery is BYTES."
    )
    return attribute


def _make_store(root: Path, **kwargs: Any) -> MediaStore:
    store: MediaStore = _adapter("FilesystemMediaStore")(root, **kwargs)
    return store


def _files_under(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_the_stored_path_is_the_content_hash_never_the_uploaded_filename(tmp_path: Path) -> None:
    """The uploader names the file; the content names the path.

    A filename joined onto the media root is a traversal and an overwrite in one, and
    neither of them fails a test - the bytes just land somewhere they should not, or on
    top of somebody else's evidence.
    """
    root = tmp_path / "media"
    store = _make_store(root)

    ref = asyncio.run(
        store.put(PNG, kind=MediaKind.IMAGE, mime_type="image/png", filename=HOSTILE_FILENAME)
    )

    assert ref.sha256 == PNG_SHA256
    assert ref.size_bytes == len(PNG)
    assert (root / PNG_SHA256[:2] / PNG_SHA256).read_bytes() == PNG, (
        "the payload lives at <root>/<sha256[:2]>/<sha256>"
    )

    written = _files_under(root)
    assert written, "put wrote nothing"
    assert all(PNG_SHA256 in path for path in written), (
        f"every path this upload created is named by its content hash; found {written}"
    )
    assert not any("passwd" in path or ".." in path for path in written), (
        f"the uploaded filename reached the filesystem: {written}"
    )
    assert [entry.name for entry in tmp_path.iterdir()] == ["media"], (
        "the upload escaped the media root - a traversal in the filename was joined onto it"
    )

    assert ref.filename == HOSTILE_FILENAME, (
        "the original name survives as metadata on the ref, which is exactly what makes "
        "it harmless: it is recorded, never resolved"
    )
    assert asyncio.run(store.get(ref.media_id)) == PNG


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_signed_url_refuses_while_delivery_is_bytes(tmp_path: Path) -> None:
    """Defence in depth for the privacy decision.

    `MediaPolicy.delivery` owns the choice; this method is the last place to catch a
    caller that ignored it. Issuing the URL anyway hands evidence that may contain
    personal data to the model provider, and returns a valid string while doing it.
    """
    store = _make_store(tmp_path / "media", delivery=MediaDelivery.BYTES)
    ref = asyncio.run(store.put(PNG, kind=MediaKind.IMAGE, mime_type="image/png"))

    refusal = _adapter("SignedUrlNotPermittedError")

    with pytest.raises(refusal):
        asyncio.run(store.signed_url(ref.media_id))

    with pytest.raises(refusal):
        asyncio.run(store.signed_url(ref.media_id, ttl_seconds=30))

    assert issubclass(refusal, PermissionError), (
        "refusing to hand a payload to a third party is a permission failure, not a "
        "lookup failure - a caller must not be able to retry its way past it"
    )


@pytest.mark.phase("F7")
def test_bytes_is_the_default_delivery(tmp_path: Path) -> None:
    """A store constructed without an opinion is the private one. Choosing SIGNED_URL is
    an explicit act, never something that happens by omission."""
    store = _make_store(tmp_path / "media")
    ref = asyncio.run(store.put(PNG, kind=MediaKind.IMAGE, mime_type="image/png"))

    with pytest.raises(_adapter("SignedUrlNotPermittedError")):
        asyncio.run(store.signed_url(ref.media_id))


@pytest.mark.phase("F7")
def test_a_signed_url_is_issued_with_a_short_expiry_when_delivery_says_so(
    tmp_path: Path,
) -> None:
    """The URL is public the moment it is issued: it names one medium, it expires, and it
    carries a signature rather than being a guessable path."""
    store = _make_store(
        tmp_path / "media",
        delivery=MediaDelivery.SIGNED_URL,
        base_url="https://media.example.invalid/evidence",
        signing_key=b"a-test-signing-key",
    )
    ref = asyncio.run(store.put(PNG, kind=MediaKind.IMAGE, mime_type="image/png"))

    issued_at = int(time.time())
    signed = asyncio.run(store.signed_url(ref.media_id))
    # t-f7-10: the store hands back the URL AND the ref it was issued for. The path is
    # still the content hash - deliberately extensionless, per the traversal reasoning at
    # the top of this file - so the type travels beside it or not at all.
    url = signed.url

    assert signed.ref == ref, "the URL arrives with the reference it names"
    assert url.startswith("https://media.example.invalid/evidence/")
    assert PNG_SHA256 in url, "the URL names one medium and cannot be reused for another"

    query = parse_qs(urlsplit(url).query)
    expires = int(query["expires"][0])
    assert issued_at + 300 <= expires <= issued_at + 301, (
        f"the default expiry is 300 seconds; found {expires - issued_at}"
    )
    assert query["sig"][0], "an unsigned URL is a guessable path with extra steps"

    shorter = asyncio.run(store.signed_url(ref.media_id, ttl_seconds=30)).url
    assert int(parse_qs(urlsplit(shorter).query)["expires"][0]) < expires
    assert parse_qs(urlsplit(shorter).query)["sig"][0] != query["sig"][0], (
        "the signature covers the expiry, or the ttl is decoration"
    )

    with pytest.raises(ValueError):
        asyncio.run(store.signed_url(ref.media_id, ttl_seconds=0))


@pytest.mark.phase("F7")
def test_the_same_payload_uploaded_twice_is_stored_once(tmp_path: Path) -> None:
    """Deduplication is what content addressing gives away for free, and it is why the
    second upload of the same evidence cannot overwrite the first."""
    root = tmp_path / "media"
    store = _make_store(root)

    first = asyncio.run(
        store.put(PNG, kind=MediaKind.IMAGE, mime_type="image/png", filename="evidence.png")
    )
    after_first = _files_under(root)
    second = asyncio.run(
        store.put(PNG, kind=MediaKind.IMAGE, mime_type="image/png", filename="renamed.png")
    )

    assert second.media_id == first.media_id
    assert second == first, "the same bytes resolve to the EXISTING ref, metadata included"
    assert _files_under(root) == after_first, "the second upload wrote a second copy"

    other = asyncio.run(store.put(b"different bytes", kind=MediaKind.IMAGE, mime_type="image/png"))
    assert other.media_id != first.media_id
    assert asyncio.run(store.get(first.media_id)) == PNG


@pytest.mark.phase("F7")
def test_an_unknown_medium_raises_instead_of_returning_empty_bytes(tmp_path: Path) -> None:
    """`b""` for a missing file sends the model an empty image and nothing goes red."""
    store = _make_store(tmp_path / "media")

    with pytest.raises(LookupError):
        asyncio.run(store.get(MediaId("0" * 64)))


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_a_media_id_that_is_not_a_digest_never_becomes_a_path(tmp_path: Path) -> None:
    """The id travels through the model and through HTTP, so it is validated before it is
    joined onto anything. A digest is 64 hex characters; everything else is unknown."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"not evidence")
    store = _make_store(tmp_path / "media")

    for hostile in ("../../secret.txt", "../secret", "/etc/passwd", "", "A" * 64, PNG_SHA256[:63]):
        with pytest.raises(LookupError):
            asyncio.run(store.get(MediaId(hostile)))

    assert secret.read_bytes() == b"not evidence"
