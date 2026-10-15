"""`provides.routers`: an addon ships a turn router, registered like toolsets and wrappers (D95).

Subject: the manifest's `provides.routers`, its registration, the addon-backed `TurnRouter`
(adapters/driven/routing/addon_router.py), the `AGENT_CORE_TURN_ROUTING*` settings and the
wiring in composition.py.

THE RULES UNDER TEST. A router is `routers/<name>.py` in the addon with a `route(request)` or
`route(request, settings)` function (sync or async) that reads a plain mapping and answers a plain
mapping; the addon imports nothing of Core. Names are global and a collision stops startup naming
the addon. Routing is off unless `AGENT_CORE_TURN_ROUTING` is set: off, no router is consulted
and the container has no `route_turn`; on, a profile naming an unknown router or a target that is
not a loaded entry point stops startup.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError, ManifestError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.manifest import parse_manifest
from agent_core.adapters.driven.addons_fs.registration import EMPTY, register_addons
from agent_core.adapters.driven.routing.addon_router import (
    AddonTurnRouter,
    RouterSaturatedError,
    decision_from_mapping,
)
from agent_core.composition import TOOL_PACKAGES, Settings, refuse_unroutable_profiles
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import RouteDecision, RouteKind, RouteRequest
from tests.contract.addon_boundary import scan_directory

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "addons"
_NO_ENV_FILE = Path("/nonexistent/.env")


def _register(root: Path, enabled: list[str], **load: Any) -> Any:
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0", **load)
    return register_addons(
        addons,
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=("http",),
    )


def _addon(root: Path, name: str, router: str, source: str) -> None:
    directory = root / name
    (directory / "routers").mkdir(parents=True)
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\nprovides:\n  routers: [{router}]\n",
        encoding="utf-8",
    )
    (directory / "routers" / f"{router}.py").write_text(dedent(source), encoding="utf-8")


def _request(text: str = "where is my invoice") -> RouteRequest:
    return RouteRequest(text=text, profile_id="front_desk", channel="http", tenant_id="t")


# --- the manifest ------------------------------------------------------------------------------


def test_the_manifest_declares_routers_by_name_and_refuses_a_duplicate() -> None:
    base = "name: a\nversion: 1.0.0\ncore: '>=0'\nprovides:\n"
    assert parse_manifest(base + "  routers: [keyword]\n").provides.routers == ("keyword",)
    assert parse_manifest(base + "  toolsets: []\n").provides.routers == ()
    with pytest.raises(ManifestError):
        parse_manifest(base + "  routers: [keyword, keyword]\n")
    with pytest.raises(ManifestError):
        parse_manifest(base + "  routers: [Not A Name]\n")


# --- registration ------------------------------------------------------------------------------


def test_an_enabled_router_addon_registers_its_router_and_no_addon_registers_none() -> None:
    contributions = _register(FIXTURES, ["keyword_router"])

    assert set(contributions.routers) == {"keyword"}
    assert EMPTY.routers == {}


def test_the_router_addon_never_reaches_core_inner_layers() -> None:
    scanned, violations = scan_directory(FIXTURES / "keyword_router")
    assert scanned >= 1 and violations == []


def test_a_router_without_its_file_or_its_function_stops_startup_naming_the_addon(
    tmp_path: Path,
) -> None:
    _addon(tmp_path, "no_function", "keyword", "VALUE = 1\n")
    with pytest.raises(AddonRegistrationError) as caught:
        _register(tmp_path, ["no_function"])
    assert "no_function" in str(caught.value) and "route(" in str(caught.value)

    missing = tmp_path / "no_file"
    missing.mkdir()
    (missing / "addon.yaml").write_text(
        "name: no_file\nversion: 1.0.0\ncore: '>=0'\nprovides:\n  routers: [gone]\n",
        encoding="utf-8",
    )
    with pytest.raises(AddonRegistrationError):
        _register(tmp_path, ["no_file"])


def test_two_addons_providing_one_router_name_stop_startup(tmp_path: Path) -> None:
    source = "def route(request):\n    return {'decision': 'pass'}\n"
    _addon(tmp_path, "first", "keyword", source)
    _addon(tmp_path, "second", "keyword", source)

    with pytest.raises(AddonRegistrationError) as caught:
        _register(tmp_path, ["first", "second"])
    assert "second" in str(caught.value) and "first" in str(caught.value)


# --- the addon-backed TurnRouter ---------------------------------------------------------------


def test_the_fixture_routes_on_its_keyword_with_its_settings_and_passes_otherwise() -> None:
    router = AddonTurnRouter(_register(FIXTURES, ["keyword_router"]).routers)

    routed = asyncio.run(router.route("keyword", _request("My INVOICE is wrong")))
    passed = asyncio.run(router.route("keyword", _request("hello")))

    assert routed == RouteDecision(
        kind=RouteKind.ROUTE, profile_id="billing", label="invoice", confidence=1.0,
        calibrated=False, model="keyword",
    )
    assert passed.kind is RouteKind.PASS and passed.label == "no_match"


def test_an_async_router_receives_the_request_as_a_plain_mapping(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "echoing",
        "echo",
        """
        SEEN = []

        async def route(request):
            SEEN.append(request)
            return {"decision": "intent", "intent": "refund", "arguments": {"order": "7"}}
        """,
    )
    contributions = _register(tmp_path, ["echoing"])
    router = AddonTurnRouter(contributions.routers)

    decision = asyncio.run(router.route("echo", _request()))

    assert decision.kind is RouteKind.INTENT and dict(decision.arguments) == {"order": "7"}
    module = __import__("sys").modules["agent_core_addons.echoing.routers.echo"]
    assert module.SEEN == [
        {
            "text": "where is my invoice",
            "profile_id": "front_desk",
            "channel": "http",
            "tenant_id": "t",
            "modality": None,
        }
    ]
    assert type(module.SEEN[0]) is dict


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "route",
        {"decision": "jump"},
        {"decision": "route"},  # no profile
        {"decision": "pass", "confidence": "high"},
        {"decision": "pass", "unexpected": 1},
        {"decision": "pass", "calibrated": "yes"},
    ],
)
def test_a_malformed_answer_raises_so_the_caller_falls_back(tmp_path: Path, answer: Any) -> None:
    _addon(tmp_path, "odd", "odd", f"def route(request):\n    return {answer!r}\n")
    router = AddonTurnRouter(_register(tmp_path, ["odd"]).routers)

    with pytest.raises((ValueError, TypeError)):
        asyncio.run(router.route("odd", _request()))


def test_an_unknown_router_name_raises_key_error() -> None:
    with pytest.raises(KeyError):
        asyncio.run(AddonTurnRouter({}).route("nobody", _request()))


# --- settings ----------------------------------------------------------------------------------


def test_routing_is_off_by_default_with_a_1500_ms_router_timeout() -> None:
    settings = Settings.from_env({}, env_file=_NO_ENV_FILE)
    assert settings.turn_routing is False
    assert settings.turn_router_timeout_ms == 1500


def test_the_routing_switch_and_timeout_are_read_strictly() -> None:
    on = Settings.from_env(
        {"AGENT_CORE_TURN_ROUTING": "true", "AGENT_CORE_TURN_ROUTER_TIMEOUT_MS": "250"},
        env_file=_NO_ENV_FILE,
    )
    assert (on.turn_routing, on.turn_router_timeout_ms) == (True, 250)
    for bad in ({"AGENT_CORE_TURN_ROUTING": "maybe"}, {"AGENT_CORE_TURN_ROUTER_TIMEOUT_MS": "0"}):
        with pytest.raises(ValueError):
            Settings.from_env(bad, env_file=_NO_ENV_FILE)


def test_follow_ups_are_not_routed_unless_the_deployment_says_so() -> None:
    assert Settings.from_env({}, env_file=_NO_ENV_FILE).turn_routing_follow_ups is False
    on = Settings.from_env({"AGENT_CORE_TURN_ROUTING_FOLLOW_UPS": "1"}, env_file=_NO_ENV_FILE)
    assert on.turn_routing_follow_ups is True
    with pytest.raises(ValueError):
        Settings.from_env({"AGENT_CORE_TURN_ROUTING_FOLLOW_UPS": "y"}, env_file=_NO_ENV_FILE)


# --- the startup check -------------------------------------------------------------------------


def _profile(pid: str, *, routing: dict[str, Any] | None = None, mode: str = "all") -> Any:
    data: dict[str, Any] = {"id": pid, "persona": "p", "model": "m", "mode": mode}
    if routing is not None:
        data["routing"] = routing
    return AgentProfile.from_mapping(data)


def _front(router: str, target: str) -> Any:
    return _profile("front", routing={"router": router, "targets": [target]})


def test_a_routing_profile_must_name_a_registered_router_and_entry_point_targets() -> None:
    good = {"front": _front("keyword", "billing"), "billing": _profile("billing")}
    refuse_unroutable_profiles(good, frozenset({"keyword"}))

    with pytest.raises(ValueError, match="front.*nobody"):
        refuse_unroutable_profiles(
            {**good, "front": _front("nobody", "billing")},
            frozenset({"keyword"}),
        )
    with pytest.raises(ValueError, match="front.*ghost"):
        refuse_unroutable_profiles(
            {**good, "front": _front("keyword", "ghost")},
            frozenset({"keyword"}),
        )
    with pytest.raises(ValueError, match="front.*billing"):
        refuse_unroutable_profiles(
            {**good, "billing": _profile("billing", mode=AgentMode.PEER.value)},
            frozenset({"keyword"}),
        )


# --- sync routers that hang --------------------------------------------------------------------


def test_sync_routers_hanging_past_the_timeout_cannot_exhaust_threads_and_then_recover() -> None:
    release = threading.Event()
    calls: list[dict[str, Any]] = []

    def hang(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        release.wait(5)
        return {"decision": "pass"}

    router = AddonTurnRouter({"hang": hang}, max_sync_calls=2)

    async def scenario() -> RouteDecision:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(router.route("hang", _request()), 0.05)
        # Both threads still hang: a third call waits briefly for a slot, then is refused.
        with pytest.raises(RouterSaturatedError):
            await asyncio.wait_for(router.route("hang", _request()), 1.0)
        assert len(calls) == 2 and router.sync_calls_in_flight == 2
        release.set()
        for _ in range(200):
            if router.sync_calls_in_flight == 0:
                break
            await asyncio.sleep(0.01)
        return await router.route("hang", _request())

    assert asyncio.run(scenario()).kind is RouteKind.PASS
    assert router.sync_calls_in_flight == 0


def test_a_sync_routers_error_reaches_the_caller_and_frees_its_slot() -> None:
    def broken(request: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("refused")

    router = AddonTurnRouter({"broken": broken}, max_sync_calls=1)

    for _ in range(3):
        with pytest.raises(ConnectionError):
            asyncio.run(router.route("broken", _request()))
    assert router.sync_calls_in_flight == 0


def test_the_sync_call_cap_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_sync_calls"):
        AddonTurnRouter({}, max_sync_calls=0)


# --- slice 34 advisories: context and load ----------------------------------------------------


_REQUEST_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("request_tag", default="unset")


def test_a_sync_router_sees_the_callers_context_variables() -> None:
    seen: list[str] = []

    def tagged(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(_REQUEST_TAG.get())
        return {"decision": "pass"}

    router = AddonTurnRouter({"tagged": tagged})

    async def scenario() -> None:
        _REQUEST_TAG.set("turn-42")
        await router.route("tagged", _request())

    asyncio.run(scenario())
    assert seen == ["turn-42"]


def test_a_burst_of_fast_sync_calls_over_the_cap_waits_for_a_slot_instead_of_failing() -> None:
    def quick(request: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.02)
        return {"decision": "pass"}

    router = AddonTurnRouter({"quick": quick}, max_sync_calls=2)

    async def scenario() -> list[RouteDecision]:
        return await asyncio.gather(*(router.route("quick", _request()) for _ in range(6)))

    decisions = asyncio.run(scenario())
    assert [d.kind for d in decisions] == [RouteKind.PASS] * 6
    assert router.sync_calls_in_flight == 0


def test_a_call_waiting_for_a_slot_gives_up_after_the_slot_wait() -> None:
    release = threading.Event()

    def hang(request: dict[str, Any]) -> dict[str, Any]:
        release.wait(5)
        return {"decision": "pass"}

    router = AddonTurnRouter({"hang": hang}, max_sync_calls=1, slot_wait_seconds=0.05)

    async def scenario() -> float:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(router.route("hang", _request()), 0.02)
        started = time.monotonic()
        with pytest.raises(RouterSaturatedError):
            await router.route("hang", _request())
        return time.monotonic() - started

    try:
        waited = asyncio.run(scenario())
    finally:
        release.set()
    assert 0.04 <= waited < 1.0


def test_the_slot_wait_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="slot_wait_seconds"):
        AddonTurnRouter({}, slot_wait_seconds=-1)


def test_an_answer_may_carry_its_reason_elapsed_time_and_usage_which_are_logged_not_decided(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def diagnosed(request: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision": "pass",
            "label": "none_of_these",
            "confidence": 0.31,
            "model": "clf-2",
            "reason": "below_threshold",
            "elapsed_ms": 123,
            "usage": {"input_tokens": 300, "output_tokens": 20},
        }

    router = AddonTurnRouter({"diagnosed": diagnosed})
    with caplog.at_level(logging.INFO, logger="agent_core.adapters.driven.routing.addon_router"):
        decision = asyncio.run(router.route("diagnosed", _request("secret customer words")))

    assert decision.kind is RouteKind.PASS and decision.label == "none_of_these"
    # L12: the reason and the usage reach the decision, so they are audited, not only logged.
    assert decision.reason == "below_threshold"
    assert decision.usage == {"input_tokens": 300, "output_tokens": 20}
    logged = caplog.text
    assert "below_threshold" in logged and "elapsed_ms=123" in logged
    assert "input_tokens" in logged and "secret customer words" not in logged


@pytest.mark.parametrize(
    "answer",
    [
        {"decision": "pass", "reason": 7},
        {"decision": "pass", "reason": "x" * 201},
        {"decision": "pass", "elapsed_ms": -1},
        {"decision": "pass", "elapsed_ms": True},
        {"decision": "pass", "usage": ["tokens"]},
        {"decision": "pass", "usage": {"input_tokens": "many"}},
        # Slice 36 warning: a count the audit column cannot hold, or a usage map that grows with
        # whatever the router said, is malformed here, not an audit write that fails later and
        # leaves the route unapplied for a reason that names the audit.
        {"decision": "pass", "usage": {"input_tokens": 2**31}},
        {"decision": "pass", "usage": {"input_tokens": -1}},
        {"decision": "pass", "usage": {f"k{i}": 1 for i in range(17)}},
        {"decision": "pass", "usage": {"x" * 65: 1}},
    ],
)
def test_malformed_diagnostics_are_a_malformed_answer(answer: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        decision_from_mapping(answer)


# --- S10: the fast path's extra questions --------------------------------------------------------


def test_a_fast_path_request_carries_the_flag_and_the_conversation_and_off_carries_neither(
    tmp_path: Path,
) -> None:
    _addon(
        tmp_path,
        "listening",
        "listen",
        """
        SEEN = []

        def route(request):
            SEEN.append(request)
            return {"decision": "pass"}
        """,
    )
    router = AddonTurnRouter(_register(tmp_path, ["listening"]).routers)
    fast = RouteRequest(
        text="and what do I order?",
        profile_id="front_desk",
        channel="http",
        tenant_id="t",
        fast_path=True,
        history=("person: sales?", "agent: 1,200"),
    )

    asyncio.run(router.route("listen", _request()))
    asyncio.run(router.route("listen", fast))

    module = __import__("sys").modules["agent_core_addons.listening.routers.listen"]
    assert "fast_path" not in module.SEEN[0] and "history" not in module.SEEN[0]
    assert module.SEEN[1]["fast_path"] is True
    assert module.SEEN[1]["history"] == ["person: sales?", "agent: 1,200"]


def test_an_answer_may_carry_the_fast_path_assessment() -> None:
    decision = decision_from_mapping(
        {
            "decision": "pass",
            "assessment": {
                "intents": {"order_plan": 0.81, "day_summary": 0.93},
                "action": 0.02,
                "follow_up": None,
                "complexity": 0.25,
            },
        }
    )
    assert decision.assessment is not None
    assert decision.assessment.intents == (("day_summary", 0.93), ("order_plan", 0.81))
    assert (decision.assessment.action, decision.assessment.follow_up) == (0.02, None)
    assert decision.assessment.complexity == 0.25
    assert decision_from_mapping({"decision": "pass"}).assessment is None


@pytest.mark.parametrize(
    "assessment",
    [
        "yes",
        {"intents": {"a": 1.5}},
        {"intents": {"a": "high"}},
        {"intents": ["a"]},
        {"intents": {}, "action": True},
        {"intents": {}, "mood": 0.1},
        {"intents": {"x" * 65: 0.1}},
        {"intents": {}, "requests": 0},
        {"intents": {}, "requests": 1.5},
        {"intents": {}, "requests": True},
        {"intents": {}, "requests": "2"},
    ],
)
def test_a_malformed_assessment_is_a_malformed_answer(assessment: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        decision_from_mapping({"decision": "pass", "assessment": assessment})


def test_an_assessment_may_count_the_requests_of_the_message() -> None:
    decision = decision_from_mapping(
        {
            "decision": "pass",
            "assessment": {"intents": {"a": 0.9}, "action": 0.1, "complexity": 0.0, "requests": 2},
        }
    )
    assert decision.assessment is not None and decision.assessment.requests == 2
    plain = decision_from_mapping(
        {"decision": "pass", "assessment": {"intents": {}, "action": 0.1, "complexity": 0.0}}
    )
    assert plain.assessment is not None and plain.assessment.requests is None
