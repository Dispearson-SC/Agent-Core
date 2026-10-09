"""A signed URL has to arrive at the runner still knowing what it points at.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-10

WHY THIS TEST EXISTS
    `MediaStore.signed_url` used to hand back a bare string, and `media_fs` signs a
    CONTENT-ADDRESSED path - `<base>/<sha256>?expires=&sig=` - with no extension on it. So
    `MediaRef.kind` and `MediaRef.mime_type`, both of which the store was holding one
    stack frame earlier, were gone by the time the payload reached the runner.
    `t-f7-07`'s `_evidence_url` then had nothing to type the provider part from and
    refused loudly, which was the right call and made SIGNED_URL delivery unusable
    against the only store that ships.

    The failure this prevents is the one docs/DECISIONS.md#d26 names: `ImageUrl` versus
    `DocumentUrl` decides whether the provider LOOKS at the file or READS it, and a
    provider handed the wrong one answers anyway. The symptom is a confident answer about
    a picture nobody could see - remote, silent, and nothing in CI goes red for it.

    Three things are locked here, and the third is the one that would be quietly undone:

    1. The type travels. An image stored as an image reaches the provider as an `ImageUrl`
       carrying its stored mime type, from a URL whose path has no extension at all.
    2. The path is still content-addressed. The obvious way to make `mimetypes.guess_type`
       work again is to put the uploaded filename - or an extension - back into the signed
       path, and a user-supplied filename on disk is a traversal and an overwrite in one
       (`t-f7-04`). The digest stays the only component of that path.
    3. The port cannot slide back. The negative case of the lock is the PRE-widening
       shape - a `signed_url` returning bare `str` - because a lock that only recognises
       the new shape recognises the old one just as happily (the `t-f1-05` precedent).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, get_type_hints
from urllib.parse import urlsplit

import pytest
from pydantic_ai.messages import AudioUrl, FileUrl, ImageUrl

from agent_core.adapters.driven.agent_pydantic.runner import evidence_content
from agent_core.adapters.driven.media_fs.store import FilesystemMediaStore
from agent_core.domain.media import MediaDelivery, MediaId, MediaKind, MediaRef
from agent_core.ports.media_store import MediaStore

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"evidence-bytes"
MP3_BYTES = b"ID3\x03\x00\x00\x00" + b"evidence-audio"

# The name the uploader chose. It is metadata and nothing else: it must not appear in the
# path, and it must not appear in the signed URL either.
HOSTILE_FILENAME = "../../../etc/passwd.png"

BASE_URL = "https://media.example.test/evidence"


def _store(root: Path) -> FilesystemMediaStore:
    return FilesystemMediaStore(
        root,
        delivery=MediaDelivery.SIGNED_URL,
        base_url=BASE_URL,
        signing_key=b"a-fixed-key-so-the-url-is-deterministic",
    )


# --------------------------------------------------------------------------------------
# 1. The type reaches the runner


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_a_signed_url_reaches_the_runner_as_the_image_it_points_at(tmp_path: Path) -> None:
    """An image stored as an image becomes an `ImageUrl`, from an extensionless path.

    This is the whole anchor. The URL's path is a bare sha256, so nothing can be read off
    it - the kind and the mime type have to arrive WITH the URL or not at all.
    """
    store = _store(tmp_path)
    ref = asyncio.run(
        store.put(PNG_BYTES, kind=MediaKind.IMAGE, mime_type="image/png", filename="photo.png")
    )

    signed = asyncio.run(store.signed_url(ref.media_id))

    assert not isinstance(signed, str), (
        "signed_url hands back a bare string, so MediaRef.kind and MediaRef.mime_type are "
        "already lost here and SIGNED_URL delivery cannot be typed. docs/TASKS.md#t-f7-10."
    )
    assert signed.ref.kind is MediaKind.IMAGE
    assert signed.ref.mime_type == "image/png"

    part = evidence_content(signed, MediaDelivery.SIGNED_URL)

    assert isinstance(part, ImageUrl), (
        f"an image behind a signed URL must reach the provider as an ImageUrl; got "
        f"{type(part).__name__}. A DocumentUrl makes the provider READ the file, and it "
        "answers about it either way - docs/DECISIONS.md#d26."
    )
    assert part.url == signed.url, "the signed URL is handed over verbatim"
    assert part.media_type == "image/png"


@pytest.mark.phase("F7")
def test_the_kind_and_not_the_extension_is_what_types_the_part(tmp_path: Path) -> None:
    """Audio proves the type came from the ref rather than from the URL.

    `mimetypes.guess_type` on `<base>/<sha256>?expires=&sig=` returns `None` - there is no
    extension. So a part typed correctly here can only have been typed from what the
    store knew.
    """
    store = _store(tmp_path)
    ref = asyncio.run(
        store.put(MP3_BYTES, kind=MediaKind.AUDIO, mime_type="audio/mpeg", filename="clip.mp3")
    )

    signed = asyncio.run(store.signed_url(ref.media_id))

    assert not isinstance(signed, str), "signed_url must carry the reference it signed"
    part = evidence_content(signed, MediaDelivery.SIGNED_URL)

    assert isinstance(part, AudioUrl), f"audio must arrive as an AudioUrl; got {type(part)}"
    assert part.media_type == "audio/mpeg"
    assert Path(urlsplit(signed.url).path).suffix == "", (
        "the signed path carries no extension, which is exactly why the type has to "
        "travel beside the URL"
    )


@pytest.mark.phase("F7")
def test_a_bytes_profile_still_refuses_to_promote_a_signed_url(tmp_path: Path) -> None:
    """D26's default half: a URL is a provider fetch, and a BYTES profile did not ask for
    one. The URL stays ordinary text the model reads, never a `FileUrl`."""
    store = _store(tmp_path)
    ref = asyncio.run(store.put(PNG_BYTES, kind=MediaKind.IMAGE, mime_type="image/png"))

    signed = asyncio.run(store.signed_url(ref.media_id))

    assert not isinstance(signed, str), "signed_url must carry the reference it signed"
    part = evidence_content(signed, MediaDelivery.BYTES)

    assert not isinstance(part, FileUrl), (
        "a BYTES profile must not be promoted into a provider fetch by the shape of a "
        "payload - the delivery mode is the decision, docs/DECISIONS.md#d26"
    )
    assert part == signed.url, (
        "the URL still reaches the model as the text it is; handing over a dataclass repr "
        "instead would make the model answer about a repr"
    )


# --------------------------------------------------------------------------------------
# 2. The path is still content-addressed


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_the_signed_path_carries_no_user_supplied_filename(tmp_path: Path) -> None:
    """The cheap fix for a missing type is to put the filename back in the path. It is
    also a traversal and an overwrite in one (`t-f7-04`), so it is locked out here.

    Nothing but the digest may appear in that path, and the uploader's name must not
    appear anywhere in the URL - not in the path, not in the query.
    """
    store = _store(tmp_path)
    ref = asyncio.run(
        store.put(
            PNG_BYTES, kind=MediaKind.IMAGE, mime_type="image/png", filename=HOSTILE_FILENAME
        )
    )

    signed = asyncio.run(store.signed_url(ref.media_id))

    assert not isinstance(signed, str), "signed_url must carry the reference it signed"
    assert signed.ref.filename == HOSTILE_FILENAME, (
        "the uploader's name survives as METADATA - that is the half that is allowed"
    )

    path = urlsplit(signed.url).path
    assert path == f"/evidence/{ref.sha256}", (
        f"the signed path is the content digest and nothing else; found {path!r}. A "
        "user-supplied name joined onto a path writes outside the root and overwrites "
        "somebody else's evidence."
    )
    assert "passwd" not in signed.url and ".." not in signed.url, (
        f"the uploader's filename leaked into the signed URL: {signed.url!r}"
    )


# --------------------------------------------------------------------------------------
# 3. The regression lock, with the PRE-widening shape as its negative case - t-f1-05


def _return_hint(member: Callable[..., object]) -> object:
    return get_type_hints(member).get("return")


def _carries_url_and_type(annotation: object) -> bool:
    """Does this return annotation hand back BOTH the URL and what it points at?

    Structural on purpose: the lock must not care what the type is called, only that a
    caller receives a URL to hand over and a `MediaRef` to type it from.
    """
    if not (isinstance(annotation, type) and dataclasses.is_dataclass(annotation)):
        return False
    hints = get_type_hints(annotation)
    return str in hints.values() and MediaRef in hints.values()


class _PreWideningStore(Protocol):
    """The port as it stood before `t-f7-10`: the URL, and nothing to type it with."""

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> str: ...


@dataclasses.dataclass(frozen=True, slots=True)
class _UrlOnly:
    """The near miss: a wrapper that renamed the string without carrying the type."""

    url: str


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_the_port_hands_back_the_url_together_with_what_it_points_at() -> None:
    """`MediaStore.signed_url` must return the reference alongside the URL.

    The lock is on the PORT rather than on the adapter: an S3 store swapped in later loses
    the type in exactly the same way, and the day it does no test that only knows about
    `media_fs` says anything.
    """
    hint = _return_hint(MediaStore.signed_url)

    assert _carries_url_and_type(hint), (
        f"signed_url returns {hint!r}. A signed URL must arrive carrying the MediaRef it "
        "was issued for: the store is the last place that still knows the kind and the "
        "mime type, and media_fs signs an extensionless content-addressed path. "
        "docs/TASKS.md#t-f7-10."
    )


@pytest.mark.phase("F7")
def test_the_lock_rejects_the_pre_widening_bare_string() -> None:
    """A lock that only recognises the new shape accepts the old one too - `t-f1-05`.

    So the negative case is the exact signature this anchor replaced, plus the wrapper
    that looks like progress and carries no type.
    """
    assert not _carries_url_and_type(_return_hint(_PreWideningStore.signed_url)), (
        "a bare `str` return is the defect t-f7-10 exists to close; a lock that passes it "
        "would pass the port it was written against"
    )
    assert not _carries_url_and_type(_UrlOnly), (
        "wrapping the string without the MediaRef changes the signature and fixes nothing"
    )
    assert not _carries_url_and_type(str)
