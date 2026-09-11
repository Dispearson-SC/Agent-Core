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
6. THE CALLER WHOSE POLICY WILL BE ENFORCED TRAVELS, FROM THE WORKFLOW STEP DOWN
   (docs/TASKS.md#t-f3-16). A resume is not one tool call: the call a human authorised is
   the FIRST of them, and the model may ask for more on the same continuation. Enforcing
   anything about those later calls needs the identity `ToolPolicy.load_rules` takes, so
   `AgentRunner.resume` grew a `caller` seat - and every layer above it has to be able to
   fill it, or the widening bought nothing. `execute` therefore has a `CallerIdentity`
   seat of its own, and `_step_resume` supplies `request.caller`.

`FakeMediaStore` is imported from tests/fakes/ports.py - the one shared stand-in for the
port (docs/TASKS.md#t-f7-02). `ContinuingRunner` stays local for a different reason: it is
not a stand-in for the port at large, it is the ONE runner behaviour this module has to
see - a second tool call on a resumed continuation.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import cast

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.resume_turn import (
    ResolvedTool,
    ResumeTurn,
    UnknownProfileError,
)
from agent_core.application.start_turn import StartTurn
from agent_core.domain.media import (
    MediaDelivery,
    MediaId,
    MediaKind,
    MediaPolicy,
    MediaRef,
)
from agent_core.domain.policy import Effect, PolicyDecision, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)
from agent_core.ports.agent_runner import ToolResolution
from agent_core.ports.media_store import SignedMedia
from tests.fakes.ports import (
    FakeAgentRunner,
    FakeAuditSink,
    FakeConversationStore,
    FakeMediaStore,
    FakeToolPolicy,
)

TURN = TurnId("turn-1")
OTHER_TURN = TurnId("turn-2")
CALL_A = ToolCallId("pyd_ai_call_a")
CALL_B = ToolCallId("pyd_ai_call_b")

# WHOSE policy a resumed continuation is judged by: the identity the TURN belongs to, the
# one `TurnRequest` has carried since the turn started. Never the human who answered the
# pending call - CLAUDE.md non-negotiable #9 forbids deriving one identity from another,
# and the approver is a different person with different rules. The tenant matches the
# `session` fixture's, because a caller and a session that disagree about the tenant are a
# bug no assertion in this module would otherwise name.
TURN_CALLER = CallerIdentity(
    subject_id="u-7",
    channel="telegram",
    tenant_id=TenantId("t-1"),
    roles=frozenset({"operator"}),
)

# The tool the model asks for AFTER the resume. A second name is the whole point: the first
# call is the one a human authorised, and this is the one that used to run with no policy
# check and no audit row because `resume` could not be handed a caller.
AFTER_RESUME_TOOL = "freeze_account"
DENY_RULE = PolicyRule(
    rule_id="r-freeze-deny",
    tool_pattern=AFTER_RESUME_TOOL,
    effect=Effect.DENY,
    reason="freezing an account is never automatic",
)

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


class ContinuingRunner:
    """A runner whose resumed continuation makes ONE MORE tool call, and polices it.

    It does exactly what `PydanticAgentRunner`'s `before_tool_execute` hook does for that
    second call - load the caller's rules, decide, write the audit row BEFORE any side
    effect - and it does it with the `caller` and `session` the PORT handed it and nothing
    else. That is what makes this a test of the plumbing rather than of the double: a
    `caller` that arrived wrong, widened from somebody else, or synthesised inside the
    adapter shows up as a rule set loaded for the wrong identity and an audit row naming
    it. `tests/unit/test_runner_resume_identity.py` proves the real adapter behaves this
    way against a real Pydantic AI run; this module proves the identity reaches it.

    No Pydantic AI here on purpose - `tests/conftest.py` and this module's docstring: a use
    case's tests run on fakes only.
    """

    def __init__(
        self, outcome: TurnOutcome, *, policy: FakeToolPolicy, audit: FakeAuditSink
    ) -> None:
        self.outcome = outcome
        self._policy = policy
        self._audit = audit
        self.resume_calls: list[
            tuple[TurnId, AgentProfile, object, tuple[ToolResolution, ...]]
        ] = []
        self.callers: list[CallerIdentity] = []
        self.sessions: list[SessionRef] = []

    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:  # pragma: no cover - this double only ever resumes
        raise NotImplementedError("ContinuingRunner exists to resume, not to start a turn.")

    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
        *,
        caller: CallerIdentity,
        session: SessionRef,
    ) -> TurnOutcome:
        self.resume_calls.append((turn_id, profile, history, resolutions))
        self.callers.append(caller)
        self.sessions.append(session)

        rules = await self._policy.load_rules(caller)
        decision = self._policy.decide(rules, AFTER_RESUME_TOOL, {})
        await self._audit.record_tool_call(turn_id, caller, AFTER_RESUME_TOOL, {}, decision)
        return self.outcome


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
    media = FakeMediaStore({MediaId("m-1"): b"hello world"}, {MediaId("m-1"): EVIDENCE})
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


def _turn_request(session: SessionRef) -> TurnRequest:
    """The request the suspended turn started from. `_step_resume` reads its `caller`."""
    return TurnRequest(
        session=session,
        caller=TURN_CALLER,
        profile_id=PROFILE.id,
        input=UserInput(text="Freeze the account behind order 4417."),
    )


def _bind(monkeypatch: pytest.MonkeyPatch, use_case: ResumeTurn) -> None:
    """Bind the workflow's collaborators for one test, and only for one test.

    `monkeypatch` rather than `bind_dependencies`, because that setter writes a module
    global with no way back: a unit test that called it would leave every later test in the
    process wired to this module's fakes.

    `start_turn` is cast from None deliberately. `_step_resume` resolves exactly one field
    and a real `StartTurn` here would be eight collaborators built to be never called -
    which reads as though the step used them.
    """
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast(StartTurn, None), resume_turn=use_case
        ),
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

    first = asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,), caller=TURN_CALLER)
    )
    second = asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,), caller=TURN_CALLER)
    )

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

    asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,), caller=TURN_CALLER)
    )

    (_, _, _, sent, _, _) = wiring.runner.resume_calls[0]
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

    asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (first,), caller=TURN_CALLER)
    )
    asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (second,), caller=TURN_CALLER)
    )

    assert len(wiring.runner.resume_calls) == 2
    assert [r[3][0].tool_call_id for r in wiring.runner.resume_calls] == [CALL_A, CALL_B]


@pytest.mark.phase("F3")
def test_an_already_resolved_pair_is_dropped_from_a_mixed_batch(session: SessionRef) -> None:
    """A retry that re-sends one answered id alongside a fresh one must resume with the
    fresh one ONLY. Passing the answered id again re-runs its tool."""
    wiring = _wire()
    first = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)
    second = ResolvedTool(tool_call_id=CALL_B, approved=True, payload=None)

    asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (first,), caller=TURN_CALLER)
    )
    asyncio.run(
        wiring.use_case.execute(
            TURN, session, PROFILE.id, (first, second), caller=TURN_CALLER
        )
    )

    assert [r[3][0].tool_call_id for r in wiring.runner.resume_calls] == [CALL_A, CALL_B]
    assert len(wiring.runner.resume_calls[1][3]) == 1


@pytest.mark.phase("F7")
def test_evidence_is_delivered_as_bytes_by_default(session: SessionRef) -> None:
    """BYTES is the default because a signed URL hands the file to the model provider."""
    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=EVIDENCE)

    asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,), caller=TURN_CALLER)
    )

    (_, _, _, sent, _, _) = wiring.runner.resume_calls[0]
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
        wiring.use_case.execute(
            TURN, session, SIGNED_URL_PROFILE.id, (resolution,), caller=TURN_CALLER
        )
    )

    (_, _, _, sent, _, _) = wiring.runner.resume_calls[0]
    assert wiring.media.signed_url_calls == [MediaId("m-1")]
    assert wiring.media.get_calls == []
    # A `SignedMedia`, not a bare string (t-f7-10): the URL alone loses `kind` at the one
    # point in the system that still has it, and the runner needs it to type what the
    # provider is handed. What must NOT survive is the `MediaRef` as the payload - handing
    # the model a pointer is the bug this branch exists to resolve.
    assert isinstance(sent[0].payload, SignedMedia)
    assert sent[0].payload.url.startswith("https://example.invalid/m-1")
    assert sent[0].payload.ref is EVIDENCE


@pytest.mark.phase("F3")
def test_an_unknown_profile_raises_before_anything_is_written(session: SessionRef) -> None:
    """Same rule as `StartTurn`: refusing to fall back to a default profile is what stops
    a typo resuming a turn with someone else's permissions."""
    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)

    with pytest.raises(UnknownProfileError):
        asyncio.run(
            wiring.use_case.execute(
                TURN, session, "no_such_profile", (resolution,), caller=TURN_CALLER
            )
        )

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

    outcome = asyncio.run(
        wiring.use_case.execute(TURN, session, PROFILE.id, (resolution,), caller=TURN_CALLER)
    )

    assert outcome is again
    assert wiring.store.outcomes == [(TURN, again)]


# --------------------------------------------------------------------------------------
# t-f3-16 - the identity a resumed continuation is judged by, from the workflow step down


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_no_layer_may_resume_without_the_caller_whose_policy_will_be_enforced(
    session: SessionRef,
) -> None:
    """The negative case is the PRE-widening call shape, per docs/TASKS.md#t-f1-05.

    A lock that only checked the new shape would accept the old one too: `execute` would
    keep working with no caller, and the seat `AgentRunner.resume` grew would be filled
    with whatever the use case could invent. So what is asserted is that the old four-
    argument call is REFUSED, and that the seat is keyword-only with no default - a default
    is how a required identity quietly becomes an optional one, and the tool call it fails
    to police raises nothing.

    KEYWORD-ONLY for the reason the port states: added positionally, an un-updated call
    site would bind `profile_id` or `resolutions` to `caller` and fail far from the
    mistake.
    """
    parameters = inspect.signature(ResumeTurn.execute).parameters
    assert "caller" in parameters, (
        "ResumeTurn.execute has no CallerIdentity seat, so a resumed continuation cannot "
        "load the rules it must enforce and every tool call after the resume runs "
        "unwatched (docs/TASKS.md#t-f3-16)"
    )
    seat = parameters["caller"]
    assert seat.kind is inspect.Parameter.KEYWORD_ONLY
    assert seat.default is inspect.Parameter.empty, (
        "a defaulted caller is an unenforced one: the policy would be loaded for a "
        "stand-in identity and the audit row would name it"
    )

    wiring = _wire()
    resolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)

    with pytest.raises(TypeError):
        asyncio.run(
            wiring.use_case.execute(  # type: ignore[call-arg]
                TURN, session, PROFILE.id, (resolution,)
            )
        )

    assert wiring.runner.resume_calls == [], "a refused call must not reach the runner"


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_a_tool_call_made_after_a_resume_is_policed_and_audited_from_the_step_down(
    monkeypatch: pytest.MonkeyPatch, session: SessionRef
) -> None:
    """THE anchor of t-f3-16's remaining half, and the only assertion that can see it.

    SILENT-BUG AREA (CLAUDE.md: policy engine, audit ordering). The human approved one
    call; the model then asks for `freeze_account`, which policy DENIES. Before the seats
    existed, that second call ran with no rule loaded and no row written, and every test in
    the suite still passed - they all resolve exactly one call and stop.

    The chain is asserted END TO END, from the DBOS step down, because each link fails on
    its own: a step that supplies no caller, a use case with nowhere to put one, or a use
    case that drops it on the way to the port. And the identity is `request.caller` - the
    caller the TURN belongs to - never the human who answered (non-negotiable #9).
    """
    policy = FakeToolPolicy(
        RuleSet.for_caller(TURN_CALLER, (DENY_RULE,), default_effect=Effect.ALLOW)
    )
    audit = FakeAuditSink()
    runner = ContinuingRunner(
        _finished("I cannot freeze that account."), policy=policy, audit=audit
    )
    use_case = ResumeTurn(
        runner=runner,
        store=FakeConversationStore(),
        audit=audit,
        media=FakeMediaStore({}),
        profiles={PROFILE.id: PROFILE},
    )
    request = _turn_request(session)
    _bind(monkeypatch, use_case)

    outcome = asyncio.run(
        turn_workflow._step_resume(
            TURN,
            request,
            turn_workflow.HumanAnswer(
                turn_id=TURN, tool_call_id=CALL_A, approved=True, note=None
            ),
        )
    )

    assert runner.callers == [request.caller], (
        "the resumed continuation was handed a different identity than the turn's own: "
        "the policy enforced after a resume must be the policy of whoever the turn "
        "belongs to (docs/TASKS.md#t-f3-16, CLAUDE.md non-negotiable #9)"
    )
    assert runner.sessions == [request.session], (
        "without the session the continuation is never compacted, and an approval loop "
        "grows until the provider refuses the whole conversation"
    )
    assert policy.load_rules_calls == [request.caller], (
        "no rules were loaded for the resumed continuation, so the tool call after the "
        "resume was never policed"
    )

    rows = [call for call in audit.calls if call.kind == "tool_call"]
    assert [row.payload[2] for row in rows] == [AFTER_RESUME_TOOL], (
        "a tool call made after a resume left no audit row - CLAUDE.md non-negotiable #6"
    )
    audited_turn, audited_caller, _name, _arguments, decision = rows[0].payload
    assert audited_turn == TURN, "the row must be filed under the turn being resumed"
    assert audited_caller == request.caller, (
        "the row must name the caller whose rules were loaded, not a widened stand-in"
    )
    assert isinstance(decision, PolicyDecision)
    assert decision.effect is Effect.DENY
    assert decision.rule_id == DENY_RULE.rule_id

    assert outcome is runner.outcome, "the step must return what the use case produced"


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_a_payload_that_is_not_this_turns_answer_is_refused_instead_of_applied(
    monkeypatch: pytest.MonkeyPatch, session: SessionRef
) -> None:
    """`HumanAnswer` carries its own `turn_id` so that this check can exist; so use it.

    `DBOS.recv_async` hands the step whatever was sent to this workflow, and the topic is
    shared - a peer's reply to `ask_peer` arrives on it too (docs/TASKS.md#t-f9-04). A step
    that applied whatever arrived would resume a turn on an answer given for another one:
    the approval-without-an-approver the correlation table exists to prevent, and nothing
    raises afterwards.
    """
    wiring = _wire()
    _bind(monkeypatch, wiring.use_case)
    request = _turn_request(session)

    with pytest.raises(turn_workflow.UnusableResumeAnswerError):
        asyncio.run(
            turn_workflow._step_resume(
                TURN,
                request,
                turn_workflow.HumanAnswer(
                    turn_id=OTHER_TURN, tool_call_id=CALL_A, approved=True, note=None
                ),
            )
        )

    with pytest.raises(turn_workflow.UnusableResumeAnswerError):
        asyncio.run(
            turn_workflow._step_resume(TURN, request, {"tool_call_id": CALL_A, "approved": True})
        )

    assert wiring.runner.resume_calls == [], "neither payload may reach the runner"


@pytest.mark.phase("F3")
def test_resolved_tool_satisfies_the_port_protocol() -> None:
    """`ResolvedTool` is what the use case hands to `AgentRunner.resume`, so it has to be
    a `ToolResolution`. Checked here rather than trusted, because a structural mismatch
    surfaces as a type error in a phase nobody is looking at."""
    resolution: ToolResolution = ResolvedTool(tool_call_id=CALL_A, approved=True, payload=None)
    assert resolution.tool_call_id == CALL_A
