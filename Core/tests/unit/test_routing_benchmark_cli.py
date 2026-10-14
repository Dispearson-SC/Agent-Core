"""The routing benchmark's entry point: `python -m evals.routing_cli` (Phase 5 P4).

Subject: evals/routing_cli.py (`main(env)`, `HttpTurnDriver`).

THE RULES UNDER TEST. The offline run (the set's keyword rules) needs no opt-in and writes
`routing.json` and `routing.md`; a real router (an addon's) or whole turns against a running Core
need `AGENT_CORE_EVAL=1` and are a clean skip without it; a missing set, an unknown router or a
bad number stops the run naming what is wrong; an accuracy floor turns a low score into exit 1.
Whole turns go through the HTTP API: started on the profile the route names, polled until done.
No network here: the addon is a local file and the API is an `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import httpx
import pytest
from evals.routing import Question
from evals.routing_cli import HttpTurnDriver, TurnNotFinishedError, main

SET = """\
name: tiny
synthetic: true
entry_profile: front_desk
keyword_router:
  - route: "profile:billing"
    keywords: [factura]
questions:
  - {id: q1, text: "mi factura", expected: "profile:billing"}
  - {id: q2, text: "hola", expected: pass}
  - {id: q3, text: "el horario", expected: "intent:opening_hours"}
"""


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    question_set = tmp_path / "set.yaml"
    question_set.write_text(SET, encoding="utf-8")
    return {
        "AGENT_CORE_EVAL_ROUTING_SET": str(question_set),
        "AGENT_CORE_EVAL_REPORT_DIR": str(tmp_path / "report"),
        **extra,
    }


def test_the_offline_run_needs_no_opt_in_and_writes_both_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_env(tmp_path))

    assert code == 0
    report = json.loads((tmp_path / "report" / "routing.json").read_text())
    assert report["router"]["router"] == "keyword"
    assert report["router"]["accuracy"] == pytest.approx(2 / 3)
    assert report["baseline"]["accuracy"] == pytest.approx(1 / 3)
    assert any("synthetic" in note for note in report["notes"])
    assert (tmp_path / "report" / "routing.md").exists()
    assert "33% off -> 67% on" in capsys.readouterr().out


def test_an_accuracy_floor_above_the_score_exits_1(tmp_path: Path) -> None:
    assert main(_env(tmp_path, AGENT_CORE_EVAL_MIN_ACCURACY="0.9")) == 1


@pytest.mark.parametrize(
    "extra",
    [{"AGENT_CORE_EVAL_ROUTER": "classifier"}, {"AGENT_CORE_EVAL_TURNS_URL": "http://core"}],
)
def test_a_real_router_or_whole_turns_without_the_opt_in_are_skipped_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: dict[str, str]
) -> None:
    code = main(_env(tmp_path, **extra))

    assert code == 0 and "skipped" in capsys.readouterr().out
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize(
    ("env", "complaint"),
    [
        ({"AGENT_CORE_EVAL_ROUTING_SET": ""}, "AGENT_CORE_EVAL_ROUTING_SET"),
        ({"AGENT_CORE_EVAL_ROUTER_TIMEOUT_MS": "soon"}, "AGENT_CORE_EVAL_ROUTER_TIMEOUT_MS"),
        ({"AGENT_CORE_EVAL_MIN_ACCURACY": "2"}, "AGENT_CORE_EVAL_MIN_ACCURACY"),
        ({"AGENT_CORE_EVAL_ROUTER": "nobody", "AGENT_CORE_EVAL": "1"}, "'nobody'"),
    ],
)
def test_a_missing_set_a_bad_number_or_an_unknown_router_stops_the_run_naming_it(
    tmp_path: Path, env: dict[str, str], complaint: str
) -> None:
    with pytest.raises(SystemExit, match=complaint):
        main({**_env(tmp_path), **env})


def test_an_opted_in_run_uses_the_router_an_enabled_addon_provides(tmp_path: Path) -> None:
    addon = tmp_path / "addons" / "bench_router"
    (addon / "routers").mkdir(parents=True)
    (addon / "addon.yaml").write_text(
        "name: bench_router\nversion: 1.0.0\ncore: '>=0'\nprovides:\n  routers: [hours]\n"
    )
    (addon / "routers" / "hours.py").write_text(
        dedent(
            """
            def route(request):
                if "horario" in request["text"]:
                    return {"decision": "intent", "intent": "opening_hours", "confidence": 0.9}
                return {"decision": "pass"}
            """
        )
    )
    env = _env(
        tmp_path,
        AGENT_CORE_EVAL="1",
        AGENT_CORE_EVAL_ROUTER="hours",
        AGENT_CORE_ADDONS="bench_router",
        AGENT_CORE_ADDONS_DIR=str(tmp_path / "addons"),
    )

    assert main(env) == 0

    report = json.loads((tmp_path / "report" / "routing.json").read_text())
    assert [v["got"] for v in report["router"]["verdicts"]] == [
        "pass",
        "pass",
        "intent:opening_hours",
    ]


def test_a_whole_turn_starts_on_the_routed_profile_and_is_polled_until_finished() -> None:
    seen: list[tuple[str, str, object]] = []
    polls = iter(["running", "running", "finished"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        assert request.headers["X-Tenant-Id"] == "t"
        if request.method == "POST":
            return httpx.Response(202, json={"turn_id": "turn-9", "status": "accepted"})
        return httpx.Response(
            200, json={"turn_id": "turn-9", "status": next(polls, "finished"), "text": None}
        )

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            driver = HttpTurnDriver(client, headers={"X-Tenant-Id": "t"}, poll_seconds=0)
            await driver.run(
                Question("q1", "mi factura", "profile:billing"), "profile:billing", "front_desk"
            )
            await driver.run(Question("q2", "el horario", "pass"), "intent:x", "front_desk")

    asyncio.run(scenario())

    posts = [body for method, _, body in seen if method == "POST"]
    assert [p["profile_id"] for p in posts] == ["billing", "front_desk"]  # type: ignore[index]
    assert posts[0]["text"] == "mi factura"  # type: ignore[index]
    assert [path for method, path, _ in seen if method == "GET"][:3] == ["/turns/turn-9"] * 3


def test_a_turn_that_never_finishes_times_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"turn_id": "t1", "status": "accepted"})
        return httpx.Response(200, json={"turn_id": "t1", "status": "running", "text": None})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            driver = HttpTurnDriver(client, headers={}, poll_seconds=0.01, timeout_seconds=0.05)
            await driver.run(Question("q", "x", "pass"), "pass", "front_desk")

    with pytest.raises(TimeoutError, match="still running"):
        asyncio.run(scenario())


def _driver_against(statuses: list[str]) -> Any:
    polls = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"turn_id": "t1", "status": "accepted"})
        return httpx.Response(200, json={"turn_id": "t1", "status": next(polls), "text": None})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            driver = HttpTurnDriver(client, headers={}, poll_seconds=0, timeout_seconds=5)
            await driver.run(Question("q", "x", "pass"), "pass", "front_desk")

    return scenario


def test_a_turn_waiting_on_a_person_is_not_a_finished_turn() -> None:
    with pytest.raises(TurnNotFinishedError, match="waiting"):
        asyncio.run(_driver_against(["running", "waiting"])())


def test_an_unknown_turn_status_stops_the_poll_naming_it() -> None:
    with pytest.raises(TurnNotFinishedError, match="exploded"):
        asyncio.run(_driver_against(["exploded"])())


def test_only_a_finished_turn_ends_the_poll_successfully() -> None:
    asyncio.run(_driver_against(["running", "running", "finished"])())


@pytest.mark.parametrize("status", [None, "", "Finished", "accepted", ["finished"]])
def test_a_status_outside_the_known_ones_is_an_unknown_status_error(status: object) -> None:
    """Slice 35 advisory: the poll knows its statuses explicitly (one done, one in progress, the
    stopped ones); anything else is its own error, never read as done or as still running."""
    from evals.routing_cli import UnknownTurnStatusError

    polls = iter([status])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"turn_id": "t1", "status": "accepted"})
        return httpx.Response(200, json={"turn_id": "t1", "status": next(polls), "text": None})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            driver = HttpTurnDriver(client, headers={}, poll_seconds=0, timeout_seconds=5)
            await driver.run(Question("q", "x", "pass"), "pass", "front_desk")

    with pytest.raises(UnknownTurnStatusError, match="unknown status"):
        asyncio.run(scenario())


def test_the_poll_statuses_are_an_explicit_allowlist() -> None:
    from evals import routing_cli

    assert routing_cli.DONE_STATUSES == frozenset({"finished"})
    assert routing_cli.IN_PROGRESS_STATUSES == frozenset({"running"})
    assert routing_cli.STOPPED_STATUSES == frozenset({"waiting"})


def _snapshot_stream(plan: object) -> bytes:
    snapshot = {
        "root_turn_id": "t1",
        "last_seq": 4,
        "settled": True,
        "turns": [
            {"turn_id": "t1", "agent": "billing", "state": "finished", "plan": plan},
        ],
    }
    return (
        f"event: snapshot\nid: 4\ndata: {json.dumps(snapshot)}\n\nevent: end\ndata: {{}}\n\n"
    ).encode()


@pytest.mark.parametrize("plan", ["plan", "fallback:tool_error", None])
def test_an_intent_turn_is_posted_to_the_entry_profile_and_reports_its_plan(plan: object) -> None:
    """D97: the deployment's router and plan answer an intent question; after the turn finished
    the driver reads the turn's snapshot from its event feed and returns how the plan ended."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            assert json.loads(request.content)["profile_id"] == "front_desk"
            return httpx.Response(202, json={"turn_id": "t1", "status": "accepted"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=_snapshot_stream(plan))
        return httpx.Response(200, json={"turn_id": "t1", "status": "finished", "text": "x"})

    async def scenario() -> object:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            driver = HttpTurnDriver(client, headers={}, poll_seconds=0)
            question = Question("q", "low stock?", "intent:x")
            return await driver.run(question, "intent:x", "front_desk")

    assert asyncio.run(scenario()) == plan
    assert seen == [("POST", "/turns"), ("GET", "/turns/t1"), ("GET", "/turns/t1/events")]


def test_a_profile_or_pass_turn_never_reads_the_event_feed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(202, json={"turn_id": "t1", "status": "accepted"})
        return httpx.Response(200, json={"turn_id": "t1", "status": "finished", "text": "x"})

    async def scenario() -> object:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            driver = HttpTurnDriver(client, headers={}, poll_seconds=0)
            return await driver.run(Question("q", "x", "pass"), "profile:billing", "front_desk")

    assert asyncio.run(scenario()) is None
    assert "/turns/t1/events" not in seen
