"""The routing benchmark (Phase 5 P4): a labelled question set, a router, and the numbers.

Subject: evals/routing.py (`load_question_set`, `run_routing`, `KeywordRouter`, the report).

THE RULES UNDER TEST. A question set (YAML or JSONL) labels each question with the route it should
take: `pass`, `profile:<id>` or `intent:<name>`; a malformed file is refused naming the file and
the question. The benchmark runs the set with routing OFF (every turn takes the normal path) and
ON (the router decides), and reports for each: accuracy, accuracy over what the router committed
to, confusion (expected x got), fallback rate (no answer: timeout, error), abstention rate (the
router said pass) and p50/p95 latency of the router and, with a `TurnDriver`, of the whole turn.
No network, no model: the routers here are scripted and the clock is fake.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import dedent

import pytest
from evals.routing import (
    KeywordRouter,
    Question,
    QuestionSet,
    QuestionSetError,
    RoutingComparison,
    load_question_set,
    parse_route,
    percentile,
    route_of,
    run_routing,
    write_routing_report,
)

from agent_core.domain.routing import RouteDecision, RouteKind, RouteRequest

YAML_SET = """\
name: front_desk_set
synthetic: true
description: A tiny set.
entry_profile: front_desk
keyword_router:
  - route: "profile:billing"
    keywords: [factura, Reembolso]
  - route: "intent:opening_hours"
    keywords: [horario]
questions:
  - id: q1
    text: "¿Dónde está mi factura?"
    expected: "profile:billing"
  - id: q2
    text: "¿Cuál es el horario?"
    expected: "intent:opening_hours"
    note: "frequent"
  - id: q3
    text: "Hola"
    expected: pass
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TableRouter:
    """A `TurnRouter` answering per question text: a decision, a delay (seconds of fake clock and
    real sleep) or an error."""

    def __init__(self, clock: FakeClock, table: dict[str, object], cost_ms: float = 10.0) -> None:
        self.clock, self.table, self.cost_ms = clock, table, cost_ms

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        self.clock.now += self.cost_ms / 1000
        answer = self.table[request.text]
        if isinstance(answer, Exception):
            raise answer
        if answer == "hang":
            await asyncio.sleep(5)
        assert isinstance(answer, RouteDecision)
        return answer


class FakeTurns:
    """A `TurnDriver`: a turn on the entry profile costs 100 ms, a routed one 40 ms."""

    def __init__(self, clock: FakeClock, failing: str | None = None) -> None:
        self.clock, self.failing = clock, failing
        self.ran: list[tuple[str, str, str]] = []

    async def run(self, question: Question, route: str, entry_profile: str) -> None:
        self.ran.append((question.id, route, entry_profile))
        self.clock.now += 0.100 if route == "pass" else 0.040
        if question.id == self.failing:
            raise RuntimeError("the turn failed")


def _set(*questions: tuple[str, str]) -> QuestionSet:
    return QuestionSet(
        name="s",
        synthetic=True,
        entry_profile="front_desk",
        questions=tuple(
            Question(f"q{i}", text, expected) for i, (text, expected) in enumerate(questions, 1)
        ),
    )


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(dedent(content), encoding="utf-8")
    return path


# --- the question set ---------------------------------------------------------------------------


def test_a_yaml_set_loads_its_header_questions_and_keyword_rules(tmp_path: Path) -> None:
    loaded = load_question_set(_write(tmp_path, "set.yaml", YAML_SET))

    assert (loaded.name, loaded.synthetic, loaded.entry_profile) == (
        "front_desk_set",
        True,
        "front_desk",
    )
    assert [(q.id, q.expected) for q in loaded.questions] == [
        ("q1", "profile:billing"),
        ("q2", "intent:opening_hours"),
        ("q3", "pass"),
    ]
    assert loaded.questions[1].note == "frequent"
    assert loaded.keyword_rules[0] == ("profile:billing", ("factura", "reembolso"))


def test_a_jsonl_set_is_a_header_line_then_one_question_per_line(tmp_path: Path) -> None:
    header = {"header": {"name": "j", "synthetic": False, "entry_profile": "front_desk"}}
    rows = [header, {"id": "a", "text": "hola", "expected": "pass"}]
    content = "\n".join(json.dumps(r) for r in rows) + "\n\n"

    loaded = load_question_set(_write(tmp_path, "set.jsonl", content))

    assert (loaded.name, loaded.synthetic) == ("j", False)
    assert loaded.questions == (Question("a", "hola", "pass"),)
    assert loaded.keyword_rules == ()


@pytest.mark.parametrize(
    ("questions", "complaint"),
    [
        ("- {id: a, text: x, expected: pass}\n- {id: a, text: y, expected: pass}", "duplicate id"),
        ("- {id: a, text: x, expected: 'route:b'}", "'pass', 'profile:<id>' or 'intent:<name>'"),
        ("- {id: a, text: x, expected: 'profile:'}", "'pass', 'profile:<id>' or 'intent:<name>'"),
        ("- {id: a, text: x, expected: pass, extra: 1}", "unknown keys"),
        ("- {id: a, text: '  ', expected: pass}", "'text' must be a non-empty string"),
        ("[]", "non-empty list"),
    ],
)
def test_a_malformed_question_refuses_the_file_naming_where(
    tmp_path: Path, questions: str, complaint: str
) -> None:
    path = _write(
        tmp_path,
        "bad.yaml",
        "name: b\nsynthetic: true\nentry_profile: f\nquestions:\n"
        + "\n".join("  " + line for line in questions.splitlines())
        + "\n",
    )

    with pytest.raises(QuestionSetError, match="bad.yaml") as refused:
        load_question_set(path)
    assert complaint in str(refused.value)


@pytest.mark.parametrize(
    ("content", "suffix", "complaint"),
    [
        (
            "name: b\nentry_profile: f\nquestions: [{id: a, text: x, expected: pass}]\n",
            ".yaml",
            "'synthetic' must be true or false",
        ),
        (
            "name: b\nsynthetic: true\nquestions: [{id: a, text: x, expected: pass}]\n",
            ".yaml",
            "'entry_profile'",
        ),
        ('{"id": "a", "text": "x", "expected": "pass"}\n', ".jsonl", '{"header"'),
        ("name: b\n", ".txt", ".yaml, .yml or .jsonl"),
    ],
)
def test_a_set_without_a_usable_header_or_format_is_refused(
    tmp_path: Path, content: str, suffix: str, complaint: str
) -> None:
    with pytest.raises(QuestionSetError) as refused:
        load_question_set(_write(tmp_path, f"bad{suffix}", content))
    assert complaint in str(refused.value)


def test_routes_are_pass_profile_or_intent_and_a_decision_maps_onto_one() -> None:
    assert parse_route(" profile:billing ") == "profile:billing"
    assert parse_route("intent:refund") == "intent:refund"
    assert parse_route("pass") == "pass"
    assert route_of(RouteDecision(kind=RouteKind.ROUTE, profile_id="b")) == "profile:b"
    assert route_of(RouteDecision(kind=RouteKind.INTENT, intent="r")) == "intent:r"
    assert route_of(RouteDecision(kind=RouteKind.PASS)) == "pass"


# --- the keyword router --------------------------------------------------------------------------


def test_the_keyword_router_takes_the_first_matching_rule_ignoring_case_and_accents() -> None:
    router = KeywordRouter(
        [("profile:billing", ["factura"]), ("intent:opening_hours", ["horario", "factura"])]
    )

    def ask(text: str) -> str:
        decision = asyncio.run(router.route("kw", RouteRequest(text, "f", "eval", "eval")))
        return route_of(decision)

    assert ask("Mi FÁCTURA del horario") == "profile:billing"
    assert ask("¿cuál es el HORARIO?") == "intent:opening_hours"
    assert ask("hola") == "pass"


# --- the metrics --------------------------------------------------------------------------------


def _scored(timeout_ms: int = 200) -> tuple[RoutingComparison, FakeClock]:
    clock = FakeClock()
    table: dict[str, object] = {
        "bill": RouteDecision(kind=RouteKind.ROUTE, profile_id="billing"),
        "hours": RouteDecision(kind=RouteKind.INTENT, intent="opening_hours"),
        "wrong": RouteDecision(kind=RouteKind.ROUTE, profile_id="billing"),
        "unsure": RouteDecision(kind=RouteKind.PASS, label="no_match"),
        "slow": "hang",
        "broken": ConnectionError("token=secret"),
    }
    question_set = _set(
        ("bill", "profile:billing"),
        ("hours", "intent:opening_hours"),
        ("wrong", "intent:opening_hours"),
        ("unsure", "pass"),
        ("slow", "profile:billing"),
        ("broken", "pass"),
    )
    comparison = asyncio.run(
        run_routing(
            question_set,
            router=TableRouter(clock, table),
            router_name="table",
            timeout_ms=timeout_ms,
            clock=clock,
        )
    )
    return comparison, clock


def test_routing_on_reports_accuracy_confusion_fallback_and_abstention() -> None:
    routed = _scored()[0].routed

    assert [v.got for v in routed.verdicts] == [
        "profile:billing",
        "intent:opening_hours",
        "profile:billing",
        "pass",
        "pass",
        "pass",
    ]
    assert [v.outcome for v in routed.verdicts] == [
        "answered",
        "answered",
        "answered",
        "abstained",
        "fallback:timeout",
        "fallback:error:ConnectionError",
    ]
    assert routed.accuracy == pytest.approx(4 / 6)  # bill, hours, unsure and broken are right
    assert routed.answered_accuracy == pytest.approx(2 / 3)
    assert routed.fallback_rate == pytest.approx(2 / 6)
    assert routed.abstention_rate == pytest.approx(1 / 6)
    assert routed.confusion == {
        ("intent:opening_hours", "intent:opening_hours"): 1,
        ("intent:opening_hours", "profile:billing"): 1,
        ("pass", "pass"): 2,
        ("profile:billing", "pass"): 1,
        ("profile:billing", "profile:billing"): 1,
    }


def test_routing_off_is_the_baseline_every_question_takes_the_normal_path() -> None:
    comparison = _scored()[0]
    baseline = comparison.baseline

    assert baseline.router == "off" and {v.outcome for v in baseline.verdicts} == {"off"}
    assert {v.got for v in baseline.verdicts} == {"pass"}
    assert baseline.accuracy == pytest.approx(2 / 6)
    assert baseline.fallback_rate == 0 and baseline.abstention_rate == 0
    assert baseline.latency("router") == {"p50": None, "p95": None}
    assert comparison.accuracy_delta == pytest.approx(2 / 6)


def test_router_latency_is_reported_as_nearest_rank_p50_and_p95() -> None:
    assert percentile([], 50) is None
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0
    assert percentile([float(n) for n in range(1, 21)], 95) == 19.0

    routed = _scored()[0].routed
    latency = routed.latency("router")
    assert latency["p50"] == pytest.approx(10.0)
    assert latency["p95"] is not None and latency["p95"] >= 10.0


def test_with_a_turn_driver_the_whole_turn_is_timed_and_compared() -> None:
    clock = FakeClock()
    question_set = _set(("bill", "profile:billing"), ("hello", "pass"), ("fails", "pass"))
    table: dict[str, object] = {
        "bill": RouteDecision(kind=RouteKind.ROUTE, profile_id="billing"),
        "hello": RouteDecision(kind=RouteKind.PASS),
        "fails": RouteDecision(kind=RouteKind.PASS),
    }
    turns = FakeTurns(clock, failing="q3")

    comparison = asyncio.run(
        run_routing(
            question_set,
            router=TableRouter(clock, table),
            router_name="table",
            turn=turns,
            clock=clock,
        )
    )

    assert turns.ran[:3] == [
        ("q1", "pass", "front_desk"),
        ("q2", "pass", "front_desk"),
        ("q3", "pass", "front_desk"),
    ]
    assert turns.ran[3][:2] == ("q1", "profile:billing")
    # Off: three 100 ms turns. On: 10 ms routing + 40 ms (routed) or + 100 ms.
    assert [v.turn_ms for v in comparison.baseline.verdicts] == pytest.approx([100.0] * 3)
    assert [v.turn_ms for v in comparison.routed.verdicts] == pytest.approx([50.0, 110.0, 110.0])
    assert comparison.routed.verdicts[2].turn_error == "RuntimeError: the turn failed"
    assert comparison.turn_delta() == pytest.approx({"p50": 10.0, "p95": 10.0})


def test_the_report_is_written_as_json_and_markdown(tmp_path: Path) -> None:
    comparison = _scored()[0]

    json_path, markdown_path = write_routing_report(comparison, tmp_path / "out")

    report = json.loads(json_path.read_text())
    assert report["synthetic"] is True and report["questions"] == 6
    assert report["router"]["accuracy"] == pytest.approx(4 / 6)
    assert report["baseline"]["router"] == "off"
    assert {"expected": "intent:opening_hours", "got": "profile:billing", "count": 1} in (
        report["router"]["confusion"]
    )
    assert "secret" not in json_path.read_text()
    markdown = markdown_path.read_text()
    assert "SYNTHETIC" in markdown and "| accuracy | 33% | 67% |" in markdown
    assert "q3: expected intent:opening_hours, got profile:billing" in markdown


def test_a_router_timeout_below_one_millisecond_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1 ms"):
        asyncio.run(
            run_routing(
                _set(("a", "pass")), router=KeywordRouter([]), router_name="k", timeout_ms=0
            )
        )
