"""C11 (D40): `peers.max_parallel_asks` and `peers.max_concurrent_asks`.

Subject: adapters/driving/workflow/turn_workflow.py (`_step_ask_peers`), domain/peers.py
(`PeerPolicy`), domain/profile.py (the `peers:` keys). The concurrent cap itself lives on the
answering side's claim and is covered by the mailbox conformance suite
(`tests/contract/mailbox_conformance.py`).

The workflow BODY is driven without DBOS, like `test_peer_fanout.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.domain.peers import (
    DEFAULT_MAX_CONCURRENT_ASKS,
    DEFAULT_MAX_PARALLEL_ASKS,
    PeerPolicy,
)
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import TurnOutcome
from tests.fakes import fanout


def _run() -> TurnOutcome:
    return asyncio.run(fanout.body()(fanout.request()))


def _ids(count: int) -> list[str]:
    return [f"tc-{n:02d}" for n in range(1, count + 1)]


def test_ten_asks_in_one_step_dispatch_eight_and_refuse_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asks = tuple(fanout.ask(tool_call_id) for tool_call_id in _ids(10))
    world = fanout.install(
        monkeypatch, asks, [fanout.answer(f"corr-{n}", f"a{n}") for n in range(1, 9)]
    )

    _run()

    assert len(world.mailbox.asks) == 8, "only eight left"
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    results = dict(world.resume.calls[0])
    assert sorted(results) == _ids(10)
    refused = [tool for tool in _ids(10) if 'status="refused"' in str(results[tool])]
    assert refused == ["tc-09", "tc-10"], "the excess is the last of the step, in call order"
    for tool in refused:
        text = str(results[tool])
        assert "8" in text and "parallel" in text, "the model is told which limit it hit"
    assert all('status="ok"' in str(results[tool]) for tool in _ids(8))


def test_refused_asks_for_other_reasons_do_not_use_up_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asks = (fanout.ask("tc-00", target="nobody-home"), *(fanout.ask(t) for t in _ids(8)))
    world = fanout.install(
        monkeypatch, asks, [fanout.answer(f"corr-{n}", "x") for n in range(1, 9)]
    )

    _run()

    results = dict(world.resume.calls[0])
    assert len(world.mailbox.asks) == 8, "the unknown peer never left, so it holds no slot"
    assert 'status="refused"' in str(results["tc-00"])
    assert all('status="ok"' in str(results[tool]) for tool in _ids(8))


def test_the_parallel_cap_is_the_asking_profiles_own(monkeypatch: pytest.MonkeyPatch) -> None:
    asks = tuple(fanout.ask(tool_call_id) for tool_call_id in _ids(3))
    world = fanout.install(
        monkeypatch, asks, [fanout.answer("corr-1", "a"), fanout.answer("corr-2", "b")]
    )
    fanout.asker_policy(max_parallel_asks=2)

    _run()

    assert len(world.mailbox.asks) == 2
    assert 'status="refused"' in str(dict(world.resume.calls[0])["tc-03"])


def test_the_defaults_are_eight_and_four() -> None:
    policy = PeerPolicy()

    assert (policy.max_parallel_asks, policy.max_concurrent_asks) == (8, 4)
    assert (DEFAULT_MAX_PARALLEL_ASKS, DEFAULT_MAX_CONCURRENT_ASKS) == (8, 4)


def _profile(**peers: object) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "m", "peers": {"enabled": True, **peers}}
    )


def test_a_profile_sets_both_caps() -> None:
    profile = _profile(max_parallel_asks=3, max_concurrent_asks=2)

    assert (profile.peers.max_parallel_asks, profile.peers.max_concurrent_asks) == (3, 2)


@pytest.mark.parametrize("key", ["max_parallel_asks", "max_concurrent_asks"])
@pytest.mark.parametrize("bad", [0, -1, 1.5, "4", True])
def test_a_cap_that_is_not_a_positive_whole_number_refuses_to_load(key: str, bad: object) -> None:
    with pytest.raises(ProfileValidationError, match=key):
        _profile(**{key: bad})


def test_both_caps_are_part_of_the_content_hash() -> None:
    base = _profile().content_hash

    assert _profile(max_parallel_asks=3).content_hash != base
    assert _profile(max_concurrent_asks=2).content_hash != base
