"""The fault-injectable mailbox and the in-memory one it usually wraps (C7).

`tests/fakes/controlled_mailbox.py` is to the mailbox what Pi's `ControlledStorage` is to its
storage: it records every call and can fail, hang or delay a verb on the Nth call, before or
AFTER the verb took effect (a lost acknowledgement is the interesting failure). The in-memory
mailbox (`tests/fakes/memory_mailbox.py`) is conformance-tested in `tests/contract`; here the
wrapper's own behaviour is pinned, so a restart test that leans on it can be trusted.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.domain.peers import AgentId
from agent_core.domain.turn import TurnId
from tests.contract.mailbox_conformance import POLICY, SESSION, TARGET
from tests.fakes.controlled_mailbox import ControlledMailbox, ProcessDied
from tests.fakes.memory_mailbox import InMemoryAgentMailbox


def _mailbox() -> tuple[ControlledMailbox, InMemoryAgentMailbox]:
    inner = InMemoryAgentMailbox()
    return ControlledMailbox(inner), inner


async def _ask(mailbox: ControlledMailbox, question: str = "q", key: str = "tc-1") -> str:
    return await mailbox.ask(
        POLICY,
        TARGET,
        question,
        from_session=SESSION,
        turn_id=TurnId("turn-1"),
        hop=0,
        asker=AgentId("asker"),
        request_key=key,
    )


def test_it_records_every_call_in_order_with_its_sequence_number() -> None:
    mailbox, _ = _mailbox()

    async def scenario() -> None:
        first = await _ask(mailbox)
        await mailbox.read_answer(first)
        await _ask(mailbox, "another", "tc-2")

    asyncio.run(scenario())

    assert [(c.verb, c.number) for c in mailbox.calls] == [
        ("ask", 1),
        ("read_answer", 1),
        ("ask", 2),
    ]
    assert mailbox.count("ask") == 2 and mailbox.count("answer") == 0


def test_a_verb_fails_on_the_nth_call_only_and_before_its_effect() -> None:
    mailbox, inner = _mailbox()
    mailbox.fail("ask", ConnectionError("down"), on_call=2)

    async def scenario() -> None:
        await _ask(mailbox)
        with pytest.raises(ConnectionError):
            await _ask(mailbox, "second", "tc-2")
        await _ask(mailbox, "third", "tc-3")

    asyncio.run(scenario())

    assert len(inner.rows) == 2, "the failed call never reached the inner mailbox"
    assert [c.outcome for c in mailbox.calls] == ["ok", "ConnectionError", "ok"]


def test_a_failure_after_the_effect_models_a_lost_acknowledgement() -> None:
    mailbox, inner = _mailbox()
    mailbox.fail("ask", ConnectionError("ack lost"), after=True)

    async def scenario() -> None:
        with pytest.raises(ConnectionError):
            await _ask(mailbox)

    asyncio.run(scenario())

    assert len(inner.rows) == 1, "the ask was written and the caller was told it failed"


def test_a_failure_can_repeat_for_several_calls() -> None:
    mailbox, _ = _mailbox()
    mailbox.fail("read_answer", TimeoutError("slow"), on_call=1, times=2)

    async def scenario() -> list[str]:
        seen: list[str] = []
        for _ in range(3):
            try:
                await mailbox.read_answer("x")
                seen.append("ok")
            except TimeoutError:
                seen.append("timeout")
        return seen

    assert asyncio.run(scenario()) == ["timeout", "timeout", "ok"]


def test_a_simulated_kill_is_a_base_exception_no_except_exception_can_swallow() -> None:
    mailbox, _ = _mailbox()
    mailbox.fail("ask", ProcessDied(), after=True)

    async def scenario() -> None:
        try:
            await _ask(mailbox)
        except Exception:  # noqa: BLE001 - the point of the test
            pytest.fail("a kill must not be catchable as an Exception")

    with pytest.raises(ProcessDied):
        asyncio.run(scenario())


def test_a_hung_verb_holds_until_released_and_reports_when_entered() -> None:
    mailbox, inner = _mailbox()
    gate = mailbox.hang("ask")

    async def scenario() -> tuple[bool, int, str]:
        task = asyncio.create_task(_ask(mailbox))
        await asyncio.wait_for(gate.entered.wait(), 2)
        held_rows = len(inner.rows)
        gate.release()
        return task.done(), held_rows, await task

    done_while_held, rows_while_held, correlation_id = asyncio.run(scenario())

    assert done_while_held is False and rows_while_held == 0, "nothing happened while it hung"
    assert correlation_id and len(inner.rows) == 1


def test_a_verb_can_be_delayed_without_changing_its_result() -> None:
    mailbox, _ = _mailbox()
    mailbox.delay("read_answer", 0.05)

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await mailbox.read_answer("nobody") is None
        return loop.time() - started

    assert asyncio.run(scenario()) >= 0.05


def test_the_queue_side_verbs_pass_through_and_are_recorded() -> None:
    mailbox, _ = _mailbox()

    async def scenario() -> str | None:
        await _ask(mailbox)
        claimed = await mailbox.claim_next(TARGET)
        return None if claimed is None else claimed.correlation_id

    assert asyncio.run(scenario()) is not None
    assert mailbox.count("claim_next") == 1
