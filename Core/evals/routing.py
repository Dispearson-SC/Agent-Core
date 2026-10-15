"""The routing benchmark (D95, Phase 5 P4): how well, and how fast, a turn router routes.

A QUESTION SET is a file of labelled questions, each with the route it SHOULD take:

    pass               the normal path (the entry profile reasons about it),
    profile:<id>       straight to that profile (S6 layer A),
    intent:<name>      a typed intent a code executor runs (S6 layer B).

The benchmark asks a `TurnRouter` (the same port production uses) about every question with the
set's entry profile, and compares what it answered with the label. The same set runs twice:
routing OFF (the baseline: every turn takes the normal path, as it does with the flag off) and
routing ON. The report carries, per run:

    accuracy            share of questions whose route equals the label,
    answered accuracy   the same over the questions the router committed to (not a pass),
    confusion           expected route x route taken, counted,
    fallback rate       share where the router gave no answer (timeout, error): the normal path,
    abstention rate     share where the router answered `pass` (it declined to route),
    latency p50/p95     of the router, and of the whole turn when a `TurnDriver` runs each turn
                        (a turn that failed is counted in `failed turns`, never timed),

and with the fast path (S10, `run_routing(..., fast_path=True)`, the router asked its extra
questions in the same call):

    intent set exact match   share of the assessed questions whose detected intents (at or above
                             the intent threshold, or the route choice's own intent at or above
                             the route threshold, as production decides) equal the expected set
                             (as sets: the order either side lists them in does not matter),
    combined set match       the same over the questions expecting two or more intents,
    intent precision/recall  micro-averaged over every (question, intent) pair,
    recall per intent        that recall for each expected intent alone (P13: one intent that is
                             never detected hides inside the micro average),
    combined composites      of the questions expecting two or more intents, how many the fast
                             path would answer as a composite of exactly that set (P13),
    fast path verdicts       how many questions each verdict got (`domain/fast_path.py`, no
                             history, the default thresholds but the intent one),

Nothing here knows a vertical. A set file is data; the scripted router (`KeywordRouter`) reads its
rules from the set; a real router is an addon's (`provides.routers`), loaded by the CLI.

FILE FORMATS (`load_question_set`)

YAML (`.yaml`/`.yml`), one document:

    name: my_set                        # required
    synthetic: true                     # required: true when nobody actually asked these
    description: "..."                  # optional
    entry_profile: front_desk           # required: the profile a person starts the turn on
    keyword_router:                     # optional: the offline scripted router, first match wins
      - route: "profile:billing"
        keywords: [invoice, refund]
    questions:
      - id: q1                          # unique within the set
        text: "Where is my invoice?"
        expected: "profile:billing"
        note: "optional, free text"
        intents: [refund]               # optional: the intents the question asks for (S10);
                                        # default: [<name>] for `intent:<name>`, else none

JSONL (`.jsonl`): one JSON object per line. The FIRST line is `{"header": {...}}` with the header
keys above (everything but `questions`); every other line is one question object. Blank lines are
ignored. Unknown keys, a duplicate id, an empty text or a malformed route refuse the whole file,
naming the file and the question.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

from agent_core.domain.fast_path import (
    COMPOSITE,
    Assessment,
    FastPathThresholds,
    decide_fast_path,
)
from agent_core.domain.routing import RouteDecision, RouteKind, RouteRequest
from agent_core.ports.turn_router import TurnRouter

__all__ = [
    "PASS",
    "KeywordRouter",
    "Question",
    "QuestionSet",
    "QuestionSetError",
    "RoutingComparison",
    "RoutingRun",
    "TurnDriver",
    "Verdict",
    "load_question_set",
    "parse_route",
    "percentile",
    "render_routing_json",
    "render_routing_markdown",
    "route_of",
    "run_routing",
    "write_routing_report",
]

PASS: Final = "pass"
_PREFIXES: Final = ("profile:", "intent:")
_HEADER_KEYS: Final = frozenset(
    {"name", "synthetic", "description", "entry_profile", "keyword_router"}
)
_QUESTION_KEYS: Final = frozenset({"id", "text", "expected", "note", "intents"})
_RULE_KEYS: Final = frozenset({"route", "keywords"})
EVAL_CHANNEL: Final = "eval"
EVAL_TENANT: Final = "eval"


class QuestionSetError(ValueError):
    """A question set file that cannot be used, with where and why."""


def parse_route(raw: object) -> str:
    """`raw` as a route label (`pass`, `profile:<id>`, `intent:<name>`), or ValueError."""
    if not isinstance(raw, str):
        raise ValueError(f"a route must be a string, got {type(raw).__name__}")
    route = raw.strip()
    if route == PASS:
        return route
    for prefix in _PREFIXES:
        if route.startswith(prefix) and route[len(prefix) :].strip():
            return prefix + route[len(prefix) :].strip()
    raise ValueError(f"a route is 'pass', 'profile:<id>' or 'intent:<name>', got {raw!r}")


def route_of(decision: RouteDecision) -> str:
    """The route label a router's decision stands for."""
    match decision.kind:
        case RouteKind.ROUTE:
            return f"profile:{decision.profile_id}"
        case RouteKind.INTENT:
            return f"intent:{decision.intent}"
        case _:
            return PASS


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    text: str
    expected: str
    note: str = ""
    # S10. The intents the question asks for; None: implied by `expected`.
    intents: tuple[str, ...] | None = None

    @property
    def expected_intents(self) -> tuple[str, ...]:
        """The expected intent set, sorted: the declared one, else the `intent:` route's, else
        none."""
        if self.intents is not None:
            return tuple(sorted(set(self.intents)))
        if self.expected.startswith("intent:"):
            return (self.expected.removeprefix("intent:"),)
        return ()


@dataclass(frozen=True, slots=True)
class QuestionSet:
    name: str
    synthetic: bool
    entry_profile: str
    questions: tuple[Question, ...]
    description: str = ""
    # (route, keywords) in order: the offline scripted router's rules. Empty when none.
    keyword_rules: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _refuse(path: Path, where: str, reason: str) -> QuestionSetError:
    return QuestionSetError(f"{path}: {where}: {reason}")


def _text(path: Path, where: str, mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _refuse(path, where, f"{key!r} must be a non-empty string")
    return value.strip()


def _header(path: Path, header: object) -> dict[str, Any]:
    if not isinstance(header, Mapping):
        raise _refuse(path, "header", "must be a mapping")
    unknown = set(header) - _HEADER_KEYS
    if unknown:
        raise _refuse(path, "header", f"unknown keys {sorted(map(str, unknown))}")
    if not isinstance(header.get("synthetic"), bool):
        raise _refuse(path, "header", "'synthetic' must be true or false")
    description = header.get("description", "")
    if not isinstance(description, str):
        raise _refuse(path, "header", "'description' must be a string")
    return {
        "name": _text(path, "header", header, "name"),
        "synthetic": header["synthetic"],
        "description": description.strip(),
        "entry_profile": _text(path, "header", header, "entry_profile"),
        "keyword_rules": _rules(path, header.get("keyword_router") or []),
    }


def _rules(path: Path, raw: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(raw, list):
        raise _refuse(path, "keyword_router", "must be a list of {route, keywords}")
    rules: list[tuple[str, tuple[str, ...]]] = []
    for index, rule in enumerate(raw, 1):
        where = f"keyword_router rule {index}"
        if not isinstance(rule, Mapping) or set(rule) != _RULE_KEYS:
            raise _refuse(path, where, "must have exactly 'route' and 'keywords'")
        try:
            route = parse_route(rule["route"])
        except ValueError as error:
            raise _refuse(path, where, str(error)) from None
        keywords = rule["keywords"]
        if (
            not isinstance(keywords, list)
            or not keywords
            or not all(isinstance(k, str) and k.strip() for k in keywords)
        ):
            raise _refuse(path, where, "'keywords' must be a non-empty list of strings")
        rules.append((route, tuple(_fold(k) for k in keywords)))
    return tuple(rules)


def _questions(path: Path, raw: object) -> tuple[Question, ...]:
    if not isinstance(raw, list) or not raw:
        raise _refuse(path, "questions", "must be a non-empty list")
    seen: set[str] = set()
    questions: list[Question] = []
    for index, item in enumerate(raw, 1):
        where = f"question {index}"
        if not isinstance(item, Mapping):
            raise _refuse(path, where, "must be a mapping")
        unknown = set(item) - _QUESTION_KEYS
        if unknown:
            raise _refuse(path, where, f"unknown keys {sorted(map(str, unknown))}")
        qid = _text(path, where, item, "id")
        where = f"question {qid!r}"
        if qid in seen:
            raise _refuse(path, where, "duplicate id")
        seen.add(qid)
        try:
            expected = parse_route(item.get("expected"))
        except ValueError as error:
            raise _refuse(path, where, str(error)) from None
        note = item.get("note", "")
        if not isinstance(note, str):
            raise _refuse(path, where, "'note' must be a string")
        intents = item.get("intents")
        if intents is not None and (
            not isinstance(intents, list)
            or not all(isinstance(i, str) and i.strip() for i in intents)
        ):
            raise _refuse(path, where, "'intents' must be a list of intent names")
        questions.append(
            Question(
                qid,
                _text(path, where, item, "text"),
                expected,
                note.strip(),
                None if intents is None else tuple(i.strip() for i in intents),
            )
        )
    return tuple(questions)


def load_question_set(path: Path) -> QuestionSet:
    """The question set in `path` (`.yaml`, `.yml` or `.jsonl`; see the module docstring)."""
    suffix = path.suffix.lower()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise QuestionSetError(f"{path}: cannot read: {error.strerror}") from None
    if suffix in (".yaml", ".yml"):
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as error:
            raise QuestionSetError(f"{path}: not valid YAML: {error}") from None
        if not isinstance(document, Mapping):
            raise _refuse(path, "document", "must be a mapping")
        header = _header(path, {k: v for k, v in document.items() if k != "questions"})
        questions = _questions(path, document.get("questions"))
    elif suffix == ".jsonl":
        lines = [line for line in content.splitlines() if line.strip()]
        rows: list[Any] = []
        for number, line in enumerate(lines, 1):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise _refuse(path, f"line {number}", f"not valid JSON: {error.msg}") from None
        if not rows or not isinstance(rows[0], Mapping) or set(rows[0]) != {"header"}:
            raise _refuse(path, "line 1", 'must be {"header": {...}}')
        header = _header(path, rows[0]["header"])
        questions = _questions(path, rows[1:])
    else:
        raise QuestionSetError(f"{path}: a question set is .yaml, .yml or .jsonl")
    return QuestionSet(questions=questions, **header)


def _fold(text: str) -> str:
    """Lower case without accents, so a keyword matches however the person typed it."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


class KeywordRouter:
    """A deterministic `TurnRouter` from a set's `keyword_router` rules: the first rule with a
    keyword in the text decides; none matching is a `pass`. Offline: the scripted router of the
    benchmark, a floor any real router should beat, never a production router."""

    MODEL: Final = "keyword"

    def __init__(self, rules: Sequence[tuple[str, Sequence[str]]]) -> None:
        self._rules = tuple((route, tuple(_fold(k) for k in kws)) for route, kws in rules)

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        text = _fold(request.text)
        for route, keywords in self._rules:
            if any(keyword in text for keyword in keywords):
                return _decision(route, label=route, model=self.MODEL)
        return RouteDecision(kind=RouteKind.PASS, label="no_match", model=self.MODEL)


def _decision(route: str, *, label: str, model: str) -> RouteDecision:
    if route.startswith("profile:"):
        return RouteDecision(
            kind=RouteKind.ROUTE,
            profile_id=route.removeprefix("profile:"),
            label=label,
            confidence=1.0,
            model=model,
        )
    if route.startswith("intent:"):
        return RouteDecision(
            kind=RouteKind.INTENT,
            intent=route.removeprefix("intent:"),
            label=label,
            confidence=1.0,
            model=model,
        )
    return RouteDecision(kind=RouteKind.PASS, label=label, confidence=1.0, model=model)


class TurnDriver(Protocol):
    """Runs one whole turn for a question along the route it took (`pass`, `profile:<id>`,
    `intent:<name>`), and returns when the turn is over. What it measures is wall time. For an
    intent route it may return how the deployment's intent plan ended (`plan` or
    `fallback:<reason>`, D97); None when it does not know or no plan ran."""

    async def run(self, question: Question, route: str, entry_profile: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    """One question, one run. `outcome` is `off` (routing off), `answered`, `abstained` (the
    router said pass) or `fallback:<reason>` (no answer: the normal path ran)."""

    question_id: str
    expected: str
    got: str
    outcome: str
    router_ms: float | None = None
    turn_ms: float | None = None
    turn_error: str | None = None
    # D97. How the deployment's intent plan ended for this turn (`plan`, `fallback:<reason>`).
    plan: str | None = None
    # S10. The expected intent set, the detected one (None: no assessment) and the fast path's
    # verdict on the assessment.
    expected_intents: tuple[str, ...] = ()
    intents: tuple[str, ...] | None = None
    fast_path: str | None = None
    # S10. Every raw answer of the assessment (None: none), so thresholds tune from one run.
    assessment: Assessment | None = None

    @property
    def correct(self) -> bool:
        return self.expected == self.got


def _same_set(verdict: Verdict) -> bool:
    return set(verdict.intents or ()) == set(verdict.expected_intents)


def percentile(values: Sequence[float], q: float) -> float | None:
    """The nearest-rank `q`-th percentile (0 < q <= 100) of `values`; None when empty."""
    if not values:
        return None
    ranked = sorted(values)
    return ranked[max(0, math.ceil(q / 100 * len(ranked)) - 1)]


@dataclass(frozen=True, slots=True)
class RoutingRun:
    mode: str  # "baseline" (routing off) or "router"
    router: str
    verdicts: tuple[Verdict, ...]

    def _share(self, predicate: Callable[[Verdict], bool]) -> float:
        return sum(1 for v in self.verdicts if predicate(v)) / len(self.verdicts)

    @property
    def accuracy(self) -> float:
        return self._share(lambda v: v.correct)

    @property
    def answered_accuracy(self) -> float | None:
        answered = [v for v in self.verdicts if v.outcome == "answered"]
        return sum(1 for v in answered if v.correct) / len(answered) if answered else None

    @property
    def fallback_rate(self) -> float:
        return self._share(lambda v: v.outcome.startswith("fallback:"))

    @property
    def abstention_rate(self) -> float:
        return self._share(lambda v: v.outcome == "abstained")

    @property
    def confusion(self) -> dict[tuple[str, str], int]:
        """(expected, got) -> how many questions, wrong cells included."""
        return dict(sorted(Counter((v.expected, v.got) for v in self.verdicts).items()))

    @property
    def plans(self) -> dict[str, int]:
        """Among the turns that ran on an intent route: how many the deployment answered with
        the intent's plan, how many fell back to the model path, and how many reported no plan."""
        ran = [
            v for v in self.verdicts
            if v.got.startswith("intent:") and v.turn_ms is not None and v.turn_error is None
        ]
        fallback = sum(1 for v in ran if (v.plan or "").startswith("fallback:"))
        used = sum(1 for v in ran if v.plan == "plan")
        return {"plan": used, "fallback": fallback, "no_plan": len(ran) - used - fallback}

    @property
    def turn_errors(self) -> int:
        """Turns that failed (or never finished): reported apart, left out of the turn latency."""
        return sum(1 for v in self.verdicts if v.turn_error is not None)

    def _assessed(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.intents is not None]

    @property
    def intent_set_accuracy(self) -> float | None:
        """S10. Exact match of detected and expected intent sets, over the assessed questions."""
        assessed = self._assessed()
        if not assessed:
            return None
        return sum(1 for v in assessed if _same_set(v)) / len(assessed)

    def _combined(self) -> list[Verdict]:
        return [v for v in self._assessed() if len(set(v.expected_intents)) >= 2]

    @property
    def combined_set_accuracy(self) -> float | None:
        """S10. `intent_set_accuracy` over the questions expecting two or more intents."""
        combined = self._combined()
        if not combined:
            return None
        return sum(1 for v in combined if _same_set(v)) / len(combined)

    @property
    def combined_composites(self) -> tuple[int, int]:
        """P13. (composites of exactly the expected set, questions expecting two or more)."""
        combined = self._combined()
        hits = sum(1 for v in combined if v.fast_path == COMPOSITE and _same_set(v))
        return hits, len(combined)

    @property
    def intent_recall_by_intent(self) -> dict[str, float]:
        """P13. For each intent some question expects: the share of those questions detecting it."""
        expected: Counter[str] = Counter()
        found: Counter[str] = Counter()
        for v in self._assessed():
            for name in set(v.expected_intents):
                expected[name] += 1
                found[name] += name in (v.intents or ())
        return {name: found[name] / expected[name] for name in sorted(expected)}

    def _pairs(self) -> tuple[int, int, int]:
        true_positive = predicted = expected = 0
        for v in self._assessed():
            got = set(v.intents or ())
            want = set(v.expected_intents)
            true_positive += len(got & want)
            predicted += len(got)
            expected += len(want)
        return true_positive, predicted, expected

    @property
    def intent_precision(self) -> float | None:
        true_positive, predicted, _ = self._pairs()
        return true_positive / predicted if predicted else None

    @property
    def intent_recall(self) -> float | None:
        true_positive, _, expected = self._pairs()
        return true_positive / expected if expected else None

    @property
    def fast_path_verdicts(self) -> dict[str, int]:
        return dict(sorted(Counter(v.fast_path for v in self.verdicts if v.fast_path).items()))

    def latency(self, which: str) -> dict[str, float | None]:
        if which == "router":
            values = [v.router_ms for v in self.verdicts if v.router_ms is not None]
        else:
            values = [
                v.turn_ms for v in self.verdicts if v.turn_ms is not None and v.turn_error is None
            ]
        return {"p50": percentile(values, 50), "p95": percentile(values, 95)}


@dataclass(frozen=True, slots=True)
class RoutingComparison:
    question_set: str
    synthetic: bool
    questions: int
    started_at: str
    baseline: RoutingRun
    routed: RoutingRun
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def accuracy_delta(self) -> float:
        return self.routed.accuracy - self.baseline.accuracy

    def turn_delta(self) -> dict[str, float | None]:
        """Routed minus baseline whole-turn latency (negative is faster); None without turns."""
        before, after = self.baseline.latency("turn"), self.routed.latency("turn")
        return {
            key: None if before[key] is None or after[key] is None else after[key] - before[key]  # type: ignore[operator]
            for key in ("p50", "p95")
        }


async def _verdict(
    question: Question,
    entry_profile: str,
    *,
    router: TurnRouter | None,
    router_name: str,
    timeout: float,
    turn: TurnDriver | None,
    clock: Callable[[], float],
    fast_path: FastPathThresholds | None = None,
) -> Verdict:
    got, outcome, router_ms = PASS, "off", None
    detected: tuple[str, ...] | None = None
    verdict: str | None = None
    assessment: Assessment | None = None
    if router is not None:
        request = RouteRequest(
            text=question.text,
            profile_id=entry_profile,
            channel=EVAL_CHANNEL,
            tenant_id=EVAL_TENANT,
            fast_path=fast_path is not None,
        )
        started = clock()
        try:
            decision = await asyncio.wait_for(router.route(router_name, request), timeout)
        except TimeoutError:
            outcome = "fallback:timeout"
        except Exception as error:
            outcome = f"fallback:error:{type(error).__name__}"
        else:
            got = route_of(decision)
            outcome = "abstained" if got == PASS else "answered"
            if fast_path is not None and decision.assessment is not None:
                assessment = decision.assessment
                routed = (
                    (decision.intent, decision.confidence)
                    if decision.kind is RouteKind.INTENT
                    and decision.intent
                    and decision.confidence is not None
                    else None
                )
                decided = decide_fast_path(
                    assessment, fast_path, has_history=False, routed=routed
                )
                detected, verdict = decided.intents, decided.code
        router_ms = (clock() - started) * 1000
    turn_ms, turn_error, plan = None, None, None
    if turn is not None:
        started = clock()
        try:
            plan = await turn.run(question, got, entry_profile)
        except Exception as error:
            turn_error = f"{type(error).__name__}: {error}"
        turn_ms = (clock() - started) * 1000 + (router_ms or 0.0)
    return Verdict(
        question.id,
        question.expected,
        got,
        outcome,
        router_ms,
        turn_ms,
        turn_error,
        plan,
        question.expected_intents,
        detected,
        verdict,
        assessment,
    )


async def run_routing(
    question_set: QuestionSet,
    *,
    router: TurnRouter,
    router_name: str,
    timeout_ms: int = 1500,
    turn: TurnDriver | None = None,
    clock: Callable[[], float] = time.monotonic,
    notes: Sequence[str] = (),
    fast_path: bool = False,
    intent_threshold: float = 0.5,
) -> RoutingComparison:
    """The set twice, one question at a time: routing off (baseline), then routing on. With
    `fast_path` the router is asked its extra questions (S10) and the intent sets are scored."""
    if timeout_ms < 1:
        raise ValueError(f"the router timeout must be at least 1 ms, got {timeout_ms}")
    runs: list[RoutingRun] = []
    for mode, consulted in (("baseline", None), ("router", router)):
        verdicts = [
            await _verdict(
                question,
                question_set.entry_profile,
                router=consulted,
                router_name=router_name,
                timeout=timeout_ms / 1000,
                turn=turn,
                clock=clock,
                fast_path=FastPathThresholds(intent=intent_threshold) if fast_path else None,
            )
            for question in question_set.questions
        ]
        runs.append(RoutingRun(mode, router_name if consulted else "off", tuple(verdicts)))
    return RoutingComparison(
        question_set=question_set.name,
        synthetic=question_set.synthetic,
        questions=len(question_set.questions),
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        baseline=runs[0],
        routed=runs[1],
        notes=tuple(notes),
    )


def _run_json(run: RoutingRun) -> dict[str, Any]:
    return {
        "mode": run.mode,
        "router": run.router,
        "accuracy": run.accuracy,
        "answered_accuracy": run.answered_accuracy,
        "fallback_rate": run.fallback_rate,
        "abstention_rate": run.abstention_rate,
        "router_latency_ms": run.latency("router"),
        "turn_latency_ms": run.latency("turn"),
        "turn_errors": run.turn_errors,
        "plans": run.plans,
        "intent_set_accuracy": run.intent_set_accuracy,
        "combined_set_accuracy": run.combined_set_accuracy,
        "intent_precision": run.intent_precision,
        "intent_recall": run.intent_recall,
        "intent_recall_by_intent": run.intent_recall_by_intent,
        "combined_composites": list(run.combined_composites),
        "fast_path_verdicts": run.fast_path_verdicts,
        "confusion": [
            {"expected": expected, "got": got, "count": count}
            for (expected, got), count in run.confusion.items()
        ],
        "verdicts": [
            {
                "id": v.question_id,
                "expected": v.expected,
                "got": v.got,
                "outcome": v.outcome,
                "router_ms": v.router_ms,
                "turn_ms": v.turn_ms,
                "turn_error": v.turn_error,
                "plan": v.plan,
                "expected_intents": list(v.expected_intents),
                "intents": None if v.intents is None else list(v.intents),
                "fast_path": v.fast_path,
                "assessment": _assessment_json(v.assessment),
            }
            for v in run.verdicts
        ],
    }


def _assessment_json(assessment: Assessment | None) -> dict[str, Any] | None:
    if assessment is None:
        return None
    return {
        "intents": dict(assessment.intents),
        "action": assessment.action,
        "follow_up": assessment.follow_up,
        "complexity": assessment.complexity,
        "requests": assessment.requests,
    }


def render_routing_json(comparison: RoutingComparison) -> dict[str, Any]:
    return {
        "question_set": comparison.question_set,
        "synthetic": comparison.synthetic,
        "questions": comparison.questions,
        "started_at": comparison.started_at,
        "notes": list(comparison.notes),
        "accuracy_delta": comparison.accuracy_delta,
        "turn_latency_delta_ms": comparison.turn_delta(),
        "baseline": _run_json(comparison.baseline),
        "router": _run_json(comparison.routed),
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def render_routing_markdown(comparison: RoutingComparison) -> str:
    base, routed = comparison.baseline, comparison.routed
    lines = [
        "# Routing benchmark",
        "",
        f"- question set: `{comparison.question_set}` ({comparison.questions} questions"
        + (", SYNTHETIC" if comparison.synthetic else "")
        + ")",
        f"- router: `{routed.router}`",
        f"- started: {comparison.started_at}",
        *(f"- note: {note}" for note in comparison.notes),
        "",
        "| metric | routing off | routing on |",
        "|---|---|---|",
        f"| accuracy | {_pct(base.accuracy)} | {_pct(routed.accuracy)} |",
        f"| answered accuracy | - | {_pct(routed.answered_accuracy)} |",
        f"| fallback rate | {_pct(base.fallback_rate)} | {_pct(routed.fallback_rate)} |",
        f"| abstention rate | - | {_pct(routed.abstention_rate)} |",
    ]
    for which in ("router", "turn"):
        for key in ("p50", "p95"):
            lines.append(
                f"| {which} latency {key} (ms) | {_ms(base.latency(which)[key])} "
                f"| {_ms(routed.latency(which)[key])} |"
            )
    if routed.intent_set_accuracy is not None:
        lines += [
            f"| intent set exact match | - | {_pct(routed.intent_set_accuracy)} |",
            f"| combined set match (2+ intents) | - | {_pct(routed.combined_set_accuracy)} |",
            f"| intent precision / recall | - | {_pct(routed.intent_precision)} / "
            f"{_pct(routed.intent_recall)} |",
            "| recall per intent | - | "
            + ", ".join(f"{k} {_pct(n)}" for k, n in routed.intent_recall_by_intent.items())
            + " |",
            "| combined questions answered by an exact composite | - | "
            f"{routed.combined_composites[0]} / {routed.combined_composites[1]} |",
            "| fast path verdicts | - | "
            + ", ".join(f"{k} {n}" for k, n in routed.fast_path_verdicts.items())
            + " |",
        ]
    if any(v.turn_ms is not None for v in (*base.verdicts, *routed.verdicts)):
        lines.append(f"| failed turns | {base.turn_errors} | {routed.turn_errors} |")
        counts = [" / ".join(str(run.plans[k]) for k in ("plan", "fallback", "no_plan"))
                  for run in (base, routed)]
        lines.append(f"| intent turns: plan / fallback / no plan | {counts[0]} | {counts[1]} |")
    lines += ["", "## Confusion (routing on)", "", "| expected | got | count |", "|---|---|---|"]
    lines.extend(
        f"| {expected} | {got}{'' if expected == got else ' (wrong)'} | {count} |"
        for (expected, got), count in routed.confusion.items()
    )
    wrong = [v for v in routed.verdicts if not v.correct]
    if wrong:
        lines += ["", "## Misrouted (routing on)", ""]
        lines.extend(
            f"- {v.question_id}: expected {v.expected}, got {v.got} ({v.outcome})" for v in wrong
        )
    missed = [
        v for v in routed.verdicts if v.intents is not None and not _same_set(v)
    ]
    if missed:
        lines += ["", "## Intent sets that differ (fast path)", ""]
        lines.extend(
            f"- {v.question_id}: expected [{', '.join(v.expected_intents)}], got "
            f"[{', '.join(v.intents or ())}] ({v.fast_path})"
            for v in missed
        )
    return "\n".join(lines) + "\n"


def write_routing_report(comparison: RoutingComparison, directory: Path) -> tuple[Path, Path]:
    """`routing.json` and `routing.md` in `directory` (created)."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = directory / "routing.json", directory / "routing.md"
    json_path.write_text(
        json.dumps(render_routing_json(comparison), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_routing_markdown(comparison), encoding="utf-8")
    return json_path, markdown_path
