"""`addons/system_one_router`: a turn router that classifies with a System One model (Typesafe).

Subject: addons/system_one_router/routers/system_one.py, its manifest and its routes file format.

THE RULES UNDER TEST. The router asks `POST <base_url>/v1/systemone` with `Authorization: Bearer
<key>` (the key read from the environment variable `api_key_env` names, at call time) one `choice`
question whose options are the configured routes plus an escape option. The answer's choice maps
onto the route's target (`profile:<id>` -> route, `intent:<name>` -> intent). The escape option, a
confidence below `threshold`, a missing key, an HTTP error, a timeout or a malformed response is a
`pass` with its reason: the normal path runs. 429 and 529 are retried once, only when the retry
fits the timeout. Every answer records the label, the confidence, calibrated=false, the model, the
elapsed time and the token usage. The key is never in an answer. No network: httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.routing.addon_router import AddonTurnRouter, decision_from_mapping
from agent_core.composition import TOOL_PACKAGES
from agent_core.domain.routing import RouteKind, RouteRequest
from tests.contract.addon_boundary import scan_directory

ADDONS = Path(__file__).resolve().parents[3] / "addons"
ADDON = ADDONS / "system_one_router"
KEY = "ts-test-key-do-not-echo"

ROUTES = """\
instructions: Which team should answer this message?
routes:
  - label: billing
    description: Invoices, refunds and payments
    target: profile:billing
  - label: opening_hours
    description: When the shop is open
    target: intent:opening_hours
"""


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "system_one_router_under_test", ADDON / "routers" / "system_one.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    return module


SYSTEM_ONE = _module()


def _settings(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    routes = tmp_path / "routes.yaml"
    if not routes.exists():
        routes.write_text(ROUTES, encoding="utf-8")
    settings: dict[str, Any] = {
        "api_key_env": "TYPESAFE_API_KEY",
        "base_url": "https://api.typesafe.test",
        "model": "jev-latest",
        "timeout_ms": 800,
        "threshold": 0.5,
        "escape_label": "none_of_these",
        "escape_description": "Fits no other option",
        "instructions": "Which option fits?",
        "routes_file": str(routes),
        "state_fields": ["text"],
        "retry_backoff_ms": 50,
    }
    settings.update(overrides)
    return settings


def _request(text: str = "where is my invoice") -> dict[str, Any]:
    return {
        "text": text,
        "profile_id": "front_desk",
        "channel": "http",
        "tenant_id": "t1",
        "modality": None,
    }


def _answer(choice: str, confidence: float, *, model: str = "jev-1.13.0") -> dict[str, Any]:
    return {
        "model": model,
        "answers": {
            "route": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence},
            }
        },
        "usage": {"input_tokens": 318, "output_tokens": 34},
    }


Handler = Callable[[httpx.Request], Any]


def _route(
    handler: Handler,
    settings: dict[str, Any],
    *,
    text: str = "where is my invoice",
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            answer: dict[str, Any] = await SYSTEM_ONE.route(
                _request(text),
                settings,
                client=client,
                environ={"TYPESAFE_API_KEY": KEY} if environ is None else environ,
            )
            return answer

    return asyncio.run(scenario())


def _replying(*responses: httpx.Response, seen: list[httpx.Request] | None = None) -> Handler:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


# --- the request -------------------------------------------------------------------------------


def test_the_request_is_one_choice_question_over_the_routes_and_the_escape_option(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    _route(
        _replying(httpx.Response(200, json=_answer("billing", 0.9)), seen=seen), _settings(tmp_path)
    )

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.test/v1/systemone"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest"
    assert body["state"] == "where is my invoice"
    assert body["questions"] == {
        "route": {
            "type": "choice",
            # the routes file's instructions win over the setting
            "instructions": "Which team should answer this message?",
            "criteria": {
                "billing": "Invoices, refunds and payments",
                "opening_hours": "When the shop is open",
                "none_of_these": "Fits no other option",
            },
        }
    }


def test_the_state_carries_the_configured_fields_as_an_object(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    settings = _settings(tmp_path, state_fields=["text", "channel", "profile_id"])

    _route(_replying(httpx.Response(200, json=_answer("billing", 0.9)), seen=seen), settings)

    assert json.loads(seen[0].content)["state"] == {
        "text": "where is my invoice",
        "channel": "http",
        "profile_id": "front_desk",
    }


def test_a_base_url_with_a_trailing_slash_still_reaches_the_endpoint(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    settings = _settings(tmp_path, base_url="https://api.typesafe.test/")

    _route(_replying(httpx.Response(200, json=_answer("billing", 0.9)), seen=seen), settings)

    assert str(seen[0].url) == "https://api.typesafe.test/v1/systemone"


# --- the decision ------------------------------------------------------------------------------


def test_a_confident_profile_route_routes_and_records_what_the_classifier_said(
    tmp_path: Path,
) -> None:
    answer = _route(
        _replying(httpx.Response(200, json=_answer("billing", 0.81))), _settings(tmp_path)
    )

    assert answer["decision"] == "route" and answer["profile_id"] == "billing"
    assert answer["label"] == "billing" and answer["confidence"] == 0.81
    assert answer["calibrated"] is False and answer["model"] == "jev-1.13.0"
    assert answer["usage"] == {"input_tokens": 318, "output_tokens": 34}
    assert isinstance(answer["elapsed_ms"], int) and answer["elapsed_ms"] >= 0
    assert "reason" not in answer
    decision = decision_from_mapping(answer)  # Core accepts it as it is
    assert decision.kind is RouteKind.ROUTE and decision.profile_id == "billing"


def test_a_confident_intent_route_answers_the_intent(tmp_path: Path) -> None:
    answer = _route(
        _replying(httpx.Response(200, json=_answer("opening_hours", 0.97))), _settings(tmp_path)
    )

    assert answer["decision"] == "intent" and answer["intent"] == "opening_hours"
    assert decision_from_mapping(answer).kind is RouteKind.INTENT


def test_the_escape_option_passes_whatever_its_confidence(tmp_path: Path) -> None:
    answer = _route(
        _replying(httpx.Response(200, json=_answer("none_of_these", 0.99))), _settings(tmp_path)
    )

    assert answer["decision"] == "pass" and answer["reason"] == "escape"
    assert answer["label"] == "none_of_these" and answer["confidence"] == 0.99


def test_a_choice_below_the_threshold_passes_and_keeps_its_label(tmp_path: Path) -> None:
    answer = _route(
        _replying(httpx.Response(200, json=_answer("billing", 0.49))), _settings(tmp_path)
    )

    assert answer["decision"] == "pass" and answer["reason"] == "below_threshold"
    assert answer["label"] == "billing" and answer["confidence"] == 0.49


def test_the_threshold_is_a_setting(tmp_path: Path) -> None:
    answer = _route(
        _replying(httpx.Response(200, json=_answer("billing", 0.49))),
        _settings(tmp_path, threshold=0.3),
    )

    assert answer["decision"] == "route"


# --- failures pass, with the reason --------------------------------------------------------------


def test_without_the_key_in_the_environment_nothing_is_sent(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    answer = _route(
        _replying(httpx.Response(200, json=_answer("billing", 0.9)), seen=seen),
        _settings(tmp_path),
        environ={},
    )

    assert seen == [] and answer == {
        "decision": "pass",
        "reason": "missing_api_key",
        "calibrated": False,
        "elapsed_ms": answer["elapsed_ms"],
    }


def test_the_key_is_read_from_the_variable_the_settings_name(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    _route(
        _replying(httpx.Response(200, json=_answer("billing", 0.9)), seen=seen),
        _settings(tmp_path, api_key_env="JEV_API_KEY"),
        environ={"JEV_API_KEY": "other-key"},
    )

    assert seen[0].headers["Authorization"] == "Bearer other-key"


@pytest.mark.parametrize("status", [401, 403, 422, 500])
def test_an_http_error_passes_with_its_status_and_never_retries(
    tmp_path: Path, status: int
) -> None:
    seen: list[httpx.Request] = []

    answer = _route(
        _replying(httpx.Response(status, json={"detail": "nope"}), seen=seen), _settings(tmp_path)
    )

    assert len(seen) == 1
    assert answer["decision"] == "pass" and answer["reason"] == f"http_{status}"


@pytest.mark.parametrize("status", [429, 529])
def test_a_busy_answer_is_retried_once_when_the_retry_fits_the_timeout(
    tmp_path: Path, status: int
) -> None:
    seen: list[httpx.Request] = []

    answer = _route(
        _replying(
            httpx.Response(status), httpx.Response(200, json=_answer("billing", 0.9)), seen=seen
        ),
        _settings(tmp_path),
    )

    assert len(seen) == 2 and answer["decision"] == "route"


def test_a_busy_answer_twice_passes_after_the_one_retry(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    answer = _route(_replying(httpx.Response(429), seen=seen), _settings(tmp_path))

    assert len(seen) == 2 and answer["reason"] == "http_429"


def test_a_busy_answer_is_not_retried_when_the_retry_would_not_fit(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    answer = _route(
        _replying(httpx.Response(529), seen=seen),
        _settings(tmp_path, timeout_ms=100, retry_backoff_ms=200),
    )

    assert len(seen) == 1 and answer["reason"] == "http_529"


def test_a_slow_classifier_passes_at_the_timeout(tmp_path: Path) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200, json=_answer("billing", 0.9))

    async def scenario() -> tuple[dict[str, Any], float]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            answer = await SYSTEM_ONE.route(
                _request(),
                _settings(tmp_path, timeout_ms=50),
                client=client,
                environ={"TYPESAFE_API_KEY": KEY},
            )
        return answer, loop.time() - started

    answer, took = asyncio.run(scenario())
    assert answer["decision"] == "pass" and answer["reason"] == "timeout"
    assert took < 1.0


def test_a_network_error_passes_naming_its_class_not_its_message(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused with {request.headers['Authorization']}")

    answer = _route(refuse, _settings(tmp_path))

    assert answer["reason"] == "network_error:ConnectError"
    assert KEY not in json.dumps(answer)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        {"model": "m", "answers": {}},
        {"model": "m", "answers": {"route": {"choice": "billing"}}},
        {"model": "m", "answers": {"route": {"choice": "billing", "confidence": 1.5}}},
        {"model": "m", "answers": {"route": {"choice": "billing", "confidence": True}}},
        {"model": "m", "answers": {"route": {"choice": "refunds", "confidence": 0.9}}},
        {"model": 7, "answers": {"route": {"choice": "billing", "confidence": 0.9}}},
    ],
)
def test_a_malformed_response_passes(tmp_path: Path, body: object) -> None:
    response = (
        httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
    )

    answer = _route(_replying(response), _settings(tmp_path))

    assert answer["decision"] == "pass" and answer["reason"] == "bad_response"
    decision_from_mapping(answer)  # still an answer Core accepts


def test_usage_keeps_only_whole_number_counts(tmp_path: Path) -> None:
    body = _answer("billing", 0.9)
    body["usage"] = {"input_tokens": 10, "note": "x", "cached": True}

    answer = _route(_replying(httpx.Response(200, json=body)), _settings(tmp_path))

    assert answer["usage"] == {"input_tokens": 10}


def test_the_key_never_appears_in_any_answer(tmp_path: Path) -> None:
    for response in (
        httpx.Response(200, json=_answer("billing", 0.9)),
        httpx.Response(401, json={"detail": f"bad key {KEY}"}),
    ):
        assert KEY not in json.dumps(_route(_replying(response), _settings(tmp_path)))


# --- the routes file -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        ("routes: []\n", "at least one route"),
        (
            "routes:\n  - {label: a, description: x, target: 'profile:a'}\n"
            "  - {label: a, description: y, target: 'profile:b'}\n",
            "twice",
        ),
        ("routes:\n  - {label: none_of_these, description: x, target: 'profile:a'}\n", "escape"),
        ("routes:\n  - {label: a, description: x, target: 'agent:a'}\n", "target"),
        ("routes:\n  - {label: 'A b', description: x, target: 'profile:a'}\n", "label"),
        ("routes:\n  - {label: a, target: 'profile:a'}\n", "description"),
        ("routes:\n  - {label: a, description: x, target: 'profile:a', extra: 1}\n", "extra"),
        ("other: 1\n", "routes"),
    ],
)
def test_a_malformed_routes_file_raises_naming_the_problem(
    tmp_path: Path, content: str, complaint: str
) -> None:
    (tmp_path / "routes.yaml").write_text(content, encoding="utf-8")

    with pytest.raises(SYSTEM_ONE.SystemOneConfigError, match=complaint):
        _route(_replying(httpx.Response(200, json=_answer("a", 0.9))), _settings(tmp_path))


def test_a_missing_routes_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SYSTEM_ONE.SystemOneConfigError, match="routes file"):
        _route(
            _replying(httpx.Response(200)),
            _settings(tmp_path, routes_file=str(tmp_path / "absent.yaml")),
        )


def test_an_unknown_state_field_raises(tmp_path: Path) -> None:
    with pytest.raises(SYSTEM_ONE.SystemOneConfigError, match="state_fields"):
        _route(_replying(httpx.Response(200)), _settings(tmp_path, state_fields=["secret"]))


def test_a_relative_routes_file_is_read_from_the_addons_root(tmp_path: Path) -> None:
    root = tmp_path / "addons"
    (root / "a_vertical").mkdir(parents=True)
    (root / "a_vertical" / "routes.yaml").write_text(ROUTES, encoding="utf-8")

    assert SYSTEM_ONE.resolve_routes_file("a_vertical/routes.yaml", addons_root=root) == (
        root / "a_vertical" / "routes.yaml"
    )
    assert SYSTEM_ONE.resolve_routes_file(str(tmp_path / "x.yaml"), addons_root=root) == (
        tmp_path / "x.yaml"
    )


# --- the addon in Core ---------------------------------------------------------------------------


def test_the_addon_registers_its_router_with_documented_defaults_and_imports_nothing_of_core(
    tmp_path: Path,
) -> None:
    routes = tmp_path / "routes.yaml"
    routes.write_text(ROUTES, encoding="utf-8")
    addons = load_addons(
        enabled=["system_one_router"],
        addons_root=ADDONS,
        core_version="0.0.0",
        env={"AGENT_CORE_ADDON__SYSTEM_ONE_ROUTER__ROUTES_FILE": str(routes)},
    )
    settings = dict(addons[0].settings)
    assert settings["api_key_env"] == "TYPESAFE_API_KEY"
    assert settings["base_url"] == "https://api.typesafe.ai"
    assert settings["model"] == "jev-latest"
    assert settings["timeout_ms"] == 800 and settings["threshold"] == 0.5
    assert list(settings["state_fields"]) == ["text"]

    contributions = register_addons(
        addons, builtin_toolsets=TOOL_PACKAGES, builtin_fragments_dir=None, reserved_channels=()
    )
    assert "system_one" in contributions.routers
    scanned, violations = scan_directory(ADDON)
    assert scanned >= 1 and violations == []
    words = (ADDON / "addon.yaml").read_text() + (ADDON / "routers" / "system_one.py").read_text()
    assert not {"glazed", "retail", "store"} & set(words.lower().replace("_", " ").split())


def test_through_core_without_a_key_the_turn_takes_the_normal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = tmp_path / "routes.yaml"
    routes.write_text(ROUTES, encoding="utf-8")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    addons = load_addons(
        enabled=["system_one_router"],
        addons_root=ADDONS,
        core_version="0.0.0",
        env={"AGENT_CORE_ADDON__SYSTEM_ONE_ROUTER__ROUTES_FILE": str(routes)},
    )
    contributions = register_addons(
        addons, builtin_toolsets=TOOL_PACKAGES, builtin_fragments_dir=None, reserved_channels=()
    )
    router = AddonTurnRouter(contributions.routers)

    decision = asyncio.run(
        router.route(
            "system_one",
            RouteRequest(text="hello", profile_id="front_desk", channel="http", tenant_id="t"),
        )
    )

    assert decision.kind is RouteKind.PASS


def test_the_manifest_documents_every_setting() -> None:
    manifest = (ADDON / "addon.yaml").read_text(encoding="utf-8")
    for key in (
        "api_key_env",
        "base_url",
        "model",
        "timeout_ms",
        "threshold",
        "escape_label",
        "escape_description",
        "instructions",
        "routes_file",
        "state_fields",
        "retry_backoff_ms",
    ):
        assert f"  {key}:" in manifest, key
    assert manifest.count("description:") >= 11


def test_an_edited_routes_file_is_reloaded_even_when_its_mtime_did_not_move(
    tmp_path: Path,
) -> None:
    """Slice 35 advisory: the parsed table is cached per file version, and the version is more than
    the modification time (a coarse clock, or an edit restoring the old time, keeps it)."""
    import os

    settings = _settings(tmp_path)
    routes = Path(settings["routes_file"])
    first = SYSTEM_ONE.load_routes(settings)
    stamp = routes.stat().st_mtime_ns
    routes.write_text(
        ROUTES + "  - label: returns\n    description: Returns\n    target: profile:returns\n",
        encoding="utf-8",
    )
    os.utime(routes, ns=(stamp, stamp))

    second = SYSTEM_ONE.load_routes(settings)

    assert [r.label for r in first.routes] == ["billing", "opening_hours"]
    assert [r.label for r in second.routes] == ["billing", "opening_hours", "returns"]


def test_an_unchanged_routes_file_is_parsed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    SYSTEM_ONE.load_routes(settings)
    parsed: list[object] = []
    real = SYSTEM_ONE.parse_routes

    def counting(*args: Any, **kwargs: Any) -> Any:
        parsed.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(SYSTEM_ONE, "parse_routes", counting)
    SYSTEM_ONE.load_routes(settings)
    SYSTEM_ONE.load_routes(settings)

    assert parsed == []


# --- a labelled pass option (L18: "Propón el pedido..." routed to a read-only plan at 0.74) ----


def test_a_route_whose_target_is_pass_is_offered_by_label_and_passes_when_chosen(
    tmp_path: Path,
) -> None:
    (tmp_path / "routes.yaml").write_text(
        ROUTES + "  - label: request_action\n"
        "    description: Asks to do something, not to read something\n"
        "    target: pass\n",
        encoding="utf-8",
    )
    seen: list[httpx.Request] = []

    answer = _route(
        _replying(httpx.Response(200, json=_answer("request_action", 0.91)), seen=seen),
        _settings(tmp_path),
    )

    criteria = json.loads(seen[0].content)["questions"]["route"]["criteria"]
    assert criteria["request_action"] == "Asks to do something, not to read something"
    assert answer["decision"] == "pass" and answer["reason"] == "pass_route"
    assert answer["label"] == "request_action" and answer["confidence"] == 0.91
    assert decision_from_mapping(answer).kind is RouteKind.PASS


# --- S10: the fast path's questions, in the same call --------------------------------------------

FAST_ROUTES = ROUTES + """\
  - label: stock_level
    description: How much of a product is left
    target: intent:stock_level
    question: Does the message ask how much stock of something is left?
fast_path:
  action_question: Does the message ask to DO something?
  complexity_levels: [One fact, A few facts, Advice or analysis]
"""


def _fast_request(text: str, history: list[str] | None = None) -> dict[str, Any]:
    return {**_request(text), "fast_path": True, "history": history or []}


def _fast_answer(
    choice: str = "billing",
    *,
    intents: dict[str, float] | None = None,
    action: float = 0.03,
    complexity: float = 0.2,
    follow_up: float | None = None,
    requests: float | None = 0.0,
) -> dict[str, Any]:
    body = _answer(choice, 0.9)
    for label, value in (intents or {}).items():
        body["answers"][f"intent__{label}"] = {"type": "noul", "noul": value}
    body["answers"]["action"] = {"type": "noul", "noul": action}
    body["answers"]["complexity"] = {
        "type": "score", "score": complexity, "confidence": 0.9,
        "legend": {"0": "One fact", "1": "A few facts", "2": "Advice or analysis"},
        "probabilities": {"0": 0.9, "1": 0.1, "2": 0.0},
    }  # fmt: skip
    if follow_up is not None:
        body["answers"]["follow_up"] = {"type": "noul", "noul": follow_up}
    if requests is not None:
        body["answers"]["requests"] = {"type": "score", "score": requests, "confidence": 0.9}
    return body


def _fast_route(
    handler: Handler, settings: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            answer: dict[str, Any] = await SYSTEM_ONE.route(
                request, settings, client=client, environ={"TYPESAFE_API_KEY": KEY}
            )
            return answer

    return asyncio.run(scenario())


def _fast_settings(tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "routes.yaml").write_text(FAST_ROUTES, encoding="utf-8")
    return _settings(tmp_path)


def test_without_the_fast_path_flag_the_request_is_exactly_the_one_choice_question(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []
    _route(_replying(httpx.Response(200, json=_answer("billing", 0.9)), seen=seen),
           _fast_settings(tmp_path))  # fmt: skip
    assert list(json.loads(seen[0].content)["questions"]) == ["route"]


def test_the_fast_path_asks_every_question_in_one_call(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    _fast_route(
        _replying(httpx.Response(200, json=_fast_answer()), seen=seen),
        _fast_settings(tmp_path),
        _fast_request("how much bread is left and when do you open?"),
    )

    (request,) = seen
    body = json.loads(request.content)
    assert body["state"] == "how much bread is left and when do you open?"
    questions = body["questions"]
    assert set(questions) == {
        "route", "intent__opening_hours", "intent__stock_level", "action", "complexity",
        "requests",
    }  # fmt: skip
    assert questions["intent__opening_hours"]["type"] == "noul"
    assert "When the shop is open" in questions["intent__opening_hours"]["instructions"]
    assert questions["intent__stock_level"]["instructions"] == (
        "Does the message ask how much stock of something is left?"
    )
    assert questions["action"] == {
        "type": "noul", "instructions": "Does the message ask to DO something?",
    }  # fmt: skip
    assert questions["complexity"]["type"] == "score"
    assert questions["complexity"]["criteria"] == ["One fact", "A few facts", "Advice or analysis"]
    assert "follow_up" not in questions, "no conversation yet: nothing to depend on"


def test_with_history_the_state_holds_the_conversation_and_the_follow_up_is_asked(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []
    history = ["person: how much bread is left?", "agent: 12 loaves."]

    _fast_route(
        _replying(httpx.Response(200, json=_fast_answer(follow_up=0.8)), seen=seen),
        _fast_settings(tmp_path),
        _fast_request("and croissants?", history),
    )

    body = json.loads(seen[0].content)
    assert body["state"] == {"message": "and croissants?", "recent_conversation": history}
    assert body["questions"]["follow_up"]["type"] == "noul"


def test_the_answers_come_back_as_an_assessment_by_intent_name(tmp_path: Path) -> None:
    answer = _fast_route(
        _replying(
            httpx.Response(
                200,
                json=_fast_answer(
                    intents={"opening_hours": 0.91, "stock_level": 0.88},
                    action=0.04, complexity=0.5, follow_up=0.1,
                ),
            )
        ),
        _fast_settings(tmp_path),
        _fast_request("x", ["person: hi"]),
    )  # fmt: skip

    assert answer["decision"] == "route"  # the choice is decided exactly as before
    assert answer["assessment"] == {
        "intents": {"opening_hours": 0.91, "stock_level": 0.88},
        "action": 0.04,
        "follow_up": 0.1,
        "complexity": 0.25,  # score 0.5 on levels 0..2
        "requests": 1,  # score 0.0: the first level, one request
    }
    decision = decision_from_mapping(answer)  # Core reads it
    assert decision.assessment is not None and decision.assessment.complexity == 0.25


def test_a_missing_or_malformed_fast_path_answer_drops_the_assessment_only(
    tmp_path: Path,
) -> None:
    broken = _fast_answer(intents={"opening_hours": 0.9})
    broken["answers"]["action"] = {"type": "noul", "noul": "yes"}

    answer = _fast_route(
        _replying(httpx.Response(200, json=broken)),
        _fast_settings(tmp_path),
        _fast_request("x"),
    )

    assert answer["decision"] == "route" and "assessment" not in answer
    assert answer["reason"] == "bad_assessment"


def test_a_fast_path_call_that_fails_passes_as_before(tmp_path: Path) -> None:
    answer = _fast_route(
        _replying(httpx.Response(529)), _fast_settings(tmp_path), _fast_request("x")
    )
    assert answer["decision"] == "pass" and "assessment" not in answer


# --- P13: the coverage count and per-intent criteria ---------------------------------------------

BOTH_LOW = {"opening_hours": 0.1, "stock_level": 0.1}


def test_the_fast_path_asks_how_many_separate_things_the_message_asks_for(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    _fast_route(
        _replying(httpx.Response(200, json=_fast_answer()), seen=seen),
        _fast_settings(tmp_path),
        _fast_request("x"),
    )
    question = json.loads(seen[0].content)["questions"]["requests"]
    assert question["type"] == "score"
    assert question["instructions"] == SYSTEM_ONE.DEFAULT_REQUESTS_QUESTION
    assert question["criteria"] == list(SYSTEM_ONE.DEFAULT_REQUESTS_LEVELS)
    assert len(question["criteria"]) == 4, "one, two, three, four or more"


def test_a_routes_file_may_word_the_requests_question_and_levels(tmp_path: Path) -> None:
    (tmp_path / "routes.yaml").write_text(
        FAST_ROUTES
        + "  requests_question: How many things does the customer ask?\n"
        + "  requests_levels: [One, Two or more]\n",
        encoding="utf-8",
    )
    seen: list[httpx.Request] = []
    answer = _fast_route(
        _replying(
            httpx.Response(200, json=_fast_answer(intents=BOTH_LOW, requests=1.0)), seen=seen
        ),
        _settings(tmp_path),
        _fast_request("x"),
    )
    question = json.loads(seen[0].content)["questions"]["requests"]
    assert question["instructions"] == "How many things does the customer ask?"
    assert question["criteria"] == ["One", "Two or more"]
    assert answer["assessment"]["requests"] == 2


@pytest.mark.parametrize(("score", "count"), [(0.0, 1), (0.4, 1), (0.96, 2), (1.99, 3), (3.0, 4)])
def test_the_requests_score_is_a_count_from_one(tmp_path: Path, score: float, count: int) -> None:
    answer = _fast_route(
        _replying(httpx.Response(200, json=_fast_answer(intents=BOTH_LOW, requests=score))),
        _fast_settings(tmp_path),
        _fast_request("x"),
    )
    assert answer["assessment"]["requests"] == count
    decision = decision_from_mapping(answer)
    assert decision.assessment is not None and decision.assessment.requests == count


@pytest.mark.parametrize("requests", [None, 4.5, -1.0])
def test_a_missing_or_impossible_requests_answer_drops_the_assessment(
    tmp_path: Path, requests: float | None
) -> None:
    answer = _fast_route(
        _replying(httpx.Response(200, json=_fast_answer(intents=BOTH_LOW, requests=requests))),
        _fast_settings(tmp_path),
        _fast_request("x"),
    )
    assert "assessment" not in answer and answer["reason"] == "bad_assessment"


def test_an_intent_route_may_give_its_yes_no_question_criteria(tmp_path: Path) -> None:
    (tmp_path / "routes.yaml").write_text(
        ROUTES
        + """\
  - label: stock_level
    description: How much of a product is left
    target: intent:stock_level
    criteria:
      true: {what: It asks how much is left, examples: ["any bread left?"]}
      false: It asks about prices or opening hours
""",
        encoding="utf-8",
    )
    seen: list[httpx.Request] = []
    _fast_route(
        _replying(httpx.Response(200, json=_fast_answer()), seen=seen),
        _settings(tmp_path),
        _fast_request("x"),
    )
    questions = json.loads(seen[0].content)["questions"]
    assert questions["intent__stock_level"]["criteria"] == {
        "true": {"what": "It asks how much is left", "examples": ["any bread left?"]},
        "false": "It asks about prices or opening hours",
    }
    assert "criteria" not in questions["intent__opening_hours"]


@pytest.mark.parametrize(
    "criteria",
    [
        "    criteria: yes\n",
        "    criteria: {true: a}\n",
        "    criteria: {true: a, false: b, maybe: c}\n",
        "    criteria: {true: '', false: b}\n",
    ],
)
def test_malformed_criteria_stop_the_router(tmp_path: Path, criteria: str) -> None:
    routes = ROUTES + criteria
    with pytest.raises(SYSTEM_ONE.SystemOneConfigError, match="criteria"):
        SYSTEM_ONE.parse_routes(
            __import__("yaml").safe_load(routes), escape_label="none_of_these", source="t"
        )


def test_criteria_belong_to_intent_routes_only() -> None:
    routes = ROUTES.replace(
        "    target: profile:billing\n",
        "    target: profile:billing\n    criteria: {true: a, false: b}\n",
    )
    with pytest.raises(SYSTEM_ONE.SystemOneConfigError, match="criteria"):
        SYSTEM_ONE.parse_routes(
            __import__("yaml").safe_load(routes), escape_label="none_of_these", source="t"
        )
