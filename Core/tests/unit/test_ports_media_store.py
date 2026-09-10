"""`MediaStore` resolves a reference to a payload, and it never hands a payload back
inside a domain type.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-02

WHY THIS TEST EXISTS
    Two properties of this port fail in production rather than in CI, which is exactly the
    class `CLAUDE.md`'s silent-bug table is about.

    1. BYTES NEVER TRAVEL INSIDE A DOMAIN TYPE. The domain holds `MediaRef`; a payload
       reaching a domain object makes every turn record enormous, unserialisable in a
       conversation history, and leaks the file into logs and exception traces. Nothing
       fails when it happens - the suite stays green and the bill and the log volume are
       the only symptoms. So the lock is structural and lives on the port: a return
       annotation may either BE `bytes` (the adapter edge, resolving as late as possible)
       or mention bytes nowhere at all. `tuple[MediaRef, bytes]` and a `MediaRef` grown a
       `payload` field are the same defect, and both are rejected here.

    2. `signed_url` IS A SEPARATE MEMBER FROM THE BYTES ONE. `MediaPolicy.delivery`
       chooses between them, BYTES is the default and the private answer, and a signed URL
       is handed to the model provider which fetches the file from its own infrastructure.
       Fusing the two into one member - `fetch(media_id, as_url=True)` - moves that
       privacy decision from the policy into a call site's keyword argument, where no
       reviewer will ever see it again. Two members means a caller that wants the URL has
       to name the URL. docs/ARCHITECTURE.md#7.

    Both locks are applied to a deliberate counterexample as well, because a rule that has
    never rejected anything is not known to reject anything.

    The port is checked structurally, from `ports/` alone: a test that has to import a
    concrete adapter to verify a contract is testing the adapter. `_InMemoryStore` below
    is the type-level half - mypy proves it satisfies the port at the annotated
    assignment, so the port cannot drift from the shape asserted here without the
    project-wide mypy run failing too.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, get_args, get_type_hints

import pytest

from agent_core.domain.media import MediaId, MediaKind, MediaRef
from agent_core.ports.media_store import MediaStore

# Every spelling of "a payload". A port may hand one back only as itself.
PAYLOAD_TYPES: tuple[object, ...] = (bytes, bytearray, memoryview)


def _carries_bytes(annotation: object, seen: frozenset[object] | None = None) -> bool:
    """Does a payload hide anywhere inside this annotation?

    Recurses through generic arguments AND through dataclass fields, because the two ways
    a payload reaches the domain look nothing alike at the call site: `tuple[MediaRef,
    bytes]` announces itself, while a `MediaRef` that quietly grew a `payload: bytes`
    field changes no signature anywhere in the system.
    """
    if any(annotation is payload for payload in PAYLOAD_TYPES):
        return True

    seen = frozenset() if seen is None else seen
    try:
        if annotation in seen:
            return False
        seen = seen | {annotation}
    except TypeError:  # pragma: no cover - an unhashable annotation is still walkable
        pass

    if any(_carries_bytes(arg, seen) for arg in get_args(annotation)):
        return True

    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return any(
            _carries_bytes(hints[field.name], seen) for field in dataclasses.fields(annotation)
        )

    return False


def _declared_members(protocol: type) -> dict[str, Callable[..., object]]:
    """The members the Protocol itself declares, without object/Protocol noise."""
    return {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def _return_hint(member: Callable[..., object]) -> object:
    return get_type_hints(member).get("return")


def _members_returning_a_payload(protocol: type) -> frozenset[str]:
    return frozenset(
        name
        for name, member in _declared_members(protocol).items()
        if _carries_bytes(_return_hint(member))
    )


def _members_smuggling_a_payload(protocol: type) -> frozenset[str]:
    """Payload-returning members that do NOT return it as bare `bytes`.

    This is the lock. Returning `bytes` is the adapter edge doing its job; returning
    anything else that CONTAINS bytes is a payload that has entered a structured type and
    will travel with it into a turn record.
    """
    return frozenset(
        name
        for name in _members_returning_a_payload(protocol)
        if _return_hint(_declared_members(protocol)[name]) is not bytes
    )


@dataclass(frozen=True, slots=True)
class _InlinedRef:
    """The counterexample's ref: a `MediaRef` that grew a payload. No signature in the
    system changes when this happens, which is precisely why it is checked here."""

    media_id: MediaId
    kind: MediaKind
    payload: bytes


class _InliningStore(Protocol):
    """A store that leaks payloads three different ways, plus one member that is correct.

    It satisfies every required member of the real port, so structural conformance would
    accept it - the lock has to read the declared surface, not an instance.
    """

    async def put(self, data: bytes, *, kind: MediaKind, mime_type: str) -> _InlinedRef: ...

    async def get_with_ref(self, media_id: MediaId) -> tuple[MediaRef, bytes]: ...

    async def get_many(self, media_ids: tuple[MediaId, ...]) -> dict[MediaId, bytes]: ...

    async def get(self, media_id: MediaId) -> bytes: ...


class _FusedStore(Protocol):
    """The second counterexample: one member, a flag, and the privacy decision buried in a
    keyword argument at whatever call site happened to set it."""

    async def fetch(self, media_id: MediaId, *, as_url: bool = False) -> bytes | str: ...


class _InMemoryStore:
    """A store holding payloads in a dict, keyed by content hash, so nothing here needs a
    disk. It is also the type-level conformance check at `_STORE` below."""

    def __init__(self) -> None:
        self.payloads: dict[MediaId, bytes] = {}

    async def put(
        self, data: bytes, *, kind: MediaKind, mime_type: str, filename: str | None = None
    ) -> MediaRef:
        media_id = MediaId(f"m-{len(self.payloads)}")
        self.payloads[media_id] = data
        return MediaRef(
            media_id=media_id,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256="0" * 64,
            filename=filename,
        )

    async def get(self, media_id: MediaId) -> bytes:
        return self.payloads[media_id]

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> str:
        return f"https://example.invalid/{media_id}?ttl={ttl_seconds}"


_STORE: MediaStore = _InMemoryStore()


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_no_member_hands_a_payload_back_inside_a_domain_type() -> None:
    """A payload is returned as itself or not at all.

    `MediaRef` is the domain's view of a file and it is what a turn record keeps. The day
    it carries the file too, conversation history stops being serialisable and every
    exception trace starts printing megabytes - and no test goes red for either.
    """
    smuggled = sorted(_members_smuggling_a_payload(MediaStore))

    assert smuggled == [], (
        f"MediaStore.{smuggled} returns bytes wrapped in another type. A payload may be "
        "returned as bare `bytes` at the adapter edge and nowhere else: inside a domain "
        "type it travels into the turn record, the conversation history and the logs."
    )

    assert not _carries_bytes(MediaRef), (
        "MediaRef grew a payload field. The domain holds a REFERENCE; resolution happens "
        "at the adapter edge, as late as possible."
    )

    assert _return_hint(_declared_members(MediaStore)["put"]) is MediaRef, (
        "put takes bytes and hands back a reference - that is the whole direction of the "
        "port."
    )


@pytest.mark.phase("F7")
@pytest.mark.silent
def test_signed_url_is_a_separate_member_from_the_bytes_one() -> None:
    """The BYTES-versus-SIGNED_URL choice belongs to `MediaPolicy.delivery`, not to a
    keyword argument.

    A signed URL is handed to the model provider, which fetches the file itself. For
    incident evidence containing personal data that is a decision somebody has to make on
    purpose. Two members keep it visible: a caller that wants the URL has to name it.
    """
    members = _declared_members(MediaStore)

    assert sorted(members) == ["get", "put", "signed_url"], (
        "MediaStore answers one question - where does this binary live and how do I "
        f"retrieve it. Found: {sorted(members)}."
    )

    payload_members = _members_returning_a_payload(MediaStore)
    assert payload_members == frozenset({"get"}), (
        f"exactly one member resolves a payload; found {sorted(payload_members)}"
    )

    assert "signed_url" not in payload_members, (
        "signed_url must be reachable without the bytes path and vice versa; a member "
        "that can answer either way puts the privacy decision in a keyword argument"
    )
    assert members["signed_url"] is not members["get"]

    url_hint = _return_hint(members["signed_url"])
    assert url_hint is str, (
        f"signed_url hands back a URL and nothing else; found {url_hint!r}. A "
        "`bytes | str` return is the fused member wearing two hats."
    )

    signature = inspect.signature(members["signed_url"])
    assert [name for name in signature.parameters if name != "self"] == [
        "media_id",
        "ttl_seconds",
    ]
    assert signature.parameters["ttl_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["ttl_seconds"].default == 300, (
        "the URL is public the moment it is issued, so the default expiry is short and "
        "explicit rather than absent"
    )


@pytest.mark.phase("F7")
def test_the_lock_rejects_a_store_that_inlines_payloads() -> None:
    """A rule that has never rejected anything is not known to reject anything."""
    assert _carries_bytes(_InlinedRef), "a ref carrying a payload field must be flagged"
    assert _carries_bytes(tuple[MediaRef, bytes])
    assert _carries_bytes(dict[MediaId, bytes])

    assert sorted(_members_smuggling_a_payload(_InliningStore)) == [
        "get_many",
        "get_with_ref",
        "put",
    ]
    assert "get" not in _members_smuggling_a_payload(_InliningStore), (
        "returning bare bytes at the adapter edge is the port working as designed"
    )


@pytest.mark.phase("F7")
def test_the_lock_rejects_a_store_that_fuses_the_two_deliveries() -> None:
    """One member answering both ways hides the privacy decision in an argument."""
    fused = _declared_members(_FusedStore)["fetch"]

    assert _carries_bytes(_return_hint(fused))
    assert str in get_args(_return_hint(fused)), (
        "the fused member is the defect: it can hand back a payload OR a provider-"
        "reachable URL, and which one it did is decided at the call site"
    )
    assert "signed_url" not in _declared_members(_FusedStore)


@pytest.mark.phase("F7")
def test_every_member_is_awaitable() -> None:
    """D13: all three are I/O - a write to storage, a read from it, and a signature issued
    by the storage backend. A sync `def` here blocks the event loop for the whole of it
    and stalls every other turn in the process."""
    for name, member in sorted(_declared_members(MediaStore).items()):
        assert inspect.iscoroutinefunction(member), (
            f"MediaStore.{name} is sync; ports/ is async end to end (D13). Storage is "
            "network or disk, and blocking on it here stalls every concurrent turn."
        )


@pytest.mark.phase("F7")
def test_a_store_resolves_a_ref_it_issued() -> None:
    """The round trip the port exists for, with the payload never entering the ref."""
    store = _InMemoryStore()

    ref = asyncio.run(store.put(b"\x89PNG\r\n\x1a\n", kind=MediaKind.IMAGE, mime_type="image/png"))

    assert ref.size_bytes == 8
    assert not _carries_bytes(type(ref))
    assert asyncio.run(store.get(ref.media_id)) == b"\x89PNG\r\n\x1a\n"

    url = asyncio.run(_STORE.signed_url(ref.media_id, ttl_seconds=60))
    assert isinstance(url, str)
