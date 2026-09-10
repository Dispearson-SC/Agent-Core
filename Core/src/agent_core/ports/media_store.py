"""Port: MediaStore - where does this binary live and how do I retrieve it?

Phase:      F7
Tasks:      docs/TASKS.md#t-f7-02
Adapter:    adapters/driven/media_fs/ (disk day 1, S3-compatible later)
Per-vertical: NO
Status:     FROZEN (t-f7-02) - members are async per D13; bodies stay pseudo-code
            until the adapter lands at t-f7-04

WHY BYTES NEVER TOUCH THE DOMAIN
    The domain holds `MediaRef`. Payloads in domain objects make turn records enormous and
    leak into logs and exception traces. Resolution happens at the adapter edge, as late
    as possible.

THE PRIVACY DECISION THIS PORT IMPLEMENTS
    Pydantic AI, given `ImageUrl` or `AudioUrl`, SENDS THE URL TO THE PROVIDER, which
    downloads the file itself. For incident evidence containing personal data that
    exposes a URL reachable from the provider's infrastructure.

    `MediaPolicy.delivery` chooses. BYTES is the default and the safe answer.
    `signed_url` exists for when payload size genuinely forces it - and then the URL must
    be short-lived and single-purpose. docs/ARCHITECTURE.md#7.

WHY EVERY MEMBER IS ASYNC (D13)
    All three are I/O and none of them is fast: `put` writes a payload to disk or to an
    object store, `get` reads one back, and `signed_url` asks the storage backend to issue
    a signature. A sync `def` here blocks the event loop for the whole of that, so one
    large upload stalls every other turn in the process. Any `@DBOS.transaction` writing
    the metadata row underneath stays synchronous and the adapter wraps it in
    `asyncio.to_thread`, exactly as in `ConversationStore`.

WHY signed_url IS ITS OWN MEMBER
    Because the alternative - `get(media_id, as_url=True)` - moves the BYTES-versus-
    SIGNED_URL choice out of `MediaPolicy.delivery` and into whatever keyword argument a
    call site happened to pass. Two members keep the decision visible: code that wants a
    provider-reachable URL has to name it. Locked in tests/unit/test_ports_media_store.py,
    together with the rule that no member returns a payload inside a domain type.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.media import MediaId, MediaKind, MediaRef


class MediaStore(Protocol):
    async def put(
        self, data: bytes, *, kind: MediaKind, mime_type: str, filename: str | None = None
    ) -> MediaRef:
        """PSEUDO-CODE - F7.

        ASYNC (D13): step 3 writes the payload and persists a metadata row.

        1. Compute sha256.
        2. If that hash already exists, return the EXISTING ref - the same evidence
           uploaded twice is stored once.
        3. Otherwise write bytes, persist metadata, return the ref.

        SIZE IS VALIDATED BY THE CALLER (`IngestMedia`) BEFORE reaching here. Validating
        after storing means a hostile upload has already consumed the disk.

        MIME SNIFFING: never trust the client's declared mime_type. Sniff the magic bytes
        and reject a mismatch. A .png that is actually a script is only dangerous once
        something downstream trusts the extension.
        """
        ...

    async def get(self, media_id: MediaId) -> bytes:
        """Resolve to bytes. Raises when unknown - never returns empty bytes for a missing
        file, which would silently send the model an empty image.

        ASYNC (D13): a read from disk or from an object store.

        The payload is handed back as bare `bytes`, never wrapped in a domain type. A
        `MediaRef` that carries its own payload turns every turn record into megabytes and
        prints the file into exception traces, and nothing fails when it happens."""
        ...

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> str:
        """PSEUDO-CODE - F7, only reachable when policy delivery is SIGNED_URL.

        ASYNC (D13): the storage backend issues the signature.

        Short TTL by default. The URL is handed to a third party (the model provider), so
        treat it as public the moment it is issued: no guessable ids, no long expiry, no
        reuse across media.

        The implementation MUST refuse to issue a URL when the profile's delivery mode is
        BYTES. Defence in depth: the decision belongs to the policy, and this method is
        the last place to catch a caller that ignored it.
        """
        ...
