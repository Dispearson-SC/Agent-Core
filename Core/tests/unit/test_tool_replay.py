"""C30 (D65): a tool call a crash interrupted is not run twice.

Subjects: domain/tool_replay.py (`replay_policy`, `interrupted_result`), ports/tool_journal.py,
adapters/driven/agent_pydantic/runner.py (`PolicyEnforcement` journals every call that is not
`replay: safe`), tests/fakes/tool_journal.py.

THE SCENARIO. The durable workflow runs a turn step again after a crash. A call the first attempt
began (the journal has it, no end) and whose tool is not `replay: safe` must not run a second time:
the model is handed an `interrupted` result instead and decides what to do. A `safe` tool just runs
again. The crash is a `BaseException` raised inside the tool body: it passes every `except
Exception` of the framework exactly like a killed process leaves no tidy ending.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.tool_replay import (
    DEFAULT_REPLAY,
    ReplayPolicy,
    interrupted_result,
    replay_policy,
    safe_tool_metadata,
)
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from agent_core.ports.tool_journal import ToolJournalUnavailableError, ToolRunState
from tests.fakes import ports as fakes
from tests.fakes.tool_journal import FakeToolJournal

SESSION = SessionRef(session_id="case-1", tenant_id=TenantId("t"))  # type: ignore[arg-type]
CALLER = CallerIdentity(subject_id="u", channel="web", tenant_id=TenantId("t"))
TURN = TurnId("turn-1")
PROFILE = AgentProfile.from_mapping({"id": "p", "persona": "x", "model": "m"})
REQUEST = TurnRequest(session=SESSION, caller=CALLER, profile_id="p", input=UserInput(text="go"))


class _Crash(BaseException):
    """What a killed process looks like from inside a tool: nothing catches it."""


class _Tools:
    """Two tools: `send` (not classified, so `interrupted`) and `peek` (declared safe)."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.crash_next = False

    def toolset(self) -> FunctionToolset[None]:
        def send(message: str) -> str:
            """Send a message."""
            self.runs.append("send")
            if self.crash_next:
                self.crash_next = False
                raise _Crash
            return "sent"

        def peek() -> str:
            """Read the state."""
            self.runs.append("peek")
            if self.crash_next:
                self.crash_next = False
                raise _Crash
            return "state"

        return FunctionToolset([Tool(send), Tool(peek, metadata=safe_tool_metadata())])


def _runner(tools: _Tools, journal: FakeToolJournal | None, call: ToolCallPart) -> Any:
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = [
            str(p.content) for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(parts=[call])
        seen.extend(returned)
        return ModelResponse(parts=[TextPart("done")])

    built = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(tools.toolset(), ("send", "peek")),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        tool_journal=journal,
    )
    built.returned = seen
    return built


def _run(runner: Any) -> None:
    asyncio.run(runner.run(TURN, REQUEST, PROFILE, None))


def _send() -> ToolCallPart:
    return ToolCallPart("send", {"message": "hi"}, tool_call_id="call-1")


def _peek() -> ToolCallPart:
    return ToolCallPart("peek", {}, tool_call_id="call-1")


# --- the domain -------------------------------------------------------------------------------


def test_an_unclassified_tool_and_an_unknown_word_are_not_safe() -> None:
    assert DEFAULT_REPLAY is ReplayPolicy.INTERRUPTED
    assert replay_policy(None) is ReplayPolicy.INTERRUPTED
    assert replay_policy({}) is ReplayPolicy.INTERRUPTED
    assert replay_policy({"replay": "yolo"}) is ReplayPolicy.INTERRUPTED
    assert replay_policy({"replay": "safe"}) is ReplayPolicy.SAFE
    assert replay_policy(safe_tool_metadata()) is ReplayPolicy.SAFE


def test_the_interrupted_result_says_what_is_known_and_what_was_not_done() -> None:
    text = interrupted_result("send")

    assert "interrupted" in text and "`send`" in text and "not repeated" in text


# --- the runner -------------------------------------------------------------------------------


def test_a_non_replayable_tool_interrupted_by_a_crash_is_not_run_again() -> None:
    """The acceptance test: attempt 1 dies inside `send`; attempt 2 (same turn, same call id) gets
    an `interrupted` result and `send` is not run a second time."""
    tools, journal = _Tools(), FakeToolJournal()
    tools.crash_next = True
    with pytest.raises(_Crash):
        _run(_runner(tools, journal, _send()))
    assert journal.rows[(str(TURN), "r1:call-1")][1] is ToolRunState.STARTED

    second = _runner(tools, journal, _send())
    _run(second)

    assert tools.runs == ["send"], "the second attempt did not run it again"
    assert second.returned == [interrupted_result("send")]


def test_a_safe_tool_interrupted_by_a_crash_simply_runs_again() -> None:
    tools, journal = _Tools(), FakeToolJournal()
    tools.crash_next = True
    with pytest.raises(_Crash):
        _run(_runner(tools, journal, _peek()))

    second = _runner(tools, journal, _peek())
    _run(second)

    assert tools.runs == ["peek", "peek"] and second.returned == ["state"]
    assert journal.rows == {}, "a safe tool is never journaled"


def test_a_call_that_finished_is_not_run_a_second_time_either() -> None:
    """The step is repeated after the tool returned but before the turn was recorded."""
    tools, journal = _Tools(), FakeToolJournal()
    first = _runner(tools, journal, _send())
    _run(first)
    assert journal.rows[(str(TURN), "r1:call-1")][1] is ToolRunState.FINISHED

    second = _runner(tools, journal, _send())
    _run(second)

    assert tools.runs == ["send"] and second.returned == [interrupted_result("send")]


def test_a_tool_that_raises_is_finished_and_a_new_call_id_is_a_new_call() -> None:
    tools, journal = _Tools(), FakeToolJournal()
    _run(_runner(tools, journal, _send()))
    other = ToolCallPart("send", {"message": "hi"}, tool_call_id="call-2")

    second = _runner(tools, journal, other)
    _run(second)

    assert tools.runs == ["send", "send"] and second.returned == ["sent"]


def test_a_provider_that_reuses_a_call_id_across_responses_still_runs_each_call() -> None:
    """R3-journal-id-reuse / R4-journal-key-collision: some providers number calls per response
    (`call-1` again in the next one). The second `send` is a different call in a later model
    response and must run, not be answered `interrupted` because the first one finished."""
    tools, journal = _Tools(), FakeToolJournal()
    seen_returns: list[int] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        seen_returns.append(len(returned))
        if len(returned) < 2:
            return ModelResponse(parts=[_send()])  # the same id, response after response
        return ModelResponse(parts=[TextPart("done")])

    built = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(tools.toolset(), ("send", "peek")),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        tool_journal=journal,
    )
    _run(built)

    assert tools.runs == ["send", "send"], "both calls ran"
    assert len(journal.rows) == 2, "and each has its own journal row"


class _UnreachableJournal(FakeToolJournal):
    """A journal whose store cannot be reached (the pool checkout timed out)."""

    def __init__(self, *, begin_fails: bool, end_fails: bool) -> None:
        super().__init__()
        self._begin_fails, self._end_fails = begin_fails, end_fails

    async def begin(self, turn_id: TurnId, tool_call_id: str, tool: str) -> ToolRunState:
        if self._begin_fails:
            raise ToolJournalUnavailableError("no connection")
        return await super().begin(turn_id, tool_call_id, tool)

    async def finish(self, turn_id: TurnId, tool_call_id: str) -> None:
        if self._end_fails:
            raise ToolJournalUnavailableError("no connection")
        await super().finish(turn_id, tool_call_id)


def test_an_unreachable_journal_fails_closed_before_the_tool_runs() -> None:
    """R4-journal-pool-checkout: if the journal cannot say whether the call began, running it could
    be a second run. The turn step fails with the named error (the workflow retries the step) and
    the tool is not run."""
    tools = _Tools()
    journal = _UnreachableJournal(begin_fails=True, end_fails=False)

    with pytest.raises(ToolJournalUnavailableError):
        _run(_runner(tools, journal, _send()))

    assert tools.runs == []


def test_an_unreachable_journal_after_the_tool_ran_keeps_the_result() -> None:
    """The call already happened: failing the turn would throw its result away and the retry would
    be answered `interrupted` anyway. The row is left `started` (the conservative record) and the
    model gets the real result."""
    tools = _Tools()
    journal = _UnreachableJournal(begin_fails=False, end_fails=True)
    built = _runner(tools, journal, _send())

    _run(built)

    assert tools.runs == ["send"] and built.returned == ["sent"]
    assert journal.rows[(str(TURN), "r1:call-1")][1] is ToolRunState.STARTED


def test_without_a_journal_nothing_changes() -> None:
    tools = _Tools()
    _run(_runner(tools, None, _send()))
    _run(_runner(tools, None, _send()))

    assert tools.runs == ["send", "send"]


def test_a_call_that_defers_for_approval_is_forgotten_so_the_approved_run_executes_it() -> None:
    """A tool that asks for approval from inside its body never ran: its journal row is dropped,
    or the run that a person's approval triggers would find it `started` and refuse it."""
    journal = FakeToolJournal()

    def risky() -> str:
        """Do the risky thing."""
        raise ApprovalRequired

    tools = _Tools()
    toolset = FunctionToolset([Tool(risky)])
    built = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(toolset, ("risky",)),
        model_factory=lambda model_id, base_url: FunctionModel(
            lambda messages, info: ModelResponse(
                parts=[ToolCallPart("risky", {}, tool_call_id="call-1")]
            )
        ),
        tool_journal=journal,
    )

    outcome = asyncio.run(built.run(TURN, REQUEST, PROFILE, None))

    assert outcome.pending, "the turn suspended for the approval"
    assert journal.rows == {}, tools.runs


# --- the built-in toolsets declare what they are -----------------------------------------------


def _policies(toolset: Any) -> dict[str, ReplayPolicy]:
    return {name: replay_policy(tool.metadata) for name, tool in toolset.tools.items()}


def test_the_built_in_toolsets_declare_their_reads_and_deferrals_safe() -> None:
    from agent_core.adapters.driven.tools import evidence, peer_background, peers, subagents
    from agent_core.adapters.driven.tools.delivery import tools as delivery
    from agent_core.adapters.driven.tools.fraud import tools as fraud

    safe, interrupted = ReplayPolicy.SAFE, ReplayPolicy.INTERRUPTED
    assert _policies(peers.build_toolset()) == {"ask_peer": safe}
    assert _policies(subagents.build_toolset()) == {"spawn_subagent": safe}
    assert _policies(evidence.build_toolset()) == {"request_evidence": safe}
    assert _policies(peer_background.build_toolset()) == {
        "peer_list": safe,
        "peer_status": safe,
        "peer_message": interrupted,  # it enqueues a message: repeating it is not harmless
        "peer_stop": safe,  # stopping twice is stopping once
    }
    assert _policies(delivery.build_toolset()) == {
        "routing_estimate": safe,
        "pricing_quote": safe,
        "pricing_apply": interrupted,
        "orders_lookup": safe,
    }
    assert _policies(fraud.build_toolset()) == {
        "sql_readonly": safe,
        "account_history": safe,
        "case_notes_append": interrupted,
        "freeze_account": interrupted,
    }


def test_the_knowledge_search_tool_is_safe() -> None:
    from agent_core.domain.knowledge import KnowledgePolicy, TenantKnowledgePolicy

    toolset = runner_module.build_knowledge_toolset(
        object(),  # type: ignore[arg-type]  # building the toolset never calls the knowledge base
        TenantKnowledgePolicy.for_caller(
            CALLER, KnowledgePolicy(enabled=True, collections=("faq",))
        ),
    )

    assert toolset is not None and _policies(toolset) == {"knowledge_search": ReplayPolicy.SAFE}


# --- the case this exists for: an approved call, executed by the resumed turn -----------------


class _Approval:
    tool_call_id = "call-1"
    approved = True
    payload = None


def _approval_runner(tools: _Tools, journal: FakeToolJournal) -> Any:
    from agent_core.domain.policy import PolicyRule

    rule = PolicyRule(rule_id="ask", tool_pattern="send", effect=Effect.NEEDS_APPROVAL, reason="r")

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[_send()])

    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (rule,))),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(tools.toolset(), ("send", "peek")),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        tool_journal=journal,
    )


def test_an_approved_call_that_crashes_in_the_resumed_turn_is_not_run_again() -> None:
    """The person approved `send`; the resumed turn begins it and the process dies. The durable
    workflow repeats the resume with the same history and the same approval: the call keeps the id
    it was suspended under, so the journal knows it and `send` is not run a second time."""
    tools, journal = _Tools(), FakeToolJournal()
    runner = _approval_runner(tools, journal)
    suspended = asyncio.run(runner.run(TURN, REQUEST, PROFILE, None))
    assert suspended.pending and tools.runs == [], "nothing ran before the approval"
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[_send()]),
    ]

    def resume() -> None:
        asyncio.run(
            runner.resume(
                TURN,
                PROFILE,
                history,
                (_Approval(),),
                caller=CALLER,
                session=SESSION,  # type: ignore[arg-type]
            )
        )

    tools.crash_next = True
    with pytest.raises(_Crash):
        resume()
    resume()  # the workflow repeats the step

    assert tools.runs == ["send"], "the repeated resume did not run `send` a second time"
