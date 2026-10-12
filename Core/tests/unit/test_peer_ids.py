"""Deterministic peer ids (C2): a DBOS replay of the ask step cannot mint a second ask.

The correlation id of an ask and the session its answering turn runs in are DERIVED from what
identifies the ask (the asking turn, the tool call, the target and the question), never drawn
from a random source. A replayed `_step_ask_peers` then names the same row, and a re-run worker
names the same session.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from agent_core.adapters.driving.peers.worker import new_peer_session
from agent_core.domain.peers import AgentId, peer_ask_id, peer_session_key
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId, TurnOutcome
from tests.fakes import fanout

TURN = TurnId("turn-1")
TARGET = AgentId("p-peer")
ORIGIN = SessionRef(session_id=SessionId("case-77"), tenant_id=TenantId("S030"))


def test_the_ask_id_is_stable_and_a_uuid() -> None:
    first = peer_ask_id(TURN, "tc-1", TARGET, "how much?")

    assert first == peer_ask_id(TURN, "tc-1", TARGET, "how much?")
    assert str(UUID(first)) == first


def test_each_input_changes_the_ask_id() -> None:
    base = peer_ask_id(TURN, "tc-1", TARGET, "how much?")

    assert peer_ask_id(TurnId("turn-2"), "tc-1", TARGET, "how much?") != base
    assert peer_ask_id(TURN, "tc-2", TARGET, "how much?") != base
    assert peer_ask_id(TURN, "tc-1", AgentId("other"), "how much?") != base
    assert peer_ask_id(TURN, "tc-1", TARGET, "how much??") != base


def test_the_fields_cannot_be_shifted_into_one_another() -> None:
    assert peer_ask_id(TURN, "a", TARGET, "b:c") != peer_ask_id(TURN, "a:b", TARGET, "c")
    shifted = peer_ask_id(TURN, "tc", AgentId("a"), "bc")
    assert peer_ask_id(TURN, "tc", AgentId("ab"), "c") != shifted


def test_a_keyed_peer_session_is_stable_and_still_carries_the_root_origin() -> None:
    key = peer_session_key("corr-1")

    first = new_peer_session(TenantId("S030"), origin=ORIGIN, key=key)
    again = new_peer_session(TenantId("S030"), origin=ORIGIN, key=key)

    assert first == again
    assert first.session_id.startswith("peer~case-77~")
    assert first.session_id != new_peer_session(TenantId("S030"), origin=ORIGIN, key="other")


def test_a_keyed_session_unwraps_a_peer_origin_to_the_root() -> None:
    nested = new_peer_session(TenantId("S030"), origin=ORIGIN, key=peer_session_key("c-1"))

    deeper = new_peer_session(TenantId("S030"), origin=nested, key=peer_session_key("c-2"))

    assert deeper.session_id.startswith("peer~case-77~")
    assert deeper.session_id.count("~") == 2


def test_an_unkeyed_peer_session_is_still_unique() -> None:
    assert (
        new_peer_session(TenantId("S030"), origin=ORIGIN).session_id
        != new_peer_session(TenantId("S030"), origin=ORIGIN).session_id
    )


def test_the_ask_step_files_the_ask_under_the_tool_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "x")])

    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.request_keys == ["r1.s0.tc-1"]


# --- C13: the request key names the round and the position, not only the tool call ---------------


def test_the_same_tool_call_id_in_two_rounds_is_two_different_request_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider may reuse a tool call id in a later model step. The ask key carries the round
    and the position in it, so a legitimate repeat is a fresh ask (D36) while a replay of the
    same round and position keeps the same key."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"),),
        [fanout.answer("corr-1", "first"), fanout.answer("corr-2", "second")],
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-1"),))]

    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.request_keys == ["r1.s0.tc-1", "r2.s0.tc-1"]


def test_asks_of_one_round_are_keyed_by_their_position_in_tool_call_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-b"), fanout.ask("tc-a")),
        [fanout.answer("corr-1", "a"), fanout.answer("corr-2", "b")],
    )

    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.request_keys == ["r1.s0.tc-a", "r1.s1.tc-b"]
