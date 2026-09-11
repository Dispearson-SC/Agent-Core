"""`MediaStore` resolves a reference to a payload, and it never hands a payload back
inside a domain type.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-02, docs/TASKS.md#t-f7-10

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

    3. EVERY STAND-IN FOR THE STORE ANSWERS IN THE PORT'S TYPE. `t-f7-10` widened
       `signed_url` from a bare `str` to `SignedMedia`, because a content-addressed path
       carries no extension and the store is the last participant that still knows
       `MediaRef.kind` and `MediaRef.mime_type`. The port moved; the fakes standing in for
       it around the suite did not, and a fake still promising `str` is a caller typed
       against the OLD contract - it compiles nowhere and it tells every test using it
       that the type survives the trip when it does not. This is the third wave in a row
       to end that way (docs/WAVES.md rule 4), so the sweep below is a guard rather than a
       one-off repair: it reads every class under `Core/tests/` that declares
       `signed_url`, not a list of the four that happened to be wrong today.

    Both locks are applied to a deliberate counterexample as well, because a rule that has
    never rejected anything is not known to reject anything.

    The port is checked structurally, from `ports/` alone: a test that has to import a
    concrete adapter to verify a contract is testing the adapter. `_InMemoryStore` below
    is the type-level half - mypy proves it satisfies the port at the annotated
    assignment, so the port cannot drift from the shape asserted here without the
    project-wide mypy run failing too.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, get_args, get_type_hints

import pytest

from agent_core.domain.media import MediaId, MediaKind, MediaRef
from agent_core.ports.media_store import MediaStore, SignedMedia

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


def _carries_url_and_type(annotation: object) -> bool:
    """Does this return annotation hand back BOTH the URL and what it points at?

    Structural, like every other lock here: it must not care what the type is called, only
    that a caller receives a URL to hand to the provider and a `MediaRef` to type it from.
    """
    if not (isinstance(annotation, type) and dataclasses.is_dataclass(annotation)):
        return False
    hints = get_type_hints(annotation)
    return str in hints.values() and MediaRef in hints.values()


TESTS_ROOT = Path(__file__).resolve().parents[1]

# The vocabulary a return annotation may be written in. Anything else resolves to None and
# is reported by name, which is the honest answer: an unrecognised type is not evidence
# that the pair survived.
_ANNOTATION_NAMES: dict[str, object] = {
    "str": str,
    "bytes": bytes,
    "MediaRef": MediaRef,
    "SignedMedia": SignedMedia,
}

# Stores that exist TO BE REJECTED. `_PreWideningStore` is the exact signature t-f7-10
# replaced, kept so the lock in tests/unit/test_media_signed_url.py has something to fail
# on; exempting it here is why `test_the_exempted_counterexample_is_still_the_old_shape`
# asserts it is still bare. An exemption nobody checks becomes a hiding place.
_COUNTEREXAMPLES: frozenset[tuple[str, str]] = frozenset(
    {("unit/test_media_signed_url.py", "_PreWideningStore")}
)


def _stand_in_stores() -> list[tuple[str, str, str]]:
    """Every class under `Core/tests/` declaring `signed_url`, with its return annotation.

    Read from source rather than by importing each module: a sweep that imports the whole
    suite to look at one annotation inherits every module's import cost and every module's
    reason to be skipped, and the shape being checked is visible in the text.
    """
    found: list[tuple[str, str, str]] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for member in node.body:
                if not isinstance(member, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                if member.name != "signed_url":
                    continue
                returns = "" if member.returns is None else ast.unparse(member.returns)
                found.append((path.relative_to(TESTS_ROOT).as_posix(), node.name, returns))
    return found


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
        self.refs: dict[MediaId, MediaRef] = {}

    async def put(
        self, data: bytes, *, kind: MediaKind, mime_type: str, filename: str | None = None
    ) -> MediaRef:
        media_id = MediaId(f"m-{len(self.payloads)}")
        self.payloads[media_id] = data
        ref = MediaRef(
            media_id=media_id,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256="0" * 64,
            filename=filename,
        )
        self.refs[media_id] = ref
        return ref

    async def get(self, media_id: MediaId) -> bytes:
        return self.payloads[media_id]

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> SignedMedia:
        """The ref travels WITH the URL (t-f7-10), and it is the one the store already
        held - never one reconstructed from the URL, which would be a guess."""
        return SignedMedia(
            url=f"https://example.invalid/{media_id}?ttl={ttl_seconds}",
            ref=self.refs[media_id],
        )


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
    assert _carries_url_and_type(url_hint), (
        f"signed_url hands back a URL and what it points at; found {url_hint!r}. A "
        "`bytes | str` return is the fused member wearing two hats. This assertion read "
        "`is str` until t-f7-10 widened the port: the URL alone lost MediaRef.kind, and a "
        "content-addressed path has nothing to read a type off."
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
@pytest.mark.silent
def test_every_media_store_stand_in_answers_with_the_url_and_its_type() -> None:
    """A fake still promising a bare `str` is a caller typed against the old contract.

    `t-f7-10` widened the port because a signed path is content-addressed and therefore
    carries no extension, so `MediaRef.kind` decides `ImageUrl` versus `DocumentUrl` and
    the store is the last place that still knows it. A fake that returns the string alone
    asserts the opposite - that the type survives - and it asserts it in every test that
    uses the fake, silently, because nothing in those tests ever looks at the return value.
    """
    stand_ins = [entry for entry in _stand_in_stores() if entry[:2] not in _COUNTEREXAMPLES]
    assert stand_ins, "the sweep found no stand-in stores at all; it has stopped reading"

    bare = [
        f"{path}::{class_name} -> {returns or 'no annotation'}"
        for path, class_name, returns in stand_ins
        if not _carries_url_and_type(_ANNOTATION_NAMES.get(returns))
    ]

    assert bare == [], (
        "these stand-ins for MediaStore still answer signed_url with something that is "
        f"not the URL and the MediaRef together: {bare}. The port hands back both "
        "(docs/TASKS.md#t-f7-10); a fake that hands back a bare string types its callers "
        "against the contract that lost the media kind."
    )


@pytest.mark.phase("F7")
def test_the_exempted_counterexample_is_still_the_old_shape() -> None:
    """The one exemption above is a deliberate counterexample, not a fake left behind.

    If it ever becomes a real stand-in, the exemption turns into a hiding place for the
    exact defect the sweep exists to catch - so the sweep has to keep finding it, and it
    has to keep finding it bare.
    """
    exempted = {
        (path, class_name): returns
        for path, class_name, returns in _stand_in_stores()
        if (path, class_name) in _COUNTEREXAMPLES
    }

    assert sorted(exempted) == sorted(_COUNTEREXAMPLES), (
        f"the exemption list names something the sweep no longer finds: {sorted(exempted)}"
    )
    for location, returns in exempted.items():
        assert not _carries_url_and_type(_ANNOTATION_NAMES.get(returns)), (
            f"{location} is exempted for being the pre-widening signature and is no longer "
            "pre-widening; delete the exemption rather than widening the counterexample"
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

    signed = asyncio.run(store.signed_url(ref.media_id, ttl_seconds=60))
    assert isinstance(signed.url, str)
    assert signed.ref == ref, "the URL arrives with the reference it was issued for"


# --------------------------------------------------------------------------------------
# 4. There is ONE fake for this port, and it lives with the other shared fakes.
#
# `docs/TASKS.md#t-f0-01` built `tests/fakes/ports.py` so tests stopped hand-rolling their
# own mocks; `FakeMediaStore` never got past its TODO at the bottom of that file. So
# `IngestMedia`'s test, the evidence route's test, `ResumeTurn`'s test and this file's own
# `_InMemoryStore` above each grew a look-alike instead - FOUR separate stand-ins, all
# implementing `put`/`get`/`signed_url` slightly differently. When `signed_url` widened
# from `str` to `SignedMedia` (`t-f7-10`), every one of them had to be found and fixed by
# hand, separately, which is exactly the collateral docs/WAVES.md rule 4 and
# docs/STATE.md's "recurring process defect" section both name. This sweep is the guard
# that keeps a fifth one from ever being worth writing: it fails for as long as more than
# one concrete stand-in exists anywhere under `Core/tests/`, and it only stops failing once
# there is exactly one, living in `tests/fakes/ports.py`, named `FakeMediaStore`.

_MEDIA_STORE_MEMBERS = frozenset({"put", "get", "signed_url"})

# The one place a concrete stand-in is allowed to live, and the name every other shared
# fake in that file already follows (`FakeAgentRunner`, `FakeToolProvider`, ...).
_SHARED_FAKES_MODULE = "fakes/ports.py"
_SHARED_FAKE_NAME = "FakeMediaStore"

# `_InMemoryStore` above is a TYPE-LEVEL conformance target for the port lock
# (`_STORE: MediaStore = _InMemoryStore()`), not a second fake any other module could
# import instead of `tests.fakes.ports.FakeMediaStore` - nothing outside this module ever
# constructs it, and its job is to make mypy fail the day this port drifts from the shape
# asserted here, not to record calls for some other test to assert against. Exempted BY
# NAME rather than silently: a nameless exemption is a hiding place for the very defect
# this sweep exists to catch, the day this class stops being what its docstring claims.
_STRUCTURAL_CONFORMANCE_TARGETS: frozenset[tuple[str, str]] = frozenset(
    {("unit/test_ports_media_store.py", "_InMemoryStore")}
)


def _is_protocol_class(node: ast.ClassDef) -> bool:
    """True for a `Protocol` shape - `_InliningStore` and `_FusedStore` above, and
    `_PreWideningStore` in tests/unit/test_media_signed_url.py. Those exist to be
    rejected by a lock, never to be constructed and called the way a fake is; counting
    them here would flag the very counterexamples the port lock needs to keep failing."""
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        if name == "Protocol":
            return True
    return False


def _media_store_stand_ins() -> list[tuple[str, str]]:
    """Every CONCRETE class under `Core/tests/` implementing the whole port - all three
    of `put`, `get` and `signed_url` together, not one member reused for an unrelated
    purpose.

    Read from source, exactly like `_stand_in_stores()` above and for the same reason:
    this test must fail on its assertion, never on collection, while the module it wants
    (`tests.fakes.ports.FakeMediaStore`) does not exist yet.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or _is_protocol_class(node):
                continue
            rel_path = path.relative_to(TESTS_ROOT).as_posix()
            if (rel_path, node.name) in _STRUCTURAL_CONFORMANCE_TARGETS:
                continue
            method_names = {
                member.name
                for member in node.body
                if isinstance(member, ast.AsyncFunctionDef | ast.FunctionDef)
            }
            if _MEDIA_STORE_MEMBERS <= method_names:
                found.append((rel_path, node.name))
    return found


@pytest.mark.phase("F7")
def test_exactly_one_media_store_fake_exists_and_it_lives_with_the_shared_fakes() -> None:
    """One fake, in `tests/fakes/ports.py`, beside `FakeAgentRunner`, `FakeToolProvider`,
    `FakeConversationStore`, `FakeAuditSink` and `FakeModelGateway` - not a look-alike
    grown wherever a test module happened to need one.

    This is `t-f0-01`'s own argument turned into an assertion: "a fake that records calls
    is worth more than a mock that asserts them", and that is only true once there is ONE
    of it. Four independent stand-ins are four independent chances to diverge, and they
    already have - this sweep is what stops a fifth from ever looking like the easy
    option again.
    """
    stand_ins = _media_store_stand_ins()
    assert stand_ins, "the sweep found no MediaStore stand-in at all; it has stopped reading"

    assert len(stand_ins) == 1, (
        f"found {len(stand_ins)} classes each implementing the whole MediaStore port on "
        f"their own: {stand_ins}. `Core/tests/fakes/ports.py` still lists `FakeMediaStore` "
        "as a t-f7 TODO, so every caller keeps hand-rolling its own instead of importing "
        "the one that should exist - exactly what t-f0-01 was written to prevent, and "
        "exactly why `signed_url`'s widening (t-f7-10) had to be fixed four times by hand."
    )

    ((path, class_name),) = stand_ins
    assert path == _SHARED_FAKES_MODULE, (
        f"the one MediaStore stand-in lives at tests/{path}::{class_name}, not "
        f"tests/{_SHARED_FAKES_MODULE}. Every other shared fake lives there; until this "
        "one does too, nothing stops a fifth module from growing a fifth copy instead of "
        "importing it."
    )
    assert class_name == _SHARED_FAKE_NAME, (
        f"the one MediaStore stand-in is named {class_name!r}; every shared fake in "
        f"tests/{_SHARED_FAKES_MODULE} is named `Fake<Port>`, so this one should be "
        f"{_SHARED_FAKE_NAME!r}."
    )
