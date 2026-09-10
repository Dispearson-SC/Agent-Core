"""Driven adapter: MediaStore over the filesystem.

Phase:   F7
Tasks:   docs/TASKS.md#t-f7-04
Status:  DONE - content-addressed writes, deduplication, and a signed_url that refuses
         while delivery is BYTES. tests/unit/test_media_fs.py.
Implements: ports/media_store.py

LAYOUT
    <media_root>/<sha256[:2]>/<sha256>          bytes, named by CONTENT
    <media_root>/<sha256[:2]>/<sha256>.json     metadata sidecar: kind, mime, size,
                                                filename, created_at

    Content addressing gives deduplication for free and makes the original filename pure
    metadata - which is what keeps an attacker-controlled name away from the filesystem.
    A USER-SUPPLIED FILENAME ON DISK IS A TRAVERSAL AND AN OVERWRITE IN ONE: joined onto
    the root, `../../../etc/passwd` writes outside it, and two people uploading
    `photo.png` silently replace each other's evidence. Naming the path after the sha256
    of the bytes removes both at once - there is no attacker-controlled component left in
    the path, and the same bytes are the same file instead of a collision.

    The sidecar is why `put` can honour the port's "return the EXISTING ref": the second
    upload of the same bytes gets the metadata recorded by the first, not its own. It
    lives beside the blob rather than in Postgres because F7 owns no media table; moving
    it to one changes this file and nothing above it.

VALIDATION HAPPENS IN IngestMedia, BEFORE bytes arrive here
    Size, magic-byte sniffing, and the profile's accepted kinds. Validating after storing
    means a hostile upload already consumed the disk.

    What is NOT delegated is the media id on the way back in. It travels through the model
    and through HTTP, so `get` and `signed_url` prove it is a 64-character lowercase
    digest before it is joined onto anything. An id that is not one this store issued
    cannot reach the filesystem at all.

signed_url() MUST REFUSE when the profile's delivery mode is BYTES
    Defence in depth. The decision belongs to MediaPolicy; this is the last place to catch
    a caller that ignored it, and the consequence of missing it is handing evidence with
    personal data to a third party. The refusal is a PermissionError: a caller must not be
    able to retry its way past it.

LATER: swap for S3-compatible storage. The port does not change - that is the point.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from agent_core.domain.media import MediaDelivery, MediaId, MediaKind, MediaRef

__all__ = [
    "FilesystemMediaStore",
    "MediaStoreError",
    "SignedUrlNotPermittedError",
    "UnknownMediaError",
]

_DIGEST_LENGTH = 64
_HEX = frozenset("0123456789abcdef")


class MediaStoreError(Exception):
    """Base for every refusal this adapter makes."""


class UnknownMediaError(MediaStoreError, LookupError):
    """No payload is stored under that id - including ids this store never issued.

    A LookupError rather than empty bytes: `b""` handed to the model is an empty image
    that nothing downstream reports as missing.
    """


class SignedUrlNotPermittedError(MediaStoreError, PermissionError):
    """A signed URL was asked for while the profile's delivery mode is BYTES."""


def _validated_digest(media_id: MediaId | str) -> str:
    """The id as a path component, or `UnknownMediaError`.

    This is the only place a caller-supplied string becomes part of a path, so it is the
    only place that has to be strict: exactly the shape `hashlib.sha256().hexdigest()`
    produces. `..`, an absolute path and a symlink name all fail the same check, which is
    why there is no separate test for any of them.
    """
    candidate = str(media_id)
    if len(candidate) != _DIGEST_LENGTH or not _HEX.issuperset(candidate):
        raise UnknownMediaError(
            "a media id is the 64-character lowercase sha256 of the payload; "
            "this store issued no such id"
        )
    return candidate


class FilesystemMediaStore:
    """`MediaStore` backed by a directory of content-addressed files.

    `delivery` mirrors `MediaPolicy.delivery` and defaults to BYTES, the private answer.
    A store configured for SIGNED_URL needs a `base_url` to point at and a `signing_key`
    to sign with; without a key it generates a process-local one, so URLs issued before a
    restart stop verifying after it - which is the safe direction for a credential nobody
    configured.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        delivery: MediaDelivery = MediaDelivery.BYTES,
        base_url: str | None = None,
        signing_key: bytes | None = None,
    ) -> None:
        if delivery is MediaDelivery.SIGNED_URL and not base_url:
            raise ValueError(
                "delivery=SIGNED_URL needs a base_url: the URL is handed to the model "
                "provider, so where it points is a deployment decision, not a default"
            )
        self._root = Path(root)
        self._delivery = delivery
        self._base_url = base_url.rstrip("/") if base_url else None
        self._signing_key = signing_key if signing_key is not None else secrets.token_bytes(32)

    async def put(
        self, data: bytes, *, kind: MediaKind, mime_type: str, filename: str | None = None
    ) -> MediaRef:
        """Store the payload under its own hash and hand back the reference.

        The same bytes uploaded twice are stored once and return the FIRST upload's ref,
        metadata included - the port's deduplication rule, and the reason a second
        uploader cannot rename somebody else's evidence.
        """
        return await asyncio.to_thread(self._put_sync, data, kind, mime_type, filename)

    async def get(self, media_id: MediaId) -> bytes:
        """Resolve to bare bytes. Raises `UnknownMediaError` when nothing is stored."""
        return await asyncio.to_thread(self._get_sync, media_id)

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> str:
        """Issue a short-lived URL for one medium, or refuse.

        The refusal comes first, before the medium is even looked up: whether a third
        party may fetch this file at all is a policy question, and answering it after a
        successful lookup would make an unknown id the more private outcome.
        """
        if self._delivery is not MediaDelivery.SIGNED_URL:
            raise SignedUrlNotPermittedError(
                f"delivery is {self._delivery.value}; a signed URL is fetched by the model "
                "provider itself, so it is issued only when MediaPolicy.delivery says "
                "SIGNED_URL"
            )
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive; the URL is public once issued")
        if self._base_url is None:  # pragma: no cover - the constructor already refused
            raise MediaStoreError("no base_url configured for SIGNED_URL delivery")

        digest = await asyncio.to_thread(self._resolved_digest_sync, media_id)
        expires = int(datetime.now(tz=UTC).timestamp()) + ttl_seconds
        signature = hmac.new(
            self._signing_key, f"{digest}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        query = urlencode({"expires": expires, "sig": signature})
        return f"{self._base_url}/{digest}?{query}"

    def _blob_path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest

    def _meta_path(self, digest: str) -> Path:
        return self._root / digest[:2] / f"{digest}.json"

    def _put_sync(
        self, data: bytes, kind: MediaKind, mime_type: str, filename: str | None
    ) -> MediaRef:
        digest = hashlib.sha256(data).hexdigest()
        blob = self._blob_path(digest)
        meta = self._meta_path(digest)

        if blob.exists() and meta.exists():
            return self._ref_from_meta(digest, meta)

        blob.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "media_id": digest,
            "kind": kind.value,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "sha256": digest,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "filename": filename,
        }
        self._write_atomically(blob, data)
        self._write_atomically(meta, json.dumps(record, ensure_ascii=False).encode("utf-8"))
        return self._ref_from_record(record)

    def _write_atomically(self, target: Path, payload: bytes) -> None:
        """Write through a temporary file in the same directory, then rename.

        A crash halfway through a large upload otherwise leaves a truncated file at a path
        that claims to be the sha256 of its contents, and every later reader trusts it.
        """
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _resolved_digest_sync(self, media_id: MediaId) -> str:
        digest = _validated_digest(media_id)
        if not self._blob_path(digest).is_file():
            raise UnknownMediaError(f"no payload stored under {digest}")
        return digest

    def _get_sync(self, media_id: MediaId) -> bytes:
        return self._blob_path(self._resolved_digest_sync(media_id)).read_bytes()

    def _ref_from_meta(self, digest: str, meta: Path) -> MediaRef:
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:  # pragma: no cover - a corrupted sidecar
            raise UnknownMediaError(f"metadata for {digest} is unreadable") from error
        return self._ref_from_record(record)

    def _ref_from_record(self, record: dict[str, Any]) -> MediaRef:
        created_at = record.get("created_at")
        return MediaRef(
            media_id=MediaId(str(record["media_id"])),
            kind=MediaKind(record["kind"]),
            mime_type=str(record["mime_type"]),
            size_bytes=int(record["size_bytes"]),
            sha256=str(record["sha256"]),
            created_at=datetime.fromisoformat(created_at) if created_at else None,
            filename=record.get("filename"),
        )
