"""The suspension half of F3, reached the way production reaches it - t-f11-41.

Phase:   F3 (deferred) / F7 (evidence) / F9 (peers)
Tasks:   docs/TASKS.md#t-f11-41
Covers:  adapters/driven/agent_pydantic/runner.py (the DeferredToolRequests translation)

WHY THIS MODULE EXISTS AT ALL, AND WHY NO EXISTING TEST COULD HAVE CAUGHT IT
    Every other test of a deferred call in this repository BUILDS the deferred result
    itself and hands it back: `test_runner_resume.py` resumes from a `ToolResolution`,
    `test_communicable_suspension.py` asserts the shape of a `PendingRequest` somebody
    constructed. None of them ever asked Pydantic AI to PRODUCE a deferred call, because
    none of them ran an agent whose toolset actually contained a deferred tool the model
    was allowed to call - `ask_peer` was `deny [no matching rule]` in the shipped rule set,
    so the model was never offered it and this code was never walked.

    The result was that `PydanticAgentRunner` had NO deferred output type, and the first
    real `ask_peer` call in production died with Pydantic AI's
    `UserError: A deferred tool call was present, but DeferredToolRequests is not among
    output types`. A green suite and a working process were different things for the ninth
    time (docs/STATE.md).

    So every test here runs the REAL runner over a REAL deferred tool with a model that
    calls it. Nothing in this module constructs a `DeferredToolRequests`, a
    `PendingRequest` or a `tool_call_id`; they all have to come back out of the library.

NO MODEL, NO NETWORK, NO DATABASE - same arrangement as tests/unit/test_runner.py.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driven.tools import evidence as evidence_tools
from agent_core.adapters.driven.tools import peers as peer_tools
from agent_core.domain.policy import Effect, PolicyDecision, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

pytestmark = [pytest.mark.phase("F3"), pytest.mark.silent]

TURN_ID = TurnId("turn-f11-41")
ASK_PEER = "ask_peer"
REQUEST_EVIDENCE = "request_evidence"
APPROVAL_TOOL = "pricing_apply"

PEER_TARGET = "billing_specialist"
PEER_QUESTION = "Was invoice INV-4471 charged twice?"
PEER_ANSWER = "INV-4471 was charged twice on 2026-09-02; the duplicate is refundable."


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="console",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _request(text: str = "Ask the billing specialist about INV-4471.") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id="s-1", tenant_id=TenantId("t-1")),  # type: ignore[arg-type]
        caller=_caller(),
        profile_id="support_triage",
        input=UserInput(text=text),
    )


def _profile() -> AgentProfile:
    return AgentProfile.from_mapping(
        {
            "id": "support_triage",
            "persona": "You triage support conversations.",
            "model": "minimax/MiniMax-M3",
        }
    )


def _allowing_policy(*tool_names: str) -> fakes.FakeToolPolicy:
    """ALLOW for exactly the named tools - the YAML grant that reached this code."""
    rules = tuple(
        PolicyRule(
            rule_id=f"r-allow-{name}",
            tool_pattern=name,
            effect=Effect.ALLOW,
            reason=f"this profile may call {name}",
        )
        for name in tool_names
    )
    return fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), rules))


class CallsOneTool:
    """A model that calls one named tool once, then answers in text.

    It records the `tool_call_id` it minted, which is what makes "the PROVIDER's id was
    round-tripped" an assertion rather than a hope - CLAUDE.md's silent-bug table.
    """

    __name__ = "calls_one_tool"

    def __init__(self, tool_name: str, args: dict[str, Any], final_text: str = "done") -> None:
        self.tool_name = tool_name
        self.args = args
        self.final_text = final_text
        self.requests = 0
        self.tool_call_ids_sent: list[str] = []
        self.tool_results_seen: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            part = ToolCallPart(self.tool_name, self.args)
            self.tool_call_ids_sent.append(part.tool_call_id)
            return ModelResponse(
                parts=[part], usage=RequestUsage(input_tokens=11, output_tokens=7)
            )
        self.tool_results_seen.extend(str(part.content) for part in returns)
        return ModelResponse(
            parts=[TextPart(self.final_text)],
            usage=RequestUsage(input_tokens=13, output_tokens=5),
        )


def _model_factory(scripted: CallsOneTool) -> Any:
    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return factory


def _runner(
    *,
    scripted: CallsOneTool,
    toolset: object,
    tool_names: tuple[str, ...],
    audit: fakes.FakeAuditSink | None = None,
    policy: Any = None,
) -> Any:
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=policy if policy is not None else _allowing_policy(*tool_names),
        audit=audit if audit is not None else fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(toolset, tool_names),
        model_factory=_model_factory(scripted),
    )


# --------------------------------------------------------------------------------------
# The defect itself: a deferred call must SUSPEND the turn, never raise


def test_an_ask_peer_call_suspends_the_turn_instead_of_raising() -> None:
    """t-f11-41, reproduced exactly as the console reproduced it.

    Before the fix this raised Pydantic AI's `UserError` - "a deferred tool call was
    present, but DeferredToolRequests is not among output types" - and the whole turn was
    lost. The peer mechanism (t-f9-04) had therefore never left a turn in production.
    """
    scripted = CallsOneTool(ASK_PEER, {"target": PEER_TARGET, "question": PEER_QUESTION})
    runner = _runner(
        scripted=scripted, toolset=peer_tools.build_toolset(), tool_names=(ASK_PEER,)
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert outcome.is_suspended, (
        "a deferred ask_peer call must come back as a SUSPENDED TurnOutcome; "
        f"got result={outcome.result!r}"
    )
    assert outcome.result is None
    assert len(outcome.pending) == 1


def test_a_peer_ask_is_labelled_DELEGATION_so_no_human_is_asked_it() -> None:
    """CLAUDE.md non-negotiable #11 and t-f9-08: the runner is the ONE producer of a kind.

    `PendingKind.DELEGATION.answerable_by_a_human` is False, which is what keeps a question
    only another agent can answer out of a person's inbox. Labelling a peer ask EVIDENCE
    here would publish it to a human who can do nothing with it while the turn holds open.
    """
    scripted = CallsOneTool(ASK_PEER, {"target": PEER_TARGET, "question": PEER_QUESTION})
    runner = _runner(
        scripted=scripted, toolset=peer_tools.build_toolset(), tool_names=(ASK_PEER,)
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))
    (pending,) = outcome.pending

    assert pending.kind is PendingKind.DELEGATION
    assert pending.kind.answerable_by_a_human is False
    assert pending.tool_name == ASK_PEER
    assert pending.arguments == {"target": PEER_TARGET, "question": PEER_QUESTION}


def test_the_pending_tool_call_id_is_the_providers_own_string() -> None:
    """The silent bug in CLAUDE.md's table: a regenerated id binds to no pending call.

    Nothing downstream can notice - the resume is dropped without an exception and the
    agent asks forever - so the only place it can be caught is here, against the id the
    model actually minted.
    """
    scripted = CallsOneTool(ASK_PEER, {"target": PEER_TARGET, "question": PEER_QUESTION})
    runner = _runner(
        scripted=scripted, toolset=peer_tools.build_toolset(), tool_names=(ASK_PEER,)
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))
    (pending,) = outcome.pending

    assert pending.tool_call_id == scripted.tool_call_ids_sent[0]


def test_a_request_evidence_call_suspends_as_EVIDENCE() -> None:
    """The second shared deferred mechanism (t-f7-05), which had never left a turn either.

    Both mechanisms are `CallDeferred`, so they arrive in the same `DeferredToolRequests
    .calls` list and the tool NAME is the only thing that separates them - which is exactly
    the discrimination `PendingKind` says the runner owns, once, here.
    """
    scripted = CallsOneTool(
        REQUEST_EVIDENCE, {"kind": "photo", "reason": "a photo of the damaged parcel"}
    )
    runner = _runner(
        scripted=scripted,
        toolset=evidence_tools.build_toolset(),
        tool_names=(REQUEST_EVIDENCE,),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))
    (pending,) = outcome.pending

    assert pending.kind is PendingKind.EVIDENCE
    assert pending.kind.answerable_by_a_human is True
    assert pending.tool_name == REQUEST_EVIDENCE
    assert "damaged parcel" in pending.reason, (
        "an EVIDENCE request must keep the sentence that tells the person WHAT to send"
    )


def test_an_approval_required_call_suspends_as_APPROVAL() -> None:
    """The third shape in `DeferredToolRequests`, and the only one in `.approvals`.

    A tool raising `ApprovalRequired` is D9's approval half. It reaches the same
    translation and must not be mislabelled as an externally-executed call: an APPROVAL is
    answerable by a human and a DELEGATION is not, and the two go to different places.
    """

    def pricing_apply(order_id: str) -> str:
        """Apply a price override to one order."""
        raise ApprovalRequired

    scripted = CallsOneTool(APPROVAL_TOOL, {"order_id": "ord-42"})
    runner = _runner(
        scripted=scripted,
        toolset=FunctionToolset([pricing_apply]),
        tool_names=(APPROVAL_TOOL,),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))
    (pending,) = outcome.pending

    assert pending.kind is PendingKind.APPROVAL
    assert pending.kind.answerable_by_a_human is True
    assert pending.tool_name == APPROVAL_TOOL
    assert pending.arguments == {"order_id": "ord-42"}


# --------------------------------------------------------------------------------------
# The half that makes the suspension resumable at all


def test_the_suspended_outcome_carries_the_messages_holding_the_call() -> None:
    """Without them the conversation has no record of the call a resume must answer.

    `resume` finds the pending ids with `_pending_tool_calls`, which reads the LAST
    `ModelResponse` in the stored history. A suspended outcome that returned no messages
    would leave `ConversationStore.append_outcome` nothing to write, and the resume three
    days later would refuse every id with `UnknownToolCallIdError` - a turn that can never
    be answered, and nothing anywhere raising until then.
    """
    scripted = CallsOneTool(ASK_PEER, {"target": PEER_TARGET, "question": PEER_QUESTION})
    runner = _runner(
        scripted=scripted, toolset=peer_tools.build_toolset(), tool_names=(ASK_PEER,)
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))
    (pending,) = outcome.pending

    assert outcome.messages, "a suspended turn must still return the agent's own messages"
    # `TurnOutcome.messages` is opaque to the domain on purpose; an ADAPTER may open it,
    # and this assertion is the adapter's own round-trip - the ids `resume` will accept,
    # read back out of the history `run` produced.
    history = cast("list[ModelMessage]", list(outcome.messages))
    assert runner_module._pending_tool_calls(history) == (pending.tool_call_id,)


def test_run_then_resume_completes_the_loop_and_the_peer_answer_stays_fenced() -> None:
    """The whole mechanism end to end, for the first time, on one runner.

    `run` suspends, the history it produced is fed straight back, and `resume` answers the
    SAME id with what the mailbox already wrapped. CLAUDE.md non-negotiable #10: a peer's
    answer is untrusted content, so what the model reads must still be inside the
    delimiters - the deferred path must not route around the fence by supplying a raw
    string, and the runner must not add a second fence around the one the mailbox applied.
    """
    scripted = CallsOneTool(ASK_PEER, {"target": PEER_TARGET, "question": PEER_QUESTION})
    runner = _runner(
        scripted=scripted, toolset=peer_tools.build_toolset(), tool_names=(ASK_PEER,)
    )
    profile = _profile()

    suspended = asyncio.run(runner.run(TURN_ID, _request(), profile, None))
    (pending,) = suspended.pending

    class Answer:
        tool_call_id = ToolCallId(pending.tool_call_id)
        approved = True
        # What `_step_answer_peer` actually puts here: the bytes `AgentMailbox.read_answer`
        # hands back, which the adapter wrapped on read. Built with `wrap_peer_answer`
        # directly now that `peers.ask_peer_result` is retired - and closer to production
        # for it, because that is the one function the mailbox itself calls.
        payload = wrap_peer_answer(PEER_ANSWER)

    finished = asyncio.run(
        runner.resume(
            TURN_ID,
            profile,
            list(suspended.messages),
            (Answer(),),
            caller=_caller(),
            session=SessionRef(session_id="s-1", tenant_id=TenantId("t-1")),  # type: ignore[arg-type]
        )
    )

    assert not finished.is_suspended
    assert finished.result is not None
    (seen,) = scripted.tool_results_seen
    assert seen == wrap_peer_answer(PEER_ANSWER), (
        "the peer's answer reached the model unfenced or double-fenced"
    )
    assert seen.count(runner_module.UNTRUSTED_OPEN) == 1


# --------------------------------------------------------------------------------------
# t-f11-45: a NEEDS_APPROVAL verdict suspends through that same deferred path
#
# `PolicyEnforcement` used to answer NEEDS_APPROVAL with `SkipToolExecution`, which ENDS
# the call: the model is told the tool is awaiting a human and the turn finishes with no
# way for that human to ever answer. It was the wrong answer in the safe direction, and
# it was only that because the output type a suspension needs did not exist (t-f11-41).
#
# F3's acceptance criterion is what the difference buys: a turn waits for approval, the
# service is REDEPLOYED, and approving twenty-four hours later resumes it. A hook that
# blocks cannot express that at all - and no hook can hold a coroutine open for a person.
#
# The policy engine is one of the five silent-bug areas in CLAUDE.md, so what is asserted
# here is not only "it suspends": the tool BODY must not run on either non-ALLOW verdict,
# the audit row must still name the winning rule, and DENY must be untouched.

APPROVAL_RULE_ID = "r-approve-pricing_apply"
APPROVAL_POLICY_REASON = "a price override moves money and needs a second person"
DENY_RULE_ID = "r-deny-pricing_apply"
DENY_POLICY_REASON = "price overrides are disabled for this tenant"
ORDER_ID = "ord-42"


def _pricing_toolset(ran: list[str]) -> FunctionToolset[Any]:
    """A tool with a REAL body, so "it did not run" is observable rather than assumed.

    `test_an_approval_required_call_suspends_as_APPROVAL` above raises `ApprovalRequired`
    from inside the body, which proves the translation but cannot prove the enforcement:
    a body that raises has already been entered. This one records that it was entered.
    """

    def pricing_apply(order_id: str) -> str:
        """Apply a price override to one order."""
        ran.append(order_id)
        return f"override applied to {order_id}"

    return FunctionToolset([pricing_apply])


def _policy_over(effect: Effect, *, rule_id: str, reason: str) -> fakes.FakeToolPolicy:
    """One rule over `APPROVAL_TOOL`, so the verdict under test is the only thing moving."""
    return fakes.FakeToolPolicy(
        RuleSet.for_caller(
            _caller(),
            (
                PolicyRule(
                    rule_id=rule_id,
                    tool_pattern=APPROVAL_TOOL,
                    effect=effect,
                    reason=reason,
                ),
            ),
        )
    )


def _needs_approval_policy() -> fakes.FakeToolPolicy:
    return _policy_over(
        Effect.NEEDS_APPROVAL, rule_id=APPROVAL_RULE_ID, reason=APPROVAL_POLICY_REASON
    )


def test_a_needs_approval_verdict_suspends_the_turn_rather_than_refusing() -> None:
    """t-f11-45, and `t-f3-02`'s open decision now that the output type exists.

    The verdict comes from the POLICY, not from the tool - the tool here is an ordinary
    one with an ordinary body, exactly as `pricing_apply` is in the shipped rule set. That
    is the whole point: an operator writes `needs_approval` in YAML and the turn suspends,
    with no tool author having to raise anything.
    """
    ran: list[str] = []
    scripted = CallsOneTool(APPROVAL_TOOL, {"order_id": ORDER_ID})
    runner = _runner(
        scripted=scripted,
        toolset=_pricing_toolset(ran),
        tool_names=(APPROVAL_TOOL,),
        policy=_needs_approval_policy(),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert outcome.is_suspended, (
        "a NEEDS_APPROVAL verdict must SUSPEND the turn so a human can answer it later; "
        f"got result={outcome.result!r}"
    )
    assert outcome.result is None
    (pending,) = outcome.pending
    assert pending.kind is PendingKind.APPROVAL
    assert pending.kind.answerable_by_a_human is True
    assert pending.tool_name == APPROVAL_TOOL
    assert pending.arguments == {"order_id": ORDER_ID}
    assert pending.tool_call_id == scripted.tool_call_ids_sent[0]
    assert ran == [], (
        "the tool body ran while policy said a human had to decide first - "
        "NEEDS_APPROVAL must block execution exactly as DENY does (t-f1-12)"
    )


def test_the_audit_row_is_written_before_the_approval_suspends_the_turn() -> None:
    """`t-f1-12` pinned the ordering and it is the security property, not a style choice.

    The audit write happens BEFORE the raise on every non-ALLOW verdict. Moving the
    suspension in front of it would leave a call that stopped a turn for a day with no
    record of which rule stopped it, and CLAUDE.md non-negotiable #6 says a failed turn
    must still leave a trace.
    """
    ran: list[str] = []
    audit = fakes.FakeAuditSink()
    scripted = CallsOneTool(APPROVAL_TOOL, {"order_id": ORDER_ID})
    runner = _runner(
        scripted=scripted,
        toolset=_pricing_toolset(ran),
        tool_names=(APPROVAL_TOOL,),
        audit=audit,
        policy=_needs_approval_policy(),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert outcome.is_suspended
    (row,) = [call for call in audit.calls if call.kind == "tool_call"]
    _recorded_turn, _recorded_caller, tool_name, arguments, recorded = row.payload
    assert tool_name == APPROVAL_TOOL
    assert arguments == {"order_id": ORDER_ID}
    # `AuditCall.payload` is deliberately an opaque tuple - see the fake - so the verdict
    # is named here rather than by the fake, which is what keeps the assertion honest.
    decision = cast("PolicyDecision", recorded)
    assert decision.effect is Effect.NEEDS_APPROVAL
    assert decision.rule_id == APPROVAL_RULE_ID, (
        "the audit row must name the rule that actually won, or nobody can answer "
        "'why was this held?' - t-f1-12"
    )


def test_a_denied_call_still_refuses_in_place_and_never_suspends() -> None:
    """DENY is NOT moved by this change, and that is the assertion guarding it.

    Suspending a denial would put a call a rule forbids in front of a human as something
    to approve, which converts a DENY into a NEEDS_APPROVAL that anyone with an inbox can
    overturn. The refusal reaches the model with the rule's reason, the body never runs,
    and the turn finishes - exactly as `t-f1-12` fixed it.
    """
    ran: list[str] = []
    scripted = CallsOneTool(APPROVAL_TOOL, {"order_id": ORDER_ID})
    runner = _runner(
        scripted=scripted,
        toolset=_pricing_toolset(ran),
        tool_names=(APPROVAL_TOOL,),
        policy=_policy_over(Effect.DENY, rule_id=DENY_RULE_ID, reason=DENY_POLICY_REASON),
    )

    outcome = asyncio.run(runner.run(TURN_ID, _request(), _profile(), None))

    assert not outcome.is_suspended
    assert outcome.pending == ()
    assert ran == []
    (seen,) = scripted.tool_results_seen
    assert DENY_POLICY_REASON in seen, "the refusal must reach the model with its reason"


def test_an_approved_call_runs_on_resume_and_is_not_held_a_second_time() -> None:
    """F3's acceptance criterion in miniature - and the loop that would eat it.

    The hook runs AGAIN on the continuation, over a fresh snapshot that still says
    NEEDS_APPROVAL. A hook that only asks the rules would therefore hold the call a second
    time, suspend again, and ask the same human the same question forever: the turn would
    look like a slow approver and nothing anywhere would raise. Pydantic AI states the
    fact that closes it - `RunContext.tool_call_approved` - so the answer a human already
    gave is read from the library rather than re-derived here.
    """
    ran: list[str] = []
    scripted = CallsOneTool(APPROVAL_TOOL, {"order_id": ORDER_ID})
    runner = _runner(
        scripted=scripted,
        toolset=_pricing_toolset(ran),
        tool_names=(APPROVAL_TOOL,),
        policy=_needs_approval_policy(),
    )
    profile = _profile()

    suspended = asyncio.run(runner.run(TURN_ID, _request(), profile, None))
    (pending,) = suspended.pending

    class Approval:
        tool_call_id = ToolCallId(pending.tool_call_id)
        approved = True
        payload = None

    finished = asyncio.run(
        runner.resume(
            TURN_ID,
            profile,
            list(suspended.messages),
            (Approval(),),
            caller=_caller(),
            session=SessionRef(session_id="s-1", tenant_id=TenantId("t-1")),  # type: ignore[arg-type]
        )
    )

    assert not finished.is_suspended, (
        "an approved call was held for approval a second time; the turn never finishes"
    )
    assert finished.result is not None
    assert ran == [ORDER_ID], (
        "the approved tool body must run exactly once, on the continuation"
    )
