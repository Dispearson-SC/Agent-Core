"""Golden turns: the addon build plays the scripted glazed turns as vertical/glazed did (G6).

Subject: the addon build (http_tool_bridge + glazed_tools + plain_language + glazed_vertical) as the
root registers it. Oracle: `fixtures/glazed_parity/golden.json`, the same turns captured on
vertical/glazed by `fixtures/glazed_parity/capture_golden.py` (its docstring says how). The turns,
the fake backend and the gate are `tests/support/glazed_parity.py`, shared by both runs.

Compared per scenario: the tools of the role and those the policy leaves offered, the tool
definitions the model sees, the persona, and per call the validated arguments, the policy outcome,
every HTTP request (method, path, query, headers, JSON body), the tool result as the model reads it
and the audit rows. Everything must be equal except, each asserted on its own below:

ACCEPTED (by the user, G4/G5):
  * the orchestrator's "How to talk to the manager" section is the `plain_language` fragment;
  * S9 (P11): the orchestrator asks every independent specialist in the same step instead of one per
    step (`glazed_vertical_support.INTENDED_ORCHESTRATOR_CHANGES`, applied to the golden persona);
  * a 404/503 refusal carries the backend's reason (D91): an error value gains `reason`, an
    "unavailable" answer says "The backend said: ...".
DOCUMENTED (D87, pinned value by value in test_glazed_tools_contract.py):
  * the schema the model sees: an optional argument is its type without `null` and without the
    `None` default, the old in-code clamps and enums are `minimum`/`maximum`/`enum`, and there is
    no `format: date`.
FINDINGS (G6, reported for a decision; pinned so a change to either side is seen):
  * F2 a POST body's JSON keys and a query's parameters come in another ORDER (same content).
RESOLVED (G7): F1, the audit row and the policy's arguments lacked the arguments the model did not
send that the old function defaulted. The bridge now fills them (default, or null) into the
validated arguments, so they compare EQUAL here; the finding's test pins that they stay equal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.registration import collect_audit_for
from agent_core.adapters.driven.persistence_pg.audit_repository import PRESENCE_RECORDED
from agent_core.composition import TOOL_PACKAGES
from tests.support.glazed_parity import SCENARIOS, Build, run_scenarios, without_client_version
from tests.unit import glazed_vertical_support as glazed

GOLDEN_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "glazed_parity" / "golden.json"
# The digest of golden.json as capture_golden.py wrote it (slice 31 advisory): checked below.
GOLDEN_SHA256 = "bb8f44b047bfe5b897a220759eb8b4d21fb59775530e3eb4f2ac4de3b11008b2"
GOLDEN: dict[str, dict[str, Any]] = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
HELPER = "agent_core_addons.glazed_tools.toolsets._glazed"
SECTION_START = "How to talk to the manager"
SECTION_END = "Examples (style only"

# Accepted difference D91: (scenario, step) -> the backend's reason the addon build shows.
REASONS = {
    ("past-recalls-with-a-warning-and-meets-a-missing-promo", 1): "promo P/9 is not in this case",
    ("supply-forecasts-and-meets-an-unavailable-view", 1): "inventory is being recomputed",
    ("strategist-audits-a-promo-and-meets-a-missing-forecast", 1): (
        "no traffic model for this store"
    ),
}


def _build() -> Build:
    contributions = glazed.contributions()
    glazed_tools = sys.modules[HELPER]

    def install(transport: Any) -> None:
        setattr(glazed_tools, "TRANSPORT", transport)  # noqa: B010 - an addon module, untyped

    return Build(
        toolset=lambda role: contributions.toolsets[f"glazed_{role}"](),
        persona=glazed.persona,
        rules=glazed.policy_rules(),
        audit_policy=collect_audit_for(TOOL_PACKAGES, contributions),
        install_transport=install,
    )


@pytest.fixture(scope="module")
def actual() -> dict[str, dict[str, Any]]:
    played = run_scenarios(_build())
    canonical: dict[str, dict[str, Any]] = json.loads(json.dumps(played, sort_keys=True))
    return canonical


# --- what the comparison lets differ, each in one place -------------------------------------------


def _schema(spec: dict[str, Any]) -> dict[str, Any]:
    """D87: an optional X is X, no `None` default, no `format`; clamps and enums: contract test."""
    flat = {k: v for k, v in spec.items() if k not in ("title", "format", "minimum", "maximum")}
    if "anyOf" in flat:
        flat.update(next(o for o in flat.pop("anyOf") if o["type"] != "null"))
    if "default" in flat and flat["default"] is None:
        del flat["default"]
    flat.pop("enum", None)
    return flat


def _definition(definition: dict[str, Any]) -> dict[str, Any]:
    parameters = definition["parameters"]
    return {
        "name": definition["name"],
        "description": definition["description"],
        "required": sorted(parameters.get("required", [])),
        "properties": {k: _schema(v) for k, v in parameters.get("properties", {}).items()},
        "rest": {k: v for k, v in parameters.items() if k not in ("required", "properties")},
    }


def _request(request: dict[str, Any]) -> dict[str, Any]:
    """F2: the same request whatever the order of its JSON keys and query parameters. The httpx
    version in the user agent is the test environment's, not glazed's (slice 31 advisory)."""
    return {
        **request,
        "body": None,
        "query": sorted(map(tuple, request["query"])),
        "headers": without_client_version(request["headers"]),
    }


def _seen(scenario: str, number: int, seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """D91: the old result with the backend's reason where the addon build now shows it."""
    reason = REASONS.get((scenario, number))
    if reason is None:
        return seen
    (only,) = seen
    result = json.loads(only["tool_return"])
    if "error" in result:
        result["reason"] = reason
    else:
        head, tail = result["reason"].split(" Say the ", 1)
        result["reason"] = f"{head} The backend said: {reason}. Say the {tail}"
    return [{"tool_return": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}]


def _persona(role: str, old: str, new: str) -> str:
    """Accepted: the orchestrator's manager section is plain_language's. Returns `new` when it is
    the old persona with that section in place (so it compares equal); asserted on its own below."""
    if role != "orchestrator":
        return old
    old = glazed.with_intended_changes(old)  # S9, P11: asserted on its own below
    start, end = old.index(SECTION_START), old.index(SECTION_END)
    if new.startswith(old[:start]) and new.endswith(old[end:]):
        return new
    return old


def _expected(scenario: str, golden: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    steps = []
    for number, step in enumerate(golden["steps"]):
        steps.append(
            {
                **step,
                "requests": [_request(r) for r in step["requests"]],
                "seen_by_model": _seen(scenario, number, step["seen_by_model"]),
            }
        )
    return {
        **golden,
        "persona": _persona(golden["role"], golden["persona"], new["persona"]),
        "definitions": [_definition(d) for d in golden["definitions"]],
        "steps": steps,
    }


def _comparable(played: dict[str, Any]) -> dict[str, Any]:
    return {
        **played,
        "definitions": [_definition(d) for d in played["definitions"]],
        "steps": [
            {**step, "requests": [_request(r) for r in step["requests"]]}
            for step in played["steps"]
        ],
    }


# --- the comparison -------------------------------------------------------------------------------


def test_the_golden_covers_every_role_a_read_a_write_refusals_and_a_peer_turn_deny() -> None:
    assert {s["id"] for s in SCENARIOS} == set(GOLDEN)
    assert {g["role"] for g in GOLDEN.values()} == set(glazed.ROLES)
    steps = [s for g in GOLDEN.values() for s in g["steps"]]
    methods = {r["method"] for s in steps for r in s["requests"]}
    assert methods == {"GET", "POST"}
    statuses = {
        json.loads(s["seen_by_model"][0]["tool_return"]).get("status")
        for s in steps
        if s["seen_by_model"][0]["tool_return"].startswith("{")
    }
    assert 404 in statuses
    assert any("not available right now" in s["seen_by_model"][0]["tool_return"] for s in steps)
    denied = GOLDEN["a-peer-turn-may-not-propose"]["steps"][0]["policy"]
    assert denied["effect"] == "deny"
    assert denied["rule_id"] == "glazed-peer-turn-no-propose-action"


def test_no_glazed_call_waits_for_an_approval_in_either_build(
    actual: dict[str, dict[str, Any]],
) -> None:
    """There is no `needs_approval` rule for a glazed tool: a write is ALLOWED and creates a PENDING
    proposal the manager decides in the backend (`requires_approval` in its answer). Both builds
    let the call through and the turn goes on."""
    for played in (GOLDEN, actual):
        effects = {s["policy"]["effect"] for g in played.values() for s in g["steps"]}
        assert effects == {"allow", "deny"}
        assert not any(g["suspended"] for g in played.values())
    proposal = actual["orchestrator-reads-the-briefing-and-proposes"]["steps"][1]
    assert proposal["policy"]["rule_id"] == "glazed-propose-action"
    assert json.loads(proposal["seen_by_model"][0]["tool_return"])["requires_approval"] is True
    assert "idempotency-key" in proposal["requests"][0]["headers"]


@pytest.mark.parametrize("scenario", sorted(GOLDEN))
def test_a_scripted_turn_plays_exactly_as_on_vertical_glazed(
    actual: dict[str, dict[str, Any]], scenario: str
) -> None:
    new = actual[scenario]
    expected = _expected(scenario, GOLDEN[scenario], new)
    played = _comparable(new)

    for key in ("tools", "offered_after_policy", "suspended", "persona", "definitions"):
        assert played[key] == expected[key], key
    assert len(played["steps"]) == len(expected["steps"])
    for number, (got, want) in enumerate(zip(played["steps"], expected["steps"], strict=True)):
        for key in ("tool", "args", "validated_args", "policy", "requests", "seen_by_model"):
            assert got[key] == want[key], (number, got["tool"], key)
    assert played["audit"] == expected["audit"]


# --- the accepted differences, asserted on their own ----------------------------------------------


def test_the_orchestrator_persona_differs_only_in_the_plain_language_section(
    actual: dict[str, dict[str, Any]],
) -> None:
    golden = GOLDEN["orchestrator-reads-the-briefing-and-proposes"]["persona"]
    old = glazed.with_intended_changes(golden)
    new = actual["orchestrator-reads-the-briefing-and-proposes"]["persona"]
    start, end = old.index(SECTION_START), old.index(SECTION_END)
    slot = glazed.contributions().fragments["plain_language"].read_text(encoding="utf-8").strip()

    assert new != old
    assert new.startswith(old[:start] + slot + "\n")
    assert new.endswith(old[end:])
    for scenario, played in actual.items():
        if played["role"] != "orchestrator":
            assert played["persona"] == GOLDEN[scenario]["persona"], scenario


@pytest.mark.parametrize(("scenario", "number"), sorted(REASONS))
def test_a_refusal_now_shows_the_model_the_backends_reason(
    actual: dict[str, dict[str, Any]], scenario: str, number: int
) -> None:
    old = json.loads(GOLDEN[scenario]["steps"][number]["seen_by_model"][0]["tool_return"])
    new = json.loads(actual[scenario]["steps"][number]["seen_by_model"][0]["tool_return"])
    reason = REASONS[(scenario, number)]

    if "error" in old:
        assert "reason" not in old
        assert new == {**old, "reason": reason}
    else:
        assert reason not in old["reason"]
        assert f"The backend said: {reason}." in new["reason"]
        assert {k: v for k, v in new.items() if k != "reason"} == {
            k: v for k, v in old.items() if k != "reason"
        }


# --- the findings, pinned -------------------------------------------------------------------------


def test_finding_f1_resolved_unsent_defaults_are_arguments_again_and_the_request_is_unchanged(
    actual: dict[str, dict[str, Any]],
) -> None:
    """F1, resolved in G7 (approved as default). The old tools were Python functions: the framework
    filled every default into the arguments the policy and the audit row saw. The bridge now does
    the same, and the request it sends is the one it sent before."""
    scenario = "orchestrator-reads-the-briefing-and-proposes"
    old_row, new_row = GOLDEN[scenario]["audit"][2], actual[scenario]["audit"][2]
    old_req = GOLDEN[scenario]["steps"][2]["requests"][0]["json"]
    new_req = actual[scenario]["steps"][2]["requests"][0]["json"]

    assert (
        new_row["arguments"]
        == old_row["arguments"]
        == {
            "category": "eventos",
            "date_from": "2026-03-07",
            "date_to": None,
            "impact": None,
            "text": PRESENCE_RECORDED,
        }
    )
    assert (
        actual[scenario]["steps"][2]["validated_args"]
        == (GOLDEN[scenario]["steps"][2]["validated_args"])
    )
    assert new_req == old_req and new_req["category"] == "eventos"


def test_finding_f2_bodies_and_queries_keep_their_content_but_not_their_order(
    actual: dict[str, dict[str, Any]],
) -> None:
    scenario = "sentinel-records-an-event-and-checks-compliance"
    old = GOLDEN[scenario]["steps"][0]["requests"][0]
    new = actual[scenario]["steps"][0]["requests"][0]

    assert new["json"] == old["json"]
    assert new["body"] != old["body"]
    assert list(json.loads(new["body"])) == sorted(old["json"])


# --- slice 31 advisories: what the comparison must not depend on ---------------------------------


def _a_request(user_agent: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "path": "/internal/v1/kpis",
        "query": [],
        "headers": {"accept": "*/*", "user-agent": user_agent, "x-glazed-store": "S030"},
        "body": "",
        "json": None,
    }


def test_the_httpx_version_in_the_user_agent_is_not_part_of_the_comparison() -> None:
    """Slice 31 advisory (capture_golden.py:68-72): the golden recorded `python-httpx/0.28.1`, so an
    httpx upgrade would fail every scenario for a reason that is not glazed's. The client and its
    version stay compared as `python-httpx`; any other user agent is compared as it is."""
    assert _request(_a_request("python-httpx/0.28.1")) == _request(_a_request("python-httpx/9.1"))
    assert _request(_a_request("python-httpx/9.1"))["headers"]["user-agent"] == "python-httpx"
    assert _request(_a_request("glazed/1.0"))["headers"]["user-agent"] == "glazed/1.0"


def test_the_golden_file_is_the_one_capture_golden_wrote() -> None:
    """Slice 31 advisory (test_glazed_golden_turns.py:43-44): the oracle was read unchecked, so a
    hand edit (or a bad merge) to golden.json would move the oracle silently. Its digest is pinned
    here; re-capturing it (capture_golden.py) is the only change that updates this line."""
    digest = hashlib.sha256(GOLDEN_FILE.read_bytes()).hexdigest()

    assert digest == GOLDEN_SHA256, "golden.json changed: re-capture it, never edit it by hand"


def _capture_module() -> Any:
    path = GOLDEN_FILE.parent / "capture_golden.py"
    spec = importlib.util.spec_from_file_location("capture_golden_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv", [[], ["a", "b"], ["{missing}"], ["{not_a_core}"]])
def test_capture_golden_refuses_a_bad_command_line_with_usage_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Slice 31 advisory (capture_golden.py:77-78): `sys.argv[1]` unchecked raised an IndexError
    with no argument, and any path was taken for the old Core. The command line is checked first:
    one argument, an existing Core directory (it has `src/agent_core`), else usage and exit 2."""
    (tmp_path / "plain").mkdir()
    paths = {"{missing}": str(tmp_path / "absent"), "{not_a_core}": str(tmp_path / "plain")}
    capture = _capture_module()
    before = GOLDEN_FILE.read_bytes()

    assert capture.cli([paths.get(a, a) for a in argv]) == 2

    assert "usage: capture_golden.py <old Core directory>" in capsys.readouterr().err
    assert GOLDEN_FILE.read_bytes() == before
