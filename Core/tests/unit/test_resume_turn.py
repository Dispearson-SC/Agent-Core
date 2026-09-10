"""ResumeTurn - the properties a resumed turn must guarantee.

Phase:   F3 (approvals) / F7 (evidence, same code path)
Tasks:   docs/TASKS.md#t-f3-02

Fakes only: no database, no network, no model. If this module ever needs one, a port is
leaking (tests/conftest.py says the same thing).

The properties, and why each is here rather than left to an integration test:

1. IDEMPOTENCY PER (turn_id, tool_call_id). This use case is awaited inside a DBOS step
   and a step is replayed after a crash. Running the tool twice freezes the account
   twice, and nothing downstream can tell the two apart. A second resume of the SAME
   pair must return the stored outcome and touch neither the runner nor the store.
2. THE tool_call_id ROUND-TRIPS VERBATIM. CLAUDE.md non-negotiable #5 and the silent-bug
   table: a regenerated id is dropped by Pydantic AI WITHOUT an exception and the agent
   asks the same question forever. Nothing fails; the turn just never ends.
3. A STILL-PENDING call on the same turn resumes normally. Idempotency keys on the pair,
   not on the turn - keying on the turn alone would swallow the second approval of a
   two-approval turn.
4. EVIDENCE IS RESOLVED THROUGH `MediaStore` PER THE PROFILE'S DELIVERY MODE. BYTES is
   the default and a signed URL is opt-in, because a signed URL hands the file to the
   model provider (docs/DECISIONS.md#d9, domain/media.py).
5. AN UNKNOWN profile_id RAISES BEFORE ANYTHING IS WRITTEN, exactly as in `StartTurn`.

`FakeMediaStore` is declared here rather than in tests/fakes/ports.py because
`MediaStore` lands in F7 and that shared file still lists it as TODO; this task does not
own that file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agent_core.application.resume_turn import (
    ResolvedTool,
    ResumeTurn,
    UnknownProfileError,
)
from agent_core.domain.media import MediaDelivery, MediaId, MediaKind, MediaPolicy, MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnResult,
)
from agent_core.ports.agent_runner import ToolResolution
from tests.fakes.ports import FakeAgentRunner, FakeAuditSink, FakeConversationStore

TURN = TurnId("turn-1")
CALL_A = ToolCallId("pyd_ai_call_a")
CALL_B = ToolCallId("pyd_ai_call_b")

PROFILE = AgentProfile(id="fraud_triage", persona="You triage fraud.", model="m")
SIGNED_URL_PROFILE = AgentProfile(
    id="fraud_triage_urls",
    persona="You triage fraud.",
    model="m",
    media=MediaPolicy(
        accepted_kinds=frozenset({MediaKind.IMAGE}),
        delivery=MediaDelivery.SIGNED_URL,
        allow_evidence_requests=True,
    ),
)

EVIDENCE = MediaRef(
    media_id=MediaId("m-1"),
    kind=MediaKind.IMAGE,
    mime_type="image/png",
    size_bytes=11,
    sha256="0" * 64,
)


class FakeMediaStore:
    """A dict keyed by `MediaId`, recording which resolution path was taken.

    Recording both call lists separately is the point: a store that returned bytes when
    the policy asked for a signed URL - or the reverse - would still satisfy an assertion
    on the payload alone if the fake produced the same value for both.
    """

    def __init__(self, blobs: dict[MediaId, bytes]) -> None:
        self._blobs = blobs
        self.get_calls: list[MediaId] = []
        self.signed_url_calls: list[MediaId] = []

    async def put(
        self, data: bytes, *, kind: MediaKind, mime_type: str, filename: str | None = None
    ) -> MediaRef:
        raise NotImplementedError("ResumeTurn never stores media; it only resolves it.")

    async def get(self, media_id: MediaId) -> bytes:
        self.get_calls.append(media_id)
        return self._blobs[media_id]

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> str:
        self.signed_url_calls.append(media_id)
        return f"https://example.invalid/{media_id}?ttl={ttl_seconds}"


@dataclass(frozen=True, slots=True)
class Wiring:
    use_case: ResumeTurn
    runner: FakeAgentRunner
    store: FakeConversationStore
    audit: FakeAuditSink
    media: FakeMediaStore


def _finished(text: str) -> TurnOutcome:
    return TurnOutcome(turn_id=TURN, result=TurnResult(text=text))


def _wire(
    outcome: TurnOutcome | None = None,
    profiles: tuple[AgentProfile, ...] = (PROFILE,),
) -> Wiring:
    runner = FakeAgentRunner(outcome or _finished("resumed"))
    store = FakeConversationStore()
    audit = FakeAuditSink()
    media = FakeMediaStore({MediaId("m-1"): b"hello world"})
    return Wiring(
        use_case=ResumeTurn(
            runner=runner,
            store=store,
            audit=audit,
            media=media,
            profiles={p.id: p for p in profiles},
        ),
        runner=runner,
        store=store,
        audit=audit,
        media=media,
    )


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_resuming_twice_with_the_same_pair_returns_the_stored_outcome_and_reruns_nothing(
    session: SessionRef,
) -> None:
    """THE anchor property of t-f3-02.

    A DBOS step is replayed after a crash, so `execute` is called again with the identical
    `(turn_id, tool_call_id)`. The second call must be a no-op that hands back what the
    first call produced. If it reaches the runner, the approved tool runs a second time -
    and the account is frozen twice with one human approval on record.
    """
    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)

    first = asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,)))
    second = asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,)))

    assert second is first, "the second resume must return the STORED outcome, not a new run"
    assert len(wiring.runner.resume_calls) == 1, "the runner must not be re-entered"
    assert len(wiring.store.outcomes) == 1, "the outcome must not be appended twice"


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_the_tool_call_id_reaches_the_runner_verbatim(session: SessionRef) -> None:
    """CLAUDE.md non-negotiable #5. A regenerated, normalised or re-encoded id is dropped
    silently and the agent asks forever - there is no exception to catch."""
    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=False, payload="Not this month.")

    asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,)))

    (_, _, _, sent) = wiring.runner.resume_calls[0]
    assert tuple(r.tool_call_id for r in sent) == (CALL_A,)
    assert sent[0].tool_call_id == "pyd_ai_call_a"
    assert sent[0].approved is False
    assert sent[0].payload == "Not this month.", "a refusal must carry the human's reason"


@pytest.mark.phase("F3")
def test_a_different_pending_call_on_the_same_turn_still_resumes(session: SessionRef) -> None:
    """Idempotency keys on the PAIR. Keying on the turn alone would swallow the second
    approval of a turn that suspended on two tools."""
    wiring = _wire()
    first = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)
    second = ResolvedTool(tool_call_id=CALL_B, approved=True, payload=None)

    asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (first,)))
    asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (second,)))

    assert len(wiring.runner.resume_calls) == 2
    assert [r[3][0].tool_call_id for r in wiring.runner.resume_calls] == [CALL_A, CALL_B]


@pytest.mark.phase("F3")
def test_an_already_resolved_pair_is_dropped_from_a_mixed_batch(session: SessionRef) -> None:
    """A retry that re-sends one answered id alongside a fresh one must resume with the
    fresh one ONLY. Passing the answered id again re-runs its tool."""
    wiring = _wire()
    first = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)
    second = ResolvedTool(tool_call_id=CALL_B, approved=True, payload=None)

    asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (first,)))
    asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (first, second)))

    assert [r[3][0].tool_call_id for r in wiring.runner.resume_calls] == [CALL_A, CALL_B]
    assert len(wiring.runner.resume_calls[1][3]) == 1


@pytest.mark.phase("F7")
def test_evidence_is_delivered_as_bytes_by_default(session: SessionRef) -> None:
    """BYTES is the default because a signed URL hands the file to the model provider."""
    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=EVIDENCE)

    asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,)))

    (_, _, _, sent) = wiring.runner.resume_calls[0]
    assert sent[0].payload == b"hello world"
    assert wiring.media.get_calls == [MediaId("m-1")]
    assert wiring.media.signed_url_calls == []
    assert sent[0].tool_call_id == CALL_A, "resolving the payload must not disturb the id"


@pytest.mark.phase("F7")
def test_evidence_becomes_a_signed_url_only_when_the_profile_says_so(
    session: SessionRef,
) -> None:
    wiring = _wire(profiles=(SIGNED_URL_PROFILE,))
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=EVIDENCE)

    asyncio.run(
        wiring.use_case.execute(TURN, session, SIGNED_URL_PROFILE.id, (resolution,))
    )

    (_, _, _, sent) = wiring.runner.resume_calls[0]
    assert wiring.media.signed_url_calls == [MediaId("m-1")]
    assert wiring.media.get_calls == []
    assert isinstance(sent[0].payload, str)
    assert sent[0].payload.startswith("https://example.invalid/m-1")


@pytest.mark.phase("F3")
def test_an_unknown_profile_raises_before_anything_is_written(session: SessionRef) -> None:
    """Same rule as `StartTurn`: refusing to fall back to a default profile is what stops
    a typo resuming a turn with someone else's permissions."""
    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)

    with pytest.raises(UnknownProfileError):
        asyncio.run(wiring.use_case.execute(TURN, session, "no_such_profile", (resolution,)))

    assert wiring.runner.resume_calls == []
    assert wiring.store.outcomes == []


@pytest.mark.phase("F3")
def test_a_suspended_outcome_is_persisted_and_returned_unchanged(session: SessionRef) -> None:
    """Resuming may suspend AGAIN - an approval unlocks a tool that then asks for
    evidence. That is normal, and this use case must not loop on it."""
    from agent_core.domain.turn import PendingKind, PendingRequest

    again = TurnOutcome(
        turn_id=TURN,
        pending=(
            PendingRequest(
                kind=PendingKind.EVIDENCE,
                tool_call_id=CALL_B,
                tool_name="request_evidence",
                arguments={},
                reason="Send a photo of the receipt.",
            ),
        ),
    )
    wiring = _wire(outcome=again)
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)

    outcome = asyncio.run(wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,)))

    assert outcome is again
    assert wiring.store.outcomes == [(TURN, again)]


@pytest.mark.phase("F3")
def test_resolved_tool_satisfies_the_port_protocol() -> None:
    """`ResolvedTool` is what the use case hands to `AgentRunner.resume`, so it has to be
    a `ToolResolution`. Checked here rather than trusted, because a structural mismatch
    surfaces as a type error in a phase nobody is looking at."""
    resolution: ToolResolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)
    assert resolution.tool_call_id == CALL_A
