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
    latency p50/p95     of the router, and of the whole turn when a `TurnDriver` runs each turn.

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
_QUESTION_KEYS: Final = frozenset({"id", "text", "expected", "note"})
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
        questions.append(Question(qid, _text(path, where, item, "text"), expected, note.strip()))
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
    `intent:<name>`), and returns when the turn is over. What it measures is wall time."""

    async def run(self, question: Question, route: str, entry_profile: str) -> None: ...


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

    @property
    def correct(self) -> bool:
        return self.expected == self.got


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

    def latency(self, which: str) -> dict[str, float | None]:
        values = [
            value
            for v in self.verdicts
            if (value := v.router_ms if which == "router" else v.turn_ms) is not None
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
) -> Verdict:
    got, outcome, router_ms = PASS, "off", None
    if router is not None:
        request = RouteRequest(
            text=question.text,
            profile_id=entry_profile,
            channel=EVAL_CHANNEL,
            tenant_id=EVAL_TENANT,
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
        router_ms = (clock() - started) * 1000
    turn_ms, turn_error = None, None
    if turn is not None:
        started = clock()
        try:
            await turn.run(question, got, entry_profile)
        except Exception as error:
            turn_error = f"{type(error).__name__}: {error}"
        turn_ms = (clock() - started) * 1000 + (router_ms or 0.0)
    return Verdict(question.id, question.expected, got, outcome, router_ms, turn_ms, turn_error)


async def run_routing(
    question_set: QuestionSet,
    *,
    router: TurnRouter,
    router_name: str,
    timeout_ms: int = 1500,
    turn: TurnDriver | None = None,
    clock: Callable[[], float] = time.monotonic,
    notes: Sequence[str] = (),
) -> RoutingComparison:
    """The set twice, one question at a time: routing off (baseline), then routing on."""
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
            }
            for v in run.verdicts
        ],
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
    return "\n".join(lines) + "\n"


def write_routing_report(comparison: RoutingComparison, directory: Path) -> tuple[Path, Path]:
    """`routing.json` and `routing.md` in `directory` (created)."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = directory / "routing.json", directory / "routing.md"
    json_path.write_text(json.dumps(render_routing_json(comparison), indent=2) + "\n")
    markdown_path.write_text(render_routing_markdown(comparison))
    return json_path, markdown_path
