"""C20 (D50): manage the background agents of a conversation.

Subject: `WorkflowBackgroundAgents` (adapters/driving/workflow/background_agents.py), over the
in-memory mailbox (the reference behaviour the conformance suite pins) and a stubbed engine.

What is defended:
  * every verb is scoped to the OWNER conversation (tenant and session): another conversation's
    handle is exactly as unknown as one that never existed, and a stop never reaches it;
  * `stop` cancels the child: the row, its workflow and what it asked in turn (the C4 cascade);
  * `message` is a new background ask that CONTINUES the child's session, through the same two-sided
    gate, caps and ownership rules as any ask, and is idempotent per tool call.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from agent_core.adapters.driven.tools.background import (
    BackgroundOwner,
    MessageMode,
    StopResult,
)
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.adapters.driving.workflow.background_agents import WorkflowBackgroundAgents
from agent_core.domain.peers import (
    BACKGROUND_LIST_LIMIT,
    AgentId,
    BackgroundState,
    background_followup_turn_id,
)
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
)
from tests.fakes import fanout
from tests.fakes.memory_mailbox import InMemoryAgentMailbox

SESSION = SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t"))
CALLER = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))
OWNER = BackgroundOwner(
    session=SESSION,
    profile_id=fanout.ASKER,
    caller=CALLER,
    turn_id="turn-2",
)


class _Engine:
    """The slice of DBOS the stop cascade uses."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_workflow_async(self, workflow_id: str, **_: Any) -> None:
        self.cancelled.append(workflow_id)

    async def get_workflow_status_async(self, workflow_id: str) -> None:
        return None


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, **asker_policy: Any) -> None:
        self.mailbox = InMemoryAgentMailbox()
        self.profiles = fanout.profiles()
        asker = self.profiles[fanout.ASKER]
        self.profiles[fanout.ASKER] = dataclasses.replace(
            asker,
            peers=dataclasses.replace(asker.peers, allow_background=True, **asker_policy),
        )
        self.engine = _Engine()
        monkeypatch.setattr(turn_workflow, "DBOS", self.engine)
        monkeypatch.setattr(
            turn_workflow,
            "_dependencies",
            turn_workflow.TurnWorkflowDependencies(
                start_turn=None,  # type: ignore[arg-type]
                peers=turn_workflow.PeerSeat(mailbox=self.mailbox, profiles=self.profiles),
            ),
        )
        self.seat = WorkflowBackgroundAgents(self.mailbox, self.profiles)

    async def ask_in_background(
        self,
        question: str = "slow one",
        *,
        session: SessionRef = SESSION,
        key: str = "tc-1",
        target: str = fanout.PEER,
    ) -> str:
        return await self.mailbox.ask(
            self.profiles[fanout.ASKER].peers,
            AgentId(target),
            question,
            from_session=session,
            turn_id=TurnId("turn-1"),
            hop=0,
            asker=AgentId(fanout.ASKER),
            request_key=key,
            background_caller=CALLER,
        )


def run[T](coroutine: Any) -> T:
    return asyncio.run(coroutine)  # type: ignore[no-any-return]


# --- list and status --------------------------------------------------------------------------


def test_list_shows_this_conversations_background_asks_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    other_case = SessionRef(session_id=SessionId("case-2"), tenant_id=TenantId("t"))
    other_tenant = SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t2"))

    async def scenario() -> tuple[str, ...]:
        mine = await world.ask_in_background("mine", key="tc-1")
        await world.ask_in_background("elsewhere", session=other_case, key="tc-2")
        await world.ask_in_background("another tenant", session=other_tenant, key="tc-3")
        assert [a.handle for a in await world.seat.list(OWNER)] == [mine]
        return tuple(a.question for a in await world.seat.list(OWNER))

    assert run(scenario()) == ("mine",)


def test_status_of_a_handle_from_another_conversation_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    other_case = SessionRef(session_id=SessionId("case-2"), tenant_id=TenantId("t"))

    async def scenario() -> tuple[Any, Any, Any]:
        mine = await world.ask_in_background("mine", key="tc-1")
        theirs = await world.ask_in_background("theirs", session=other_case, key="tc-2")
        return (
            await world.seat.status(OWNER, mine),
            await world.seat.status(OWNER, theirs),
            await world.seat.status(OWNER, "never-asked"),
        )

    found, foreign, unknown = run(scenario())

    assert found is not None and found.state is BackgroundState.QUEUED
    assert foreign is None and unknown is None


def test_a_handle_older_than_the_listing_window_is_still_found_and_stoppable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1o: status and stop used to look the handle up in a listing of the newest 50."""
    world = _World(monkeypatch)

    async def scenario() -> tuple[Any, StopResult, int, int]:
        oldest = await world.ask_in_background("oldest", key="tc-old")
        for n in range(BACKGROUND_LIST_LIMIT + 2):
            await world.ask_in_background(f"q{n}", key=f"tc-{n}")
        listed = {a.handle for a in await world.seat.list(OWNER)}
        assert oldest not in listed
        return (
            await world.seat.status(OWNER, oldest),
            await world.seat.stop(OWNER, oldest),
            await world.seat.count(OWNER),
            len(listed),
        )

    status, stopped, total, shown = run(scenario())

    assert status is not None and status.question == "oldest"
    assert stopped is StopResult.STOPPED
    assert (total, shown) == (BACKGROUND_LIST_LIMIT + 3, BACKGROUND_LIST_LIMIT)


# --- stop -------------------------------------------------------------------------------------


def test_stop_cancels_the_row_and_the_callee_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)

    async def scenario() -> tuple[str, StopResult]:
        handle = await world.ask_in_background()
        return handle, await world.seat.stop(OWNER, handle)

    handle, result = run(scenario())

    assert result is StopResult.STOPPED
    assert run(world.mailbox.is_cancelled(handle)) is True
    assert turn_workflow.peer_workflow_id(handle) in world.engine.cancelled


def test_stop_reaches_what_the_child_asked_in_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)

    async def scenario() -> tuple[str, str]:
        handle = await world.ask_in_background()
        callee_turn = turn_workflow.peer_workflow_id(handle)
        nested = await world.mailbox.ask(
            world.profiles[fanout.PEER].peers,
            AgentId(fanout.ASKER),
            "pass it on",
            from_session=SESSION,
            turn_id=TurnId(callee_turn),
            hop=1,
            asker=AgentId(fanout.PEER),
            request_key="n-1",
        )
        await world.seat.stop(OWNER, handle)
        return handle, nested

    _, nested = run(scenario())

    assert run(world.mailbox.is_cancelled(nested)) is True


def test_stop_of_a_finished_ask_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)

    async def scenario() -> StopResult:
        handle = await world.ask_in_background()
        token = world.mailbox.token_of(handle)
        await world.mailbox.answer(handle, "done", answer_token=token)
        return await world.seat.stop(OWNER, handle)

    assert run(scenario()) is StopResult.NOT_RUNNING
    assert world.engine.cancelled == []


def test_stop_never_reaches_another_conversations_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    other_case = SessionRef(session_id=SessionId("case-2"), tenant_id=TenantId("t"))

    async def scenario() -> tuple[str, StopResult]:
        theirs = await world.ask_in_background("theirs", session=other_case)
        return theirs, await world.seat.stop(OWNER, theirs)

    theirs, result = run(scenario())

    assert result is StopResult.UNKNOWN
    assert run(world.mailbox.is_cancelled(theirs)) is False and world.engine.cancelled == []


# --- message ----------------------------------------------------------------------------------


def test_a_message_is_a_background_ask_that_continues_the_childs_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)

    async def scenario() -> tuple[str, Any]:
        handle = await world.ask_in_background()
        result = await world.seat.message(
            OWNER, handle, "use the March invoice", MessageMode.STEER, call_id="call-1"
        )
        assert result.refusal is None and result.handle is not None
        return handle, result

    child, result = run(scenario())

    sent = world.mailbox.rows[result.handle]
    assert sent.peer_session_id == world.mailbox.rows[child].peer_session_id
    assert sent.target == fanout.PEER and sent.asker == fanout.ASKER
    assert sent.background_caller == CALLER, "its answer comes back like any background answer"
    assert "use the March invoice" in sent.question and sent.hop == 1
    assert sent.turn_id == "turn-2" and sent.root_turn_id == "turn-1", (
        "asked by this turn, hanging from the root the child hangs from"
    )


def test_the_two_modes_are_framed_differently_for_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)

    async def scenario() -> tuple[str, str]:
        handle = await world.ask_in_background()
        steer = await world.seat.message(OWNER, handle, "x", MessageMode.STEER, call_id="c1")
        follow = await world.seat.message(OWNER, handle, "x", MessageMode.FOLLOW_UP, call_id="c2")
        assert steer.handle and follow.handle
        return world.mailbox.rows[steer.handle].question, world.mailbox.rows[follow.handle].question

    steer_text, follow_text = run(scenario())

    assert steer_text != follow_text
    assert "steering" in steer_text.lower() and "follow-up" in follow_text.lower()


def test_the_same_tool_call_sends_one_message(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)

    async def scenario() -> tuple[str | None, str | None]:
        handle = await world.ask_in_background()
        first = await world.seat.message(OWNER, handle, "again", MessageMode.FOLLOW_UP, call_id="c")
        replay = await world.seat.message(
            OWNER, handle, "again", MessageMode.FOLLOW_UP, call_id="c"
        )
        return first.handle, replay.handle

    first, replay = run(scenario())

    assert first == replay and len(world.mailbox.rows) == 2


@pytest.mark.parametrize("case", ["other_conversation", "unknown", "stopped"])
def test_a_message_to_something_that_is_not_a_running_child_of_this_conversation_is_refused(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    world = _World(monkeypatch)
    other_case = SessionRef(session_id=SessionId("case-2"), tenant_id=TenantId("t"))

    async def scenario() -> Any:
        if case == "other_conversation":
            handle = await world.ask_in_background("theirs", session=other_case)
        elif case == "unknown":
            handle = "never-asked"
        else:
            handle = await world.ask_in_background()
            await world.seat.stop(OWNER, handle)
        return await world.seat.message(OWNER, handle, "hello", MessageMode.STEER, call_id="c")

    result = run(scenario())

    assert result.handle is None and result.refusal
    assert len(world.mailbox.rows) == (0 if case == "unknown" else 1), "nothing new was sent"
    assert all("hello" not in row.question for row in world.mailbox.rows.values())


def test_a_message_to_a_finished_child_continues_its_session_as_a_new_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)

    async def scenario() -> Any:
        handle = await world.ask_in_background()
        await world.mailbox.answer(handle, "done", answer_token=world.mailbox.token_of(handle))
        return await world.seat.message(
            OWNER, handle, "one more thing", MessageMode.FOLLOW_UP, call_id="c"
        )

    result = run(scenario())

    assert result.handle is not None and result.refusal is None


def test_a_message_goes_through_the_two_sided_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    # The specialist stopped allowing this asker after the child started.
    world.profiles[fanout.PEER] = dataclasses.replace(
        world.profiles[fanout.PEER],
        peers=dataclasses.replace(world.profiles[fanout.PEER].peers, peers=()),
    )

    async def scenario() -> Any:
        handle = await world.ask_in_background()
        return await world.seat.message(OWNER, handle, "hello", MessageMode.STEER, call_id="c")

    result = run(scenario())

    assert result.handle is None and "allow" in (result.refusal or "").lower()
    assert len(world.mailbox.rows) == 1


def test_a_message_counts_toward_the_conversations_background_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch, max_background_asks=1)

    async def scenario() -> Any:
        handle = await world.ask_in_background()
        return await world.seat.message(OWNER, handle, "hello", MessageMode.STEER, call_id="c")

    result = run(scenario())

    assert result.handle is None and "background" in (result.refusal or "")


def test_replaying_a_message_at_the_cap_returns_the_same_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1o: the replay found its own earlier message open and counted it against the cap, so the
    second call of a tool that is meant to be idempotent was refused."""
    world = _World(monkeypatch, max_background_asks=2)

    async def scenario() -> tuple[Any, Any]:
        handle = await world.ask_in_background()
        first = await world.seat.message(OWNER, handle, "hi", MessageMode.STEER, call_id="c")
        replay = await world.seat.message(OWNER, handle, "hi", MessageMode.STEER, call_id="c")
        return first, replay

    first, replay = run(scenario())

    assert first.handle is not None and replay.refusal is None
    assert replay.handle == first.handle and len(world.mailbox.rows) == 2


def test_a_follow_up_turn_may_not_message_a_background_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1o: a message is a background ask, so it would restart the chain the follow-up cannot."""
    world = _World(monkeypatch)

    async def scenario() -> tuple[Any, int]:
        handle = await world.ask_in_background()
        await world.mailbox.answer(handle, "done", answer_token=world.mailbox.token_of(handle))
        in_follow_up = dataclasses.replace(OWNER, turn_id=background_followup_turn_id(handle))
        result = await world.seat.message(
            in_follow_up, handle, "more", MessageMode.FOLLOW_UP, call_id="c"
        )
        return result, len(world.mailbox.rows)

    result, rows = run(scenario())

    assert result.handle is None and "follow-up" in (result.refusal or "")
    assert rows == 1, "nothing was sent"


def test_a_missing_asker_profile_is_not_reported_as_an_unknown_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)

    async def scenario() -> Any:
        handle = await world.ask_in_background()
        del world.profiles[fanout.ASKER]
        return await world.seat.message(OWNER, handle, "hi", MessageMode.STEER, call_id="c")

    result = run(scenario())

    assert result.handle is None
    assert "profile" in (result.refusal or "") and "handle" not in (result.refusal or "")


def test_a_profile_that_did_not_opt_in_cannot_message(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    world.profiles[fanout.ASKER] = dataclasses.replace(
        world.profiles[fanout.ASKER],
        peers=dataclasses.replace(world.profiles[fanout.ASKER].peers, allow_background=False),
    )

    async def scenario() -> Any:
        handle = await world.ask_in_background()
        return await world.seat.message(OWNER, handle, "hello", MessageMode.STEER, call_id="c")

    assert run(scenario()).handle is None
