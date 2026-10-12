"""C25 (D57): peer answers are remembered per (tenant, target, audience, question, version).

Subjects: domain/answer_cache.py, the `peers.answer_cache_*` profile fields, domain/peer_result.py
(the `cached` attribute) and the peer round in adapters/driving/workflow/turn_workflow.py
(`_step_ask_peers`, `_collect_round`). The adapters are tests/contract/test_answer_cache.py.

What is defended: a repeated question within the TTL is answered from the cache WITHOUT a peer run;
nothing is cached unless the deployment and the answering profile both opt in; only an `ok`
answer is ever stored; a peer the asker may not ask stays refused whatever the cache holds; the
audience, the version and the TTL separate entries; and a cache that fails costs a miss, never a
turn.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.peers.answer_cache import PgAnswerCache
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.composition import Settings
from agent_core.domain.answer_cache import (
    AnswerCacheKey,
    AnswerCacheScope,
    CachedAnswer,
    audience_for,
    normalize_question,
)
from agent_core.domain.peer_result import PeerResult, PeerStatus
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import PendingKind, PendingRequest, ToolCallId
from tests.fakes import fanout
from tests.fakes.answer_cache import InMemoryAnswerCache

# --- the question, the key, the audience ---


@pytest.mark.parametrize(
    "a, b",
    [
        ("What is the TOTAL for order 7?", "what is the total for order 7"),
        ("  what   is the total\tfor order 7 ?? ", "What is the total for order 7"),
        ("¿Cuánto debe el pedido 7?", "cuánto debe el pedido 7"),
        ("ﬁnal balance", "final balance"),
    ],
)
def test_questions_that_differ_only_in_case_spacing_or_edge_punctuation_are_one(
    a: str, b: str
) -> None:
    assert normalize_question(a) == normalize_question(b)


def test_questions_with_different_words_are_different() -> None:
    assert normalize_question("total for order 7") != normalize_question("total for order 8")
    assert normalize_question("order 7 total") != normalize_question("total order 7")


def test_each_part_of_the_key_changes_its_digest() -> None:
    base = AnswerCacheKey("t", "billing", "subject:u", "q", "v1")
    others = [
        AnswerCacheKey("t2", "billing", "subject:u", "q", "v1"),
        AnswerCacheKey("t", "support", "subject:u", "q", "v1"),
        AnswerCacheKey("t", "billing", "subject:v", "q", "v1"),
        AnswerCacheKey("t", "billing", "subject:u", "r", "v1"),
        AnswerCacheKey("t", "billing", "subject:u", "q", "v2"),
        AnswerCacheKey("t", "billing", "subject:u", "q\x1fv1", ""),  # no collision by joining
    ]
    assert len({base.digest(), *(k.digest() for k in others)}) == 1 + len(others)


def test_the_audience_is_the_asker_and_the_subject_or_the_sorted_roles() -> None:
    subject = audience_for(AnswerCacheScope.SUBJECT, "orch", "u-1", ["b", "a"])
    shared = audience_for(AnswerCacheScope.ROLES, "orch", "u-1", ["b", "a", "a"])
    assert subject == "orch|subject:u-1"
    assert shared == "orch|roles:a,b"
    assert audience_for(AnswerCacheScope.ROLES, "orch", "u-2", ["a", "b"]) == shared
    assert audience_for(AnswerCacheScope.ROLES, "other", "u-1", ["a", "b"]) != shared, (
        "another asking agent is another audience"
    )


# --- the profile switch ---


def _profile(**peers: Any) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "m", "peers": {"enabled": True, **peers}}
    )


def test_the_cache_is_off_by_default_and_a_profile_opts_in_with_a_ttl() -> None:
    assert _profile().peers.answer_cache_seconds == 0
    assert _profile().peers.answer_cache_scope is AnswerCacheScope.SUBJECT
    on = _profile(answer_cache_seconds=60, answer_cache_scope="roles")
    assert on.peers.answer_cache_seconds == 60
    assert on.peers.answer_cache_scope is AnswerCacheScope.ROLES
    assert on.content_hash != _profile().content_hash


@pytest.mark.parametrize("bad", [-1, 1.5, "60", True, 86_401])
def test_a_malformed_ttl_is_refused_at_load(bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="answer_cache_seconds"):
        _profile(answer_cache_seconds=bad)


def test_an_unknown_scope_is_refused_at_load() -> None:
    with pytest.raises(ProfileValidationError, match="answer_cache_scope"):
        _profile(answer_cache_seconds=60, answer_cache_scope="everyone")


def test_the_envelope_marks_an_answer_that_came_from_the_cache() -> None:
    text = PeerResult(peer="billing", status=PeerStatus.OK, text="x", cached=True).render()

    assert 'cached="true"' in text and "session=" not in text
    assert "cached" not in PeerResult(peer="billing", status=PeerStatus.OK, text="x").render()


# --- the workflow ---

ASKED = "What is owed on order 7?"


def _ask(tool_call_id: str = "tc-1", question: str = ASKED, **extra: Any) -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.DELEGATION,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="ask_peer",
        arguments={"target": fanout.PEER, "question": question, **extra},
        reason="asking",
    )


def _bind_cache(cache: InMemoryAnswerCache | None, *, version: str = "") -> None:
    deps = turn_workflow._dependencies  # noqa: SLF001 - the test seam
    assert deps is not None and deps.peers is not None
    seat = dataclasses.replace(deps.peers, answer_cache=cache, answer_cache_version=version)
    turn_workflow._dependencies = dataclasses.replace(deps, peers=seat)  # noqa: SLF001


def _callee_caches(seconds: int = 60, **more: Any) -> None:
    seat = turn_workflow._dependencies.peers  # noqa: SLF001
    assert seat is not None
    peers = dataclasses.replace(
        seat.profiles[fanout.PEER].peers, answer_cache_seconds=seconds, **more
    )
    fanout.callee_profile(peers=peers)


def _turn(
    monkeypatch: pytest.MonkeyPatch,
    cache: InMemoryAnswerCache | None,
    script: list[Any],
    *,
    pending: tuple[PendingRequest, ...] | None = None,
    callee_ttl: int | None = 60,
    version: str = "",
    **callee: Any,
) -> fanout.World:
    world = fanout.install(monkeypatch, pending or (_ask(),), script)
    _bind_cache(cache, version=version)
    if callee_ttl is not None:
        _callee_caches(callee_ttl, **callee)
    asyncio.run(fanout.body()(fanout.request()))
    return world


def _payload(world: fanout.World) -> str:
    ((_, payload),) = world.resume.calls[0]
    return str(payload)


def test_a_repeated_question_within_the_ttl_is_answered_without_a_peer_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance test of C25."""
    cache = InMemoryAnswerCache()

    first = _turn(monkeypatch, cache, [fanout.answer("corr-1", "42 owed")])
    second = _turn(monkeypatch, cache, [])

    assert len(first.mailbox.asks) == 1, "the first ask runs the peer"
    assert second.mailbox.asks == [], "the repeat never reaches the mailbox"
    assert second.recv_timeouts == [], "and nothing waits for a peer"
    assert "42 owed" in _payload(second) and 'cached="true"' in _payload(second)
    assert 'status="ok"' in _payload(second)
    assert "session=" not in _payload(second), "a cached answer opens no peer session"


def test_the_same_question_in_other_words_and_case_hits_the_same_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InMemoryAnswerCache()
    _turn(monkeypatch, cache, [fanout.answer("corr-1", "42 owed")])

    again = _turn(monkeypatch, cache, [], pending=(_ask(question="  what is OWED on order 7  ?"),))

    assert again.mailbox.asks == []


def test_nothing_is_cached_unless_both_the_deployment_and_the_profile_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InMemoryAnswerCache()
    # The callee profile does not set a TTL.
    _turn(monkeypatch, cache, [fanout.answer("corr-1", "x")], callee_ttl=None)
    assert cache.puts == [] and cache.gets == 0, "no TTL on the callee: the cache is not touched"

    # The deployment has no cache bound at all.
    world = _turn(monkeypatch, None, [fanout.answer("corr-1", "x")])
    assert len(world.mailbox.asks) == 1


def test_a_cache_hit_still_goes_through_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer the asker may no longer ask stays refused whatever the cache holds."""
    cache = InMemoryAnswerCache()
    _turn(monkeypatch, cache, [fanout.answer("corr-1", "42 owed")])

    world = fanout.install(monkeypatch, (_ask(),), [])
    _bind_cache(cache)
    _callee_caches()
    fanout.asker_policy(peers=())  # the asker's allowlist no longer names the peer
    reads_before = cache.gets
    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.asks == []
    assert "42 owed" not in _payload(world) and 'status="refused"' in _payload(world)
    assert cache.gets == reads_before, "the cache is not even consulted for a refused ask"


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param("failed", id="a callee that failed"),
        pytest.param("timeout", id="a peer that never answered"),
    ],
)
def test_an_error_or_a_timeout_is_never_remembered(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    cache = InMemoryAnswerCache()
    script = (
        [fanout.answer("corr-1", "boom", failed=True)]
        if outcome == "failed"
        else [fanout.nobody_answers(10_000)]
    )

    _turn(monkeypatch, cache, script)
    assert cache.puts == [], "the bad outcome was not stored"
    again = _turn(monkeypatch, cache, [fanout.answer("corr-1", "fine")])

    assert len(again.mailbox.asks) == 1, "the bad moment was not replayed"
    assert "fine" in _payload(again)


def test_a_different_audience_a_different_version_or_an_expired_entry_is_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    cache = InMemoryAnswerCache(clock=lambda: now[0])
    _turn(monkeypatch, cache, [fanout.answer("corr-1", "42 owed")], version="data-1")

    other_version = _turn(
        monkeypatch, cache, [fanout.answer("corr-1", "43 owed")], version="data-2"
    )
    assert len(other_version.mailbox.asks) == 1, "a new data version does not see the old answer"

    now[0] = 61.0
    expired = _turn(monkeypatch, cache, [fanout.answer("corr-1", "44 owed")], version="data-1")
    assert len(expired.mailbox.asks) == 1, "past the TTL the entry is gone"
    assert [p[2] for p in cache.puts] == [60, 60, 60]


def test_the_default_audience_is_the_asking_subject_and_roles_scope_shares_across_subjects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InMemoryAnswerCache()
    _turn(monkeypatch, cache, [fanout.answer("corr-1", "42 owed")])
    (stored_key, _, _) = cache.puts[0]
    assert stored_key.audience == f"{fanout.ASKER}|subject:u" and stored_key.tenant_id == "t"
    assert stored_key.target == fanout.PEER and stored_key.question == normalize_question(ASKED)

    cache = InMemoryAnswerCache()
    _turn(
        monkeypatch,
        cache,
        [fanout.answer("corr-1", "42 owed")],
        answer_cache_scope=AnswerCacheScope.ROLES,
    )
    assert cache.puts[0][0].audience == f"{fanout.ASKER}|roles:"


def test_continuations_and_background_asks_are_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InMemoryAnswerCache()
    fanout.install(monkeypatch, (_ask(background=True),), [])
    _bind_cache(cache)
    _callee_caches()
    fanout.asker_policy(allow_background=True)
    asyncio.run(fanout.body()(fanout.request()))
    assert cache.gets == 0 and cache.puts == [], "a background ask is delivered later, not cached"

    fanout.install(
        monkeypatch, (_ask(continue_session="peer~s~k"),), [fanout.nobody_answers(10_000)]
    )
    _bind_cache(cache)
    _callee_caches()
    fanout.asker_policy(allow_continuation=True)
    asyncio.run(fanout.body()(fanout.request()))
    assert cache.gets == 0 and cache.puts == [], "a follow-up depends on the earlier exchange"


def test_an_ask_that_carries_the_asking_conversation_is_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `peers.visibility` the peer is told about the conversation, so its answer may depend on
    it and the question alone no longer identifies the answer."""
    from agent_core.domain.peers import PeerVisibility

    class _Context:
        async def render(self, session: Any, mode: Any, max_chars: int) -> str:
            return "the person said hello"

    cache = InMemoryAnswerCache()
    world = fanout.install(monkeypatch, (_ask(),), [fanout.answer("corr-1", "42 owed")])
    _bind_cache(cache)
    _callee_caches()
    fanout.asker_policy(visibility=PeerVisibility.SUMMARY)
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None and deps.peers is not None
    seat = dataclasses.replace(deps.peers, context=_Context())
    turn_workflow._dependencies = dataclasses.replace(deps, peers=seat)  # noqa: SLF001
    asyncio.run(fanout.body()(fanout.request()))

    assert len(world.mailbox.asks) == 1
    assert cache.gets == 0 and cache.puts == []


def test_a_cache_that_fails_costs_a_miss_not_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = InMemoryAnswerCache()
    cache.fail = True

    world = _turn(monkeypatch, cache, [fanout.answer("corr-1", "42 owed")])

    assert len(world.mailbox.asks) == 1 and "42 owed" in _payload(world)


def test_a_cached_answer_is_capped_by_the_asking_profile_like_any_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InMemoryAnswerCache()
    _turn(monkeypatch, cache, [fanout.answer("corr-1", "x" * 500)])

    world = fanout.install(monkeypatch, (_ask(),), [])
    _bind_cache(cache)
    _callee_caches()
    fanout.asker_policy(max_answer_chars=50)
    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.asks == [] and 'truncated="true"' in _payload(world)


def test_a_dispatch_recorded_before_the_cache_existed_is_still_readable() -> None:
    """A workflow waiting on peers across the deploy replays the PeerDispatch it recorded then,
    which has no `cached` or `remember` slot. Reading it must not raise (no version bump needed)."""
    old = object.__new__(turn_workflow.PeerDispatch)
    object.__setattr__(old, "handles", (("corr-1", ToolCallId("tc-1")),))
    object.__setattr__(old, "refusals", ())
    object.__setattr__(old, "started", ())

    assert turn_workflow._cached_results(old) == ()  # noqa: SLF001
    assert turn_workflow._remembered(old) == {}  # noqa: SLF001


# --- the deployment's switch and the wiring ---

_NO_FILE = Path("/nonexistent/.env")


def test_the_cache_is_off_unless_the_deployment_switches_it_on() -> None:
    off = Settings.from_env({}, env_file=_NO_FILE)
    on = Settings.from_env(
        {
            "AGENT_CORE_PEER_ANSWER_CACHE": "true",
            "AGENT_CORE_PEER_CACHE_DATA_VERSION": "2026-10-12-load",
        },
        env_file=_NO_FILE,
    )

    assert off.peer_answer_cache is False and off.peer_cache_data_version == ""
    assert on.peer_answer_cache is True and on.peer_cache_data_version == "2026-10-12-load"
    with pytest.raises(ValueError, match="AGENT_CORE_PEER_ANSWER_CACHE"):
        Settings.from_env({"AGENT_CORE_PEER_ANSWER_CACHE": "maybe"}, env_file=_NO_FILE)


def _container(**settings: Any) -> Any:
    from agent_core import composition
    from tests.unit.test_composition import _RecordingPool

    resolved = dataclasses.replace(Settings.from_env({}, env_file=_NO_FILE), **settings)
    return composition.build_container(resolved, pool_factory=_RecordingPool)  # type: ignore[arg-type]


def test_the_workflow_seat_has_no_cache_unless_the_deployment_switched_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core import composition

    monkeypatch.setattr(turn_workflow, "_dependencies", None)
    composition.bind_turn_workflow(_container())
    seat = turn_workflow._dependencies.peers  # noqa: SLF001
    assert seat is not None and seat.answer_cache is None

    composition.bind_turn_workflow(_container(peer_answer_cache=True, peer_cache_data_version="v7"))
    seat = turn_workflow._dependencies.peers  # noqa: SLF001
    assert seat is not None and isinstance(seat.answer_cache, PgAnswerCache)
    assert seat.answer_cache_version == "v7"


# --- the operator's invalidation hook ---


def _purge(argv: list[str], cache: InMemoryAnswerCache) -> tuple[int, list[str]]:
    from agent_core.adapters.driving.cli.peer_cache import run_peer_cache
    from agent_core.main import _argument_parser

    said: list[str] = []
    arguments = _argument_parser().parse_args(["peer-cache", *argv])
    code = run_peer_cache(
        arguments, Settings.from_env({}, env_file=_NO_FILE), cache=cache, out=said.append
    )
    return code, said


def test_the_operator_forgets_a_tenants_answers_or_one_targets_and_is_told_how_many() -> None:
    cache = InMemoryAnswerCache()
    for tenant, target in (("t", "billing"), ("t", "support"), ("other", "billing")):
        asyncio.run(
            cache.put(
                AnswerCacheKey(tenant, target, "a|subject:u", "q", "v"), CachedAnswer("x"), 60
            )
        )

    code, said = _purge(["purge", "--tenant", "t", "--target", "billing"], cache)
    assert code == 0 and "forgot 1 " in said[0] and "billing" in said[0]

    code, said = _purge(["purge", "--tenant", "t"], cache)
    assert code == 0 and "forgot 1 " in said[0]

    code, said = _purge(["purge", "--tenant", "t"], cache)
    assert code == 0 and "forgot 0 " in said[0], "nothing to forget is not an error"
    assert asyncio.run(cache.invalidate("other")) == 1, "another tenant's entry was untouched"


def test_a_purge_needs_a_tenant() -> None:
    from agent_core.main import _argument_parser

    with pytest.raises(SystemExit):
        _argument_parser().parse_args(["peer-cache", "purge"])


def test_an_unreachable_cache_is_reported_not_called_clean() -> None:
    cache = InMemoryAnswerCache()
    cache.fail = True

    async def broken(tenant_id: str, target: str | None = None) -> int:
        raise RuntimeError("down")

    cache.invalidate = broken  # type: ignore[method-assign]
    code, said = _purge(["purge", "--tenant", "t"], cache)

    assert code == 2 and "could not reach" in said[0]
