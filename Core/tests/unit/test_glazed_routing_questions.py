"""The glazed routing question set (Phase 5 P4): glazed_vertical/evals/routing_questions.yaml.

THE RULES UNDER TEST. The set loads with the benchmark's loader, says it is synthetic, holds 60-80
questions started on the orchestrator, labels each with a route the glazed vertical can actually
take (a specialist profile it ships, one of 5-10 frequent intents, or the normal path) with every
specialist and every intent covered, and the offline benchmark runs on it end to end.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import yaml
from evals.routing import load_question_set
from evals.routing_cli import main

from tests.unit import glazed_vertical_support as glazed

QUESTIONS = glazed.ADDON / "evals" / "routing_questions.yaml"
SPECIALISTS = {f"glazed_{role}" for role in glazed.ROLES} - {"glazed_orchestrator"}


def test_the_set_is_synthetic_sized_and_started_on_the_orchestrator() -> None:
    question_set = load_question_set(QUESTIONS)

    assert question_set.synthetic is True
    assert question_set.entry_profile == "glazed_orchestrator"
    assert 60 <= len(question_set.questions) <= 80


def test_every_route_is_one_the_vertical_can_take_and_each_is_covered() -> None:
    question_set = load_question_set(QUESTIONS)
    shipped = {
        yaml.safe_load(path.read_text())["id"]
        for path in (glazed.ADDON / "profiles").glob("*.yaml")
    }
    routes = Counter(q.expected for q in question_set.questions)

    profiles = {r.removeprefix("profile:") for r in routes if r.startswith("profile:")}
    intents = {r for r in routes if r.startswith("intent:")}
    assert profiles == SPECIALISTS and profiles <= shipped
    assert 5 <= len(intents) <= 10
    assert all(routes[intent] >= 4 for intent in intents), routes
    assert routes["pass"] >= 10
    assert set(routes) == {"pass"} | intents | {f"profile:{p}" for p in profiles}


def test_the_offline_benchmark_runs_on_the_set(tmp_path: Path) -> None:
    env = {
        "AGENT_CORE_EVAL_ROUTING_SET": str(QUESTIONS),
        "AGENT_CORE_EVAL_REPORT_DIR": str(tmp_path),
    }

    assert main(env) == 0

    report = json.loads((tmp_path / "routing.json").read_text())
    assert report["question_set"] == "glazed_store_manager" and report["synthetic"] is True
    assert report["router"]["fallback_rate"] == 0
    assert report["router"]["accuracy"] > report["baseline"]["accuracy"]
