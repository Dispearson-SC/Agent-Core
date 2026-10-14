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
import threading
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError, ManifestError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.manifest import parse_manifest
from agent_core.adapters.driven.addons_fs.registration import EMPTY, register_addons
from agent_core.adapters.driven.routing.addon_router import AddonTurnRouter, RouterSaturatedError
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
        # Both threads still hang: a third call is refused at once, never queued behind them.
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
