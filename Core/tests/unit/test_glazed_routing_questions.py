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
    # 78 synthetic + 4 recorded (L18) + 15 combined (S10)
    assert 60 <= len(question_set.questions) <= 110


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


def test_at_least_fifteen_combined_questions_name_two_or_more_routable_intents() -> None:
    """S10: questions that ask two things at once, labelled with their intent set; their single
    route is `pass` (no one route answers both), and each intent is one the router asks about."""
    question_set = load_question_set(QUESTIONS)
    routes = yaml.safe_load((glazed.ADDON / "routing" / "system_one_routes.yaml").read_text())
    routable = {
        r["target"].removeprefix("intent:")
        for r in routes["routes"]
        if r["target"].startswith("intent:")
    }
    combined = [q for q in question_set.questions if len(q.expected_intents) >= 2]
    assert len(combined) >= 15
    assert all(q.expected == "pass" for q in combined)
    assert all(set(q.expected_intents) <= routable for q in question_set.questions)



def test_coverage_questions_pair_one_routable_intent_with_a_part_no_intent_covers() -> None:
    """P13. The coverage guard's questions (cv*): one intent plus advice, an explanation or a past
    event. The fast path must not answer them as a composite of that one intent."""
    question_set = load_question_set(QUESTIONS)
    coverage = [q for q in question_set.questions if q.id.startswith("cv")]
    assert len(coverage) >= 4
    assert all(q.expected == "pass" and len(q.expected_intents) == 1 for q in coverage)


def test_day_summary_has_plain_and_combined_phrasings() -> None:
    """P13. day_summary was never detected at 0.7 in the P12 battery: the set asks it many ways."""
    question_set = load_question_set(QUESTIONS)
    asks = [q for q in question_set.questions if "day_summary" in q.expected_intents]
    assert sum(len(q.expected_intents) == 1 for q in asks) >= 8
    assert sum(len(q.expected_intents) >= 2 for q in asks) >= 8
