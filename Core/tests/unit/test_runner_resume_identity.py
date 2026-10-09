"""What a RESUMED continuation still enforces, and how evidence reaches the model.

Phase:   F3 (the resume identity seats) / F7 (the bytes-versus-URL branch)
Tasks:   docs/TASKS.md#t-f3-16, docs/TASKS.md#t-f7-07
Covers:  ports/agent_runner.py, adapters/driven/agent_pydantic/runner.py

SILENT-BUG AREA (CLAUDE.md), TWICE OVER, AND NOTHING ELSE IN THE SUITE SEES EITHER

    1. A resumed turn is not one tool call. The call a human authorised is the FIRST of
       them; the model may then ask for more, and `resume` used to attach no
       `PolicyEnforcement` and no `ProcessHistory` because the port carried neither a
       `CallerIdentity` nor a `SessionRef`. So every FURTHER tool call on the continuation
       ran with no policy check, no audit row and no compaction - and every existing test
       passed, because they all resolve exactly one call and stop. `test_runner_resume.py`
       is that test: it proves the human's answer arrives, which is a different question.

    2. `MediaPolicy.delivery` decides whether evidence travels as bytes or as a URL, and
       docs/DECISIONS.md#d26 is the ruling: BYTES, by default, always. The fact that makes
       it a ruling rather than a preference is that Pydantic AI does NOT fetch an
       `ImageUrl` - it hands the URL to the PROVIDER, which downloads the file from our
       storage. For a photo of an identity document that is a disclosure to a third party,
       made in a request we never see and never logged. Nothing fails when the wrong branch
       is taken: the model answers about the picture either way.

WHAT IS ASSERTED HERE AND NOWHERE ELSE

    - A tool call made AFTER a resume is policed and audited EXACTLY like one made before
      it: same `ToolPolicy` snapshot, same `turn_id`, same `CallerIdentity`, same
      append-only row, written before the refusal reaches the model.
    - The resumed continuation reaches the `ContextEngine` for the same SESSION, so a long
      approval loop compacts instead of growing until the provider refuses it.
    - `knowledge_search` survives a resume. It is narrowed per tenant, so it was among the
      things the missing seats silently dropped.
    - Evidence bytes reach the model as `BinaryContent`, never as bare `bytes` and never as
      a URL - and a URL is built only when the profile explicitly opted into SIGNED_URL.
    - The port cannot be quietly un-widened: the PRE-widening `resume` signature must fail
      to type-check as `AgentRunner`, exactly as `tests/unit/test_ports_agent_runner.py`
      locks the pre-widening `run`. docs/TASKS.md#t-f1-05 is the note this follows.

NO MODEL, NO NETWORK, NO DATABASE. `FunctionModel` stands in for the provider, exactly as
`test_runner_resume.py` and `test_runner_hooks.py` do it, so the hooks are wired the way
production wires them and the assertions are made against a real Pydantic AI run.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    FileUrl,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

# Imported as a MODULE for the reason `test_runner_resume.py` states: while a target is
# still a stub, a name-level import turns "not implemented yet" into a collection-time
# ImportError instead of a red assertion inside a test that actually ran.
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.compaction import (
    CompactionPolicy,
    CompactionResult,
    ContextState,
    Rung,
)
from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    KnowledgeDoc,
    KnowledgeHit,
    TenantKnowledgePolicy,
)
from agent_core.domain.policy import Effect, PolicyDecision, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
)
from tests.fakes import ports as fakes

pytestmark = [pytest.mark.phase("F3"), pytest.mark.silent]

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"

TURN_ID = TurnId("turn-f3-16")
TENANT = TenantId("t-1")
SESSION = SessionRef(session_id=SessionId("s-f3-16"), tenant_id=TENANT)

# The tool the human answered, and the tool the model asks for AFTERWARDS. Two names,
# because the whole anchor is the difference between them: the first call is the one a
# human authorised, the second is the one that used to run unwatched.
ANSWERED_TOOL = "lookup_delivery_code"
AFTER_RESUME_TOOL = "freeze_account"

# A provider-shaped id, hostile to every plausible tidy-up: mixed case, an underscore run,
# a hyphen, nothing that parses as a UUID.
PROVIDER_TOOL_CALL_ID = "call_Ab3-XY_9__MiXeD"
OUTSIDE_ANSWER = "DLV-ANSWERED-FROM-OUTSIDE-7714"

DENY_REASON = "freezing an account is never automatic"
DENY_RULE = PolicyRule(
    rule_id="r-freeze-deny",
    tool_pattern=AFTER_RESUME_TOOL,
    effect=Effect.DENY,
    reason=DENY_REASON,
)

# A one-pixel-shaped PNG: a real signature so `sniff_media` can name it, and a body short
# enough to compare whole. Evidence in production is a photo; what matters here is that
# the bytes arrive unchanged and typed.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"evidence-bytes-not-a-url"
SIGNED_URL = "https://media.example.test/ab/abc123.png?expires=1799999999&sig=deadbeef"


@dataclass
class Resolution:
    """One human answer, in the shape `ports/agent_runner.py::ToolResolution` declares.

    Built here rather than imported from `application/resume_turn.py`: this is an adapter
    test and the adapter's contract is the structural Protocol, not one use case's carrier.
    """

    tool_call_id: ToolCallId
    approved: bool
    payload: object | None = None


class ScriptedModel:
    """A `FunctionModel` body that asks for ONE MORE TOOL after the resume, then answers.

    Recording `tool_names_offered` is what lets a toolset's ABSENCE be asserted: a
    tenant-narrowed tool that is silently dropped on resume looks identical to one that was
    never configured, unless the test reads what the provider was actually offered.
    """

    __name__ = "scripted_resume_identity_model"

    def __init__(self, *, ask_after_resume: bool = True) -> None:
        self.ask_after_resume = ask_after_resume
        self.requests = 0
        self.tool_returns: list[ToolReturnPart] = []
        self.user_parts: list[UserPromptPart] = []
        self.tool_names_offered: list[tuple[str, ...]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        self.tool_names_offered.append(tuple(tool.name for tool in info.function_tools))
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    self.tool_returns.append(part)
                elif isinstance(part, UserPromptPart):
                    self.user_parts.append(part)
        if self.requests == 1 and self.ask_after_resume:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        AFTER_RESUME_TOOL,
                        {"account_id": "acc-9"},
                        tool_call_id="call_after_resume_1",
                    )
                ],
                usage=RequestUsage(input_tokens=19, output_tokens=6),
            )
        return ModelResponse(
            parts=[TextPart("Understood.")],
            usage=RequestUsage(input_tokens=21, output_tokens=3),
        )


class Desk:
    """The two tools. Records its calls so "the body never ran" is a fact, not a hope."""

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.freezes: list[str] = []

    def lookup_delivery_code(self, order_id: str) -> str:
        """Return the internal delivery code for one order id."""
        self.lookups.append(order_id)
        return "DLV-FROM-THE-TOOL-BODY"

    def freeze_account(self, account_id: str) -> str:
        """Freeze one account. Denied by policy in every test in this module."""
        self.freezes.append(account_id)
        return "FROZEN"


class ScriptedContextEngine:
    """A `ContextEngine` that records which SESSION it was asked about, and when.

    The session is the whole point: `ProcessHistory` cannot be built without one, and a
    compaction attached for the wrong session would compact somebody else's conversation.
    """

    def __init__(self) -> None:
        self.states: list[ContextState] = []
        self.compress_sessions: list[SessionRef] = []

    def on_session_start(self, session: SessionRef) -> None: ...

    def update_from_response(self, session: SessionRef, usage: Any) -> None: ...

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        self.states.append(state)
        return False

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:  # pragma: no cover - should_compress says no in this module
        self.compress_sessions.append(session)
        return CompactionResult(
            compacted_history=[],
            checkpoint=None,
            rungs_applied=(Rung.L2_SLIDING_WINDOW,),
            tokens_before=1_000,
            tokens_after=100,
        )

    def on_session_end(self, session: SessionRef) -> None: ...


class ScriptedKnowledgeBase:
    """A `KnowledgeBase` that records the narrowed policy it was handed.

    The whole port is implemented, not just `search`: a partial stub would satisfy the
    runner today and stop satisfying `KnowledgeBase` the moment a caller reaches for
    another member, which is a test failing for a reason that is not the anchor.
    """

    def __init__(self) -> None:
        self.searches: list[tuple[TenantKnowledgePolicy, str]] = []

    async def search(
        self,
        policy: TenantKnowledgePolicy,
        query: str,
        *,
        collections: tuple[CollectionId, ...] = (),
    ) -> tuple[KnowledgeHit, ...]:  # pragma: no cover - presence is what is asserted
        self.searches.append((policy, query))
        return (
            KnowledgeHit(
                doc_id=DocId("d-1"),
                collection=CollectionId("prices"),
                title="Prices",
                excerpt="A widget costs 10.",
                score=0.9,
                version=3,
            ),
        )

    async def get(
        self, policy: TenantKnowledgePolicy, doc_id: DocId
    ) -> KnowledgeDoc | None:  # pragma: no cover - not reached by this module
        return None

    async def full_context(
        self, policy: TenantKnowledgePolicy
    ) -> str:  # pragma: no cover - not reached by this module
        return ""


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
    )


def _profile(**overrides: Any) -> AgentProfile:
    data: dict[str, Any] = {
        "id": "delivery",
        "persona": "You are a delivery desk assistant.",
        "model": "minimax/MiniMax-M3",
    }
    data.update(overrides)
    return AgentProfile.from_mapping(data)


def _model_factory(scripted: ScriptedModel) -> Any:
    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return factory


def _runner(
    *,
    scripted: ScriptedModel,
    desk: Desk | None = None,
    audit: Any = None,
    rules: tuple[PolicyRule, ...] = (),
    context: ScriptedContextEngine | None = None,
    knowledge: ScriptedKnowledgeBase | None = None,
) -> Any:
    tools = None
    if desk is not None:
        tools = fakes.FakeToolProvider(
            FunctionToolset([desk.lookup_delivery_code, desk.freeze_account]),
            (ANSWERED_TOOL, AFTER_RESUME_TOOL),
        )
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(
            RuleSet.for_caller(_caller(), rules, default_effect=Effect.ALLOW)
        ),
        audit=audit if audit is not None else fakes.FakeAuditSink(),
        tools=tools,
        knowledge=knowledge,
        context=context,
        model_factory=_model_factory(scripted),
    )


def _suspended_history(tool_name: str = ANSWERED_TOOL) -> list[ModelMessage]:
    """A turn frozen mid-exchange: the model asked, and nothing has answered yet.

    Unpaired BY DESIGN - the pending call is the thing a human is being asked about.
    """
    return [
        ModelRequest(parts=[UserPromptPart("What is the delivery code for order ord-42?")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name, {"order_id": "ord-42"}, tool_call_id=PROVIDER_TOOL_CALL_ID
                )
            ],
            usage=RequestUsage(input_tokens=11, output_tokens=7),
        ),
    ]


def _resume(
    runner: Any,
    *,
    profile: AgentProfile,
    history: Sequence[ModelMessage],
    resolutions: tuple[Resolution, ...],
) -> Any:
    """The one call shape, in one place: the port's settled `resume` arity.

    `caller` and `session` are the t-f3-16 seats. They are keyword-only on purpose - a
    positional pair added after `turn_id` would let an un-updated call site bind `profile`
    to `caller` and fail somewhere far away instead of here.
    """
    return asyncio.run(
        runner.resume(
            TURN_ID,
            profile,
            list(history),
            resolutions,
            caller=_caller(),
            session=SESSION,
        )
    )


def _binary_parts(scripted: ScriptedModel) -> list[BinaryContent]:
    """Every `BinaryContent` the provider was handed, wherever Pydantic AI put it.

    Both places are read because 2.31 may keep multimodal content inside the tool return
    or split it into a following user part depending on the provider - and which of the two
    happened is not what this module is pinning.
    """
    found: list[BinaryContent] = []
    for part in scripted.tool_returns:
        found.extend(_binaries_in(part.content))
    for user_part in scripted.user_parts:
        found.extend(_binaries_in(user_part.content))
    return found


def _file_urls(scripted: ScriptedModel) -> list[FileUrl]:
    """Every provider-fetched URL the provider was handed. D26 says: none, by default."""
    found: list[FileUrl] = []
    for part in scripted.tool_returns:
        found.extend(item for item in _items(part.content) if isinstance(item, FileUrl))
    for user_part in scripted.user_parts:
        found.extend(item for item in _items(user_part.content) if isinstance(item, FileUrl))
    return found


def _items(content: object) -> list[object]:
    if isinstance(content, (str, bytes)):
        return [content]
    if isinstance(content, Sequence):
        return list(content)
    return [content]


def _binaries_in(content: object) -> list[BinaryContent]:
    return [item for item in _items(content) if isinstance(item, BinaryContent)]


# --------------------------------------------------------------------------------------
# 1. t-f3-16 - the continuation is policed and audited, or it is a hole


def test_a_tool_call_made_after_a_resume_is_policed_and_audited() -> None:
    """THE ANCHOR. The second call is not the one a human authorised, and it knows it.

    The human answered `lookup_delivery_code`. The model then asks for `freeze_account`,
    which policy DENIES. Three things must be true and each one fails separately when the
    resume path attaches no `PolicyEnforcement`:

      - the tool body does not run (a denial that audits perfectly and still freezes the
        account has enforced nothing);
      - the refusal reaches the MODEL, carrying `PolicyDecision.reason`, so the agent can
        adapt instead of retrying blindly;
      - an append-only audit row names the turn, the caller and the verdict - filed under
        the SAME `turn_id` and the SAME `CallerIdentity` as a first-turn call would be.
    """
    scripted = ScriptedModel()
    desk = Desk()
    audit = fakes.FakeAuditSink()
    runner = _runner(scripted=scripted, desk=desk, audit=audit, rules=(DENY_RULE,))

    outcome = _resume(
        runner,
        profile=_profile(),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=OUTSIDE_ANSWER,
            ),
        ),
    )

    assert desk.freezes == [], (
        "the model asked for a DENIED tool after resuming and the body ran: the resumed "
        "continuation is not policed (docs/TASKS.md#t-f3-16)"
    )

    refusals = [
        str(part.content)
        for part in scripted.tool_returns
        if part.tool_name == AFTER_RESUME_TOOL
    ]
    assert refusals, "the model was told nothing about the denied call"
    assert DENY_REASON in refusals[0], (
        "the refusal must carry PolicyDecision.reason so the agent can adapt"
    )

    tool_rows = [call for call in audit.calls if call.kind == "tool_call"]
    assert [row.payload[2] for row in tool_rows] == [AFTER_RESUME_TOOL], (
        "a tool call made after a resume left no audit row - CLAUDE.md non-negotiable #6"
    )
    turn, audited_caller, _name, _arguments, decision = tool_rows[0].payload
    assert turn == TURN_ID
    assert audited_caller == _caller(), (
        "the row must name the caller whose rules were loaded, not a widened stand-in"
    )
    assert isinstance(decision, PolicyDecision)
    assert decision.effect is Effect.DENY
    assert decision.rule_id == DENY_RULE.rule_id

    assert outcome.turn_id == TURN_ID
    assert outcome.result is not None


def test_the_resumed_continuation_is_compacted_for_its_own_session() -> None:
    """The other seat. `ProcessHistory` needs a `SessionRef`; without one it is not attached.

    A resumed turn is exactly where this matters: an approval loop appends a request and a
    return per human answer, and a continuation that is never asked about grows until the
    provider refuses the whole conversation. The state's `session` is asserted, not merely
    that the engine was called - an engine asked about the wrong session compacts somebody
    else's history.
    """
    scripted = ScriptedModel(ask_after_resume=False)
    engine = ScriptedContextEngine()
    runner = _runner(scripted=scripted, desk=Desk(), context=engine)

    _resume(
        runner,
        profile=_profile(),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=OUTSIDE_ANSWER,
            ),
        ),
    )

    assert engine.states, (
        "the ContextEngine was never asked about the resumed continuation: compaction is "
        "not attached on resume (docs/TASKS.md#t-f3-16)"
    )
    assert {state.session for state in engine.states} == {SESSION}


def test_the_knowledge_tool_survives_a_resume() -> None:
    """`knowledge_search` is narrowed per TENANT, so it needs the caller the seats carry.

    Found by a wave-13b agent for exactly the same reason as the two capabilities above:
    `_per_turn_toolsets` takes a `CallerIdentity`, and a resume had none. The tool then
    vanished from the continuation - and CLAUDE.md non-negotiable #8's gate is ABSENCE, so
    a silently absent tool is indistinguishable from a deliberately disabled one.
    """
    scripted = ScriptedModel(ask_after_resume=False)
    knowledge = ScriptedKnowledgeBase()
    runner = _runner(
        scripted=scripted,
        desk=Desk(),
        knowledge=knowledge,
    )

    _resume(
        runner,
        profile=_profile(knowledge={"enabled": True, "collections": ["prices"]}),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=OUTSIDE_ANSWER,
            ),
        ),
    )

    assert scripted.tool_names_offered, "the provider was never called"
    assert runner_module.KNOWLEDGE_SEARCH_TOOL_NAME in scripted.tool_names_offered[0], (
        "knowledge_search was dropped from the resumed continuation"
    )


# --------------------------------------------------------------------------------------
# 2. t-f7-07 / D26 - evidence travels as bytes, and a URL is opt-in only


def test_evidence_bytes_reach_the_model_as_binary_content_and_never_as_a_url() -> None:
    """D26, in the one assertion that can see it: typed bytes in, no URL anywhere.

    `ResumeTurn` has already resolved the `MediaRef` through `MediaStore`, so what arrives
    here is a payload of bytes. Handing those to Pydantic AI raw makes it a `str()` of a
    bytes repr in the tool result - the model reads `b'\\x89PNG...'` and answers about a
    picture it never saw. Typing it as `BinaryContent` with the SNIFFED media type is what
    makes it an image, and the mime is sniffed rather than declared for the reason
    `IngestMedia` sniffs: a declared type is whatever the client said.

    The negative half is the ruling itself. No `FileUrl` of any kind may reach the
    provider on a profile that never opted into SIGNED_URL - and the default is BYTES.
    """
    scripted = ScriptedModel(ask_after_resume=False)
    runner = _runner(scripted=scripted, desk=Desk())

    _resume(
        runner,
        profile=_profile(),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=PNG_BYTES,
            ),
        ),
    )

    binaries = _binary_parts(scripted)
    assert binaries, (
        "the evidence bytes did not reach the model as BinaryContent: an untyped bytes "
        "payload is read by the provider as a repr, not as an image "
        "(docs/DECISIONS.md#d26, docs/TASKS.md#t-f7-07)"
    )
    assert binaries[0].data == PNG_BYTES, "the bytes must arrive unchanged"
    assert binaries[0].media_type == "image/png", (
        "the media type must be SNIFFED from the bytes, exactly as IngestMedia sniffs it"
    )
    assert _file_urls(scripted) == [], (
        "a URL reached the provider on a BYTES profile: Pydantic AI hands a URL to the "
        "PROVIDER, which downloads the evidence itself - D26 forbids it by default"
    )


def test_a_url_payload_is_not_turned_into_a_provider_fetch_unless_the_profile_opted_in() -> None:
    """The branch is `MediaPolicy.delivery`, and BYTES is not a mode that guesses.

    A string payload on a BYTES profile is an ordinary tool result and stays one. Promoting
    any URL-shaped string to an `ImageUrl` would hand the provider a fetch the profile
    never authorised - and it would do so for a tool result that merely happened to contain
    a link.
    """
    scripted = ScriptedModel(ask_after_resume=False)
    runner = _runner(scripted=scripted, desk=Desk())

    _resume(
        runner,
        profile=_profile(),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=SIGNED_URL,
            ),
        ),
    )

    assert _file_urls(scripted) == [], (
        "a BYTES profile must not promote a string to a provider-fetched URL"
    )
    assert any(SIGNED_URL in str(part.content) for part in scripted.tool_returns), (
        "the payload must still reach the model as the ordinary tool result it is"
    )


def test_the_signed_url_branch_is_reached_only_when_the_profile_opts_into_it() -> None:
    """The opt-in half of D26: `delivery: signed_url` is a written decision, so honour it.

    The URL is typed by what it points at - an image URL is an `ImageUrl`, which is what
    tells the provider to look at it as an image rather than to read it as a document.
    """
    scripted = ScriptedModel(ask_after_resume=False)
    runner = _runner(scripted=scripted, desk=Desk())

    _resume(
        runner,
        profile=_profile(media={"delivery": "signed_url"}),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=SIGNED_URL,
            ),
        ),
    )

    urls = _file_urls(scripted)
    assert [type(url).__name__ for url in urls] == [ImageUrl.__name__], (
        "an opted-in signed URL for an image must reach the provider as an ImageUrl"
    )
    assert urls[0].url == SIGNED_URL, "the URL must be handed over verbatim"


def test_bytes_are_still_sent_as_bytes_on_a_signed_url_profile() -> None:
    """Opting into URLs does not forbid bytes. Bytes are never the disclosure.

    The branch reads the PAYLOAD as well as the policy: `signed_url` says a URL is
    permitted, not that a payload already resolved to bytes must be pushed back out to
    storage to become one.
    """
    scripted = ScriptedModel(ask_after_resume=False)
    runner = _runner(scripted=scripted, desk=Desk())

    _resume(
        runner,
        profile=_profile(media={"delivery": "signed_url"}),
        history=_suspended_history(),
        resolutions=(
            Resolution(
                tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                approved=True,
                payload=PNG_BYTES,
            ),
        ),
    )

    binaries = _binary_parts(scripted)
    assert [binary.data for binary in binaries] == [PNG_BYTES]
    assert _file_urls(scripted) == []


# --------------------------------------------------------------------------------------
# 3. The regression lock, moved WITH the port - docs/TASKS.md#t-f1-05


PRE_WIDENING_RESUME_STUB = """
from __future__ import annotations

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest
from agent_core.ports.agent_runner import AgentRunner, ToolResolution


class PreWideningRunner:
    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:
        raise NotImplementedError

    # The signature `resume` carried BEFORE the caller/session seats were added. It is the
    # exact shape the lock has to reject: a resume that cannot be handed the identity its
    # own enforcement requires.
    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        raise NotImplementedError


runner: AgentRunner = PreWideningRunner()
"""


@pytest.mark.phase("F1")
def test_the_pre_widening_resume_signature_does_not_type_check_as_agent_runner(
    tmp_path: Path,
) -> None:
    """A lock that only checked the new shape would accept the old one too.

    Same reasoning as `tests/unit/test_ports_agent_runner.py`'s negative case for `run`,
    and the same note behind it (docs/TASKS.md#t-f1-05): the widening cannot be quietly
    undone, because the pre-widening signature is the expression this test rejects.

    Checked in a subprocess over a module written OUTSIDE the repository tree: a fixture
    that deliberately fails to type-check must never be picked up by the project-wide run.
    """
    module = tmp_path / "pre_widening.py"
    module.write_text(PRE_WIDENING_RESUME_STUB, encoding="utf-8")

    env = dict(os.environ)
    env["MYPYPATH"] = str(SRC_DIR)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=str(CORE_DIR),
        env=env,
        check=False,
    )

    assert result.returncode != 0, (
        "AgentRunner accepted a resume() with no caller and no session seat - the "
        "pre-widening arity. A resumed continuation cannot be policed, audited or "
        "compacted without them (docs/TASKS.md#t-f3-16)."
    )
    assert "Incompatible types in assignment" in result.stdout, (
        f"Expected the assignment to AgentRunner to be the rejected expression.\n"
        f"{result.stdout}{result.stderr}"
    )
