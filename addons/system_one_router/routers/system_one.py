"""A turn router over a System One classifier: the official Typesafe API (docs.typesafe.ai).

Plain Python on purpose: an addon never imports Core's domain, application or ports. `route` reads
the plain request mapping Core hands every router and answers a plain mapping (docs/ADDONS.md,
"Routers").

THE CALL
    POST <base_url>/v1/systemone, `Authorization: Bearer <key>`, body
    `{state, model, questions: {route: {type: choice, instructions, criteria}}}` where `criteria`
    maps every route label, and the escape label, to its description. The answer is
    `answers.route.{choice, confidence, probabilities}`, `model` and `usage`.

WHAT IT ANSWERS
    * a route label with confidence >= threshold: `route` (profile:<id>) or `intent`
      (intent:<name>);
    * the escape label, or a confidence below the threshold: `pass` with reason `escape` or
      `below_threshold`, the label and confidence kept so the abstention can be tuned;
    * a route whose target is `pass` (a labelled abstention, e.g. "asks to DO something"): `pass`
      with reason `pass_route` whatever its confidence. A specific option competes with the routes
      better than one broad escape description (L18);
    * no key (`missing_api_key`), an HTTP error (`http_<status>`), the time budget spent
      (`timeout`), a transport error (`network_error:<Class>`) or an answer that does not hold
      the contract (`bad_response`): `pass` with that reason.

    Every answer carries `calibrated: false` (Typesafe's confidence is the sharpness of the
    distribution, with no calibration claim), `elapsed_ms`, and when the API answered, `model`
    and `usage`. A configuration mistake (no routes file, a malformed one, an unknown state
    field) raises `SystemOneConfigError`: Core falls back to the normal path and records the
    error class, so a misconfiguration is visible instead of a silent pass.

THE FAST PATH (S10, Core's AGENT_CORE_FAST_PATH)
    When Core's request says `fast_path: true`, the SAME call also asks (the Typesafe fan-out
    pattern; questions run in parallel, so they cost little latency):
      * `intent__<label>`: one `noul` per `intent:` route, "does the message ask for this?" (the
        route's `question`, or the default built from its description), with the route's
        `criteria` (`{true, false}`: what a yes and a no mean, a string or a `{what, examples}`
        mapping) when it has them (P13: Typesafe's way to pin a subtle yes/no boundary);
      * `action`: a `noul`, does it ask to DO something rather than to be told;
      * `follow_up`: a `noul`, only when Core sent a recent conversation: does the message depend
        on it (the state is then `{message, recent_conversation}`);
      * `complexity`: a `score` over three levels (a routes file may name its own);
      * `requests`: a `score`, how many separate things the message asks for (P13; levels one, two,
        three, four or more unless the routes file names its own). Core's coverage guard passes a
        message that asks for more things than the intents it detected.
    The answer then carries `assessment: {intents: {<intent name>: p}, action, follow_up,
    complexity, requests}` (the complexity normalised to [0, 1]; `requests` the count, 1 for the
    first level: Typesafe's score is the expected level, rounded); the route choice is decided
    exactly as without it. A missing or malformed fast path answer drops the assessment only (reason
    `bad_assessment`): Core then never takes the fast path for that turn. Without the flag the
    request is exactly the one choice question.

RETRIES
    429 and 529 are retried once after `retry_backoff_ms`, and only when that retry still fits
    `timeout_ms`; anything else is not retried. The budget covers the whole call.

THE KEY
    Read from the environment variable `api_key_env` names on every call; never stored in the
    settings, never in an answer, never in a reason (a reason is a fixed code or a class name).

httpx directly rather than the `typesafe-sdk` package: httpx is already a Core dependency, the
contract is one POST, and the router must own its time budget (one retry only when it fits)
where the SDK's default policy retries with its own backoff.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import yaml  # noqa: TID251  # an addon is adapter code; Core's inner-layer ban

QUESTION_ID: Final = "route"
ENDPOINT: Final = "/v1/systemone"
STATE_FIELDS: Final = frozenset({"text", "profile_id", "channel", "modality"})
MAX_OPTIONS: Final = 255  # Typesafe's limit per choice question, escape option included
RETRY_STATUSES: Final = frozenset({429, 529})
_MAX_TEXT: Final = 200  # Core refuses longer label/model/reason strings
_LABEL: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TARGET: Final = re.compile(r"^(profile|intent):([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
PASS_TARGET: Final = "pass"
_ROUTE_KEYS: Final = frozenset({"label", "description", "target", "question", "criteria"})
_FILE_KEYS: Final = frozenset({"routes", "instructions", "escape_description", "fast_path"})
_FAST_KEYS: Final = frozenset(
    {"intent_question", "action_question", "follow_up_question", "complexity_question",
     "complexity_levels", "requests_question", "requests_levels"}
)  # fmt: skip
_LEVEL_KEYS: Final = frozenset({"complexity_levels", "requests_levels"})
INTENT_PREFIX: Final = "intent__"
DEFAULT_INTENT_QUESTION: Final = "Does this message ask for the following? {description}"
DEFAULT_ACTION_QUESTION: Final = (
    "Does this message ask the assistant to DO something (create, change, propose, approve, send, "
    "record or reject something) rather than only to be told information?"
)
DEFAULT_FOLLOW_UP_QUESTION: Final = (
    "Can `message` only be understood with `recent_conversation`: does it refer back to something "
    "said earlier (those, that one, and tomorrow?, why?) instead of standing on its own?"
)
DEFAULT_COMPLEXITY_QUESTION: Final = "How much work does answering this message take?"
DEFAULT_COMPLEXITY_LEVELS: Final = (
    "A direct lookup of one or a few facts",
    "Combines or compares several facts",
    "Needs analysis, advice, a plan, a judgement or a decision",
)
DEFAULT_REQUESTS_QUESTION: Final = "How many separate things does this message ask for?"
DEFAULT_REQUESTS_LEVELS: Final = (
    "It asks for one thing",
    "It asks for two different things",
    "It asks for three different things",
    "It asks for four or more different things",
)
_ADDONS_ROOT: Final = Path(__file__).resolve().parents[2]


class SystemOneConfigError(ValueError):
    """The router's configuration (settings or routes file) cannot work."""


@dataclass(frozen=True, slots=True)
class Route:
    label: str
    description: str
    kind: str  # "profile", "intent" or "pass"
    name: str
    question: str | None = None  # S10: the intent's own yes/no question
    criteria: dict[str, Any] | None = None  # P13: what a yes and a no mean for that question


@dataclass(frozen=True, slots=True)
class FastQuestions:
    """S10. The fast path's question texts (a routes file's `fast_path:` block, else defaults)."""

    intent_question: str = DEFAULT_INTENT_QUESTION
    action_question: str = DEFAULT_ACTION_QUESTION
    follow_up_question: str = DEFAULT_FOLLOW_UP_QUESTION
    complexity_question: str = DEFAULT_COMPLEXITY_QUESTION
    complexity_levels: tuple[str, ...] = DEFAULT_COMPLEXITY_LEVELS
    requests_question: str = DEFAULT_REQUESTS_QUESTION
    requests_levels: tuple[str, ...] = DEFAULT_REQUESTS_LEVELS


@dataclass(frozen=True, slots=True)
class RouteTable:
    routes: tuple[Route, ...]
    instructions: str | None
    escape_description: str | None
    fast: FastQuestions = FastQuestions()

    def by_label(self) -> dict[str, Route]:
        return {route.label: route for route in self.routes}


# Keyed by the file's version: modification time, size and inode. The time alone misses an edit
# that lands within the clock's granularity or restores the old time (slice 35).
_tables: dict[tuple[Path, int, int, int], RouteTable] = {}
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


# --- configuration -------------------------------------------------------------------------------


def resolve_routes_file(raw: str, *, addons_root: Path = _ADDONS_ROOT) -> Path:
    """The routes file a setting names: absolute as it is, relative from the addons root."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else addons_root / path


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemOneConfigError(f"{where} must be a non-empty string")
    return value.strip()


def parse_routes(document: object, *, escape_label: str, source: str) -> RouteTable:
    if not isinstance(document, Mapping) or "routes" not in document:
        raise SystemOneConfigError(f"routes file {source} must be a mapping with `routes`")
    extra = set(document) - _FILE_KEYS
    if extra:
        raise SystemOneConfigError(f"routes file {source} has unknown keys: {sorted(extra)}")
    raw_routes = document["routes"]
    if not isinstance(raw_routes, list) or not raw_routes:
        raise SystemOneConfigError(f"routes file {source} needs at least one route")
    if len(raw_routes) >= MAX_OPTIONS:
        raise SystemOneConfigError(
            f"routes file {source} has {len(raw_routes)} routes; at most {MAX_OPTIONS - 1}"
        )
    routes: list[Route] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_routes):
        where = f"routes file {source}, route {index + 1}"
        if not isinstance(raw, Mapping):
            raise SystemOneConfigError(f"{where} must be a mapping")
        extra = set(raw) - _ROUTE_KEYS
        if extra:
            raise SystemOneConfigError(f"{where} has extra keys: {sorted(map(str, extra))}")
        label = raw.get("label")
        if not isinstance(label, str) or not _LABEL.match(label):
            raise SystemOneConfigError(
                f"{where}: label must be lower case letters, digits and underscores"
            )
        if label == escape_label:
            raise SystemOneConfigError(f"{where}: {label!r} is the escape label")
        if label in seen:
            raise SystemOneConfigError(f"{where}: label {label!r} is used twice")
        seen.add(label)
        target = raw.get("target")
        description = _text(raw.get("description"), f"{where}: description")
        question = raw.get("question")
        if question is not None:
            question = _text(question, f"{where}: question")
        criteria = raw.get("criteria")
        if criteria is not None:
            if not (isinstance(target, str) and target.startswith("intent:")):
                raise SystemOneConfigError(f"{where}: criteria belong to an intent: route")
            criteria = _criteria(criteria, where)
        if target == PASS_TARGET:
            routes.append(Route(label, description, PASS_TARGET, ""))
            continue
        match = _TARGET.match(target) if isinstance(target, str) else None
        if match is None:
            raise SystemOneConfigError(
                f"{where}: target must be profile:<id>, intent:<name> or {PASS_TARGET}"
            )
        routes.append(
            Route(label, description, match.group(1), match.group(2), question, criteria)
        )
    instructions = document.get("instructions")
    escape = document.get("escape_description")
    return RouteTable(
        routes=tuple(routes),
        instructions=None if instructions is None else _text(instructions, "instructions"),
        escape_description=None if escape is None else _text(escape, "escape_description"),
        fast=_fast_questions(document.get("fast_path"), source),
    )


def _criteria(raw: object, where: str) -> dict[str, Any]:
    """P13. A noul's `{true, false}` criteria: each a non-empty string or a non-empty mapping
    (Typesafe's `{what, examples}`), sent as they are."""
    if not isinstance(raw, Mapping) or set(raw) != {True, False} and set(raw) != {"true", "false"}:
        raise SystemOneConfigError(f"{where}: criteria must be a mapping of exactly true and false")
    criteria: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key).lower()
        if isinstance(value, str):
            criteria[name] = _text(value, f"{where}: criteria {name}")
        elif isinstance(value, Mapping) and value:
            criteria[name] = dict(value)
        else:
            raise SystemOneConfigError(
                f"{where}: criteria {name} must be a non-empty string or mapping"
            )
    return criteria


def _fast_questions(raw: object, source: str) -> FastQuestions:
    if raw is None:
        return FastQuestions()
    where = f"routes file {source}, fast_path"
    if not isinstance(raw, Mapping):
        raise SystemOneConfigError(f"{where} must be a mapping")
    extra = set(raw) - _FAST_KEYS
    if extra:
        raise SystemOneConfigError(f"{where} has unknown keys: {sorted(map(str, extra))}")
    values: dict[str, Any] = {
        key: _text(raw[key], f"{where}: {key}") for key in _FAST_KEYS - _LEVEL_KEYS if key in raw
    }  # fmt: skip
    if "intent_question" in values and "{description}" not in values["intent_question"]:
        raise SystemOneConfigError(f"{where}: intent_question must contain {{description}}")
    for key in sorted(_LEVEL_KEYS & set(raw)):
        levels = raw[key]
        if not isinstance(levels, list) or not 2 <= len(levels) <= 10:
            raise SystemOneConfigError(f"{where}: {key} must list 2-10 levels")
        values[key] = tuple(_text(level, f"{where}: {key} level") for level in levels)
    return FastQuestions(**values)


def load_routes(settings: Mapping[str, Any]) -> RouteTable:
    """The routes file the settings name, parsed once per modification."""
    raw = settings.get("routes_file")
    if not isinstance(raw, str) or not raw.strip():
        raise SystemOneConfigError("the routes file setting `routes_file` is not set")
    path = resolve_routes_file(raw)
    try:
        status = path.stat()
    except OSError:
        raise SystemOneConfigError(f"routes file {path} cannot be read") from None
    stamp = (path, status.st_mtime_ns, status.st_size, status.st_ino)
    cached = _tables.get(stamp)
    if cached is not None:
        return cached
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SystemOneConfigError(
            f"routes file {path} is not readable YAML ({type(error).__name__})"
        ) from None
    table = parse_routes(document, escape_label=settings["escape_label"], source=str(path))
    _tables.clear()
    _tables[stamp] = table
    return table


def build_state(request: Mapping[str, Any], fields: list[str]) -> str | dict[str, Any]:
    unknown = [field for field in fields if field not in STATE_FIELDS]
    if unknown or not fields:
        raise SystemOneConfigError(
            f"state_fields must name some of {sorted(STATE_FIELDS)}, got {unknown or fields}"
        )
    if fields == ["text"]:
        return str(request["text"])
    return {field: request.get(field) for field in fields}


def build_body(
    request: Mapping[str, Any], settings: Mapping[str, Any], table: RouteTable
) -> dict[str, Any]:
    criteria = {route.label: route.description for route in table.routes}
    criteria[settings["escape_label"]] = table.escape_description or settings["escape_description"]
    questions: dict[str, Any] = {
        QUESTION_ID: {
            "type": "choice",
            "instructions": table.instructions or settings["instructions"],
            "criteria": criteria,
        }
    }
    state = build_state(request, list(settings["state_fields"]))
    if request.get("fast_path") is True:
        history = [str(line) for line in request.get("history") or []]
        questions.update(_fast_path_questions(table, bool(history)))
        if history:
            state = {"message": str(request["text"]), "recent_conversation": history}
    return {"state": state, "model": settings["model"], "questions": questions}


def _fast_path_questions(table: RouteTable, has_history: bool) -> dict[str, Any]:
    """S10. The fast path's questions, asked beside the route choice in the same call."""
    fast = table.fast
    questions: dict[str, Any] = {}
    for route in table.routes:
        if route.kind == "intent":
            text = route.question or fast.intent_question.format(description=route.description)
            question: dict[str, Any] = {"type": "noul", "instructions": text}
            if route.criteria is not None:
                question["criteria"] = route.criteria
            questions[INTENT_PREFIX + route.label] = question
    questions["action"] = {"type": "noul", "instructions": fast.action_question}
    if has_history:
        questions["follow_up"] = {"type": "noul", "instructions": fast.follow_up_question}
    questions["complexity"] = {
        "type": "score",
        "instructions": fast.complexity_question,
        "criteria": list(fast.complexity_levels),
    }
    questions["requests"] = {
        "type": "score",
        "instructions": fast.requests_question,
        "criteria": list(fast.requests_levels),
    }
    return questions


def _unit(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise _BadResponse("a noul is not a number in [0, 1]")
    return float(value)


def read_assessment(
    payload: Mapping[str, Any], table: RouteTable, has_history: bool
) -> dict[str, Any]:
    """S10. The fast path's answers as Core's `assessment`; `_BadResponse` when any is missing or
    malformed (the whole assessment is dropped, never half of it)."""
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise _BadResponse("no answers")

    def noul(question_id: str) -> float:
        answer = answers.get(question_id)
        if not isinstance(answer, Mapping):
            raise _BadResponse(f"no answer to {question_id}")
        return _unit(answer.get("noul"))

    intents: dict[str, float] = {}
    for route in table.routes:
        if route.kind == "intent":
            value = noul(INTENT_PREFIX + route.label)
            intents[route.name] = max(value, intents.get(route.name, 0.0))
    complexity = answers.get("complexity")
    score = complexity.get("score") if isinstance(complexity, Mapping) else None
    top = len(table.fast.complexity_levels) - 1
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= top:
        raise _BadResponse("the complexity is not a score on its levels")
    return {
        "intents": intents,
        "action": noul("action"),
        "follow_up": noul("follow_up") if has_history else None,
        "complexity": round(float(score) / top, 4),
        "requests": _requests(answers.get("requests"), len(table.fast.requests_levels)),
    }


def _requests(answer: object, levels: int) -> int:
    """P13. The request count: the score's expected level, rounded, plus one (level 0 is one)."""
    score = answer.get("score") if isinstance(answer, Mapping) else None
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= levels - 1
    ):
        raise _BadResponse("the requests answer is not a score on its levels")
    return int(math.floor(score + 0.5)) + 1


# --- the answer ----------------------------------------------------------------------------------


class _BadResponse(ValueError):
    pass


def _bounded(value: str) -> str:
    return value[:_MAX_TEXT]


def _usage(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        _bounded(str(key)): value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def _read(payload: object, labels: set[str]) -> tuple[str, float, str | None, dict[str, int]]:
    """choice, confidence, model and usage of a response; `_BadResponse` when it breaks the
    contract."""
    if not isinstance(payload, Mapping):
        raise _BadResponse("not an object")
    answers = payload.get("answers")
    answer = answers.get(QUESTION_ID) if isinstance(answers, Mapping) else None
    if not isinstance(answer, Mapping):
        raise _BadResponse("no answer to the question")
    choice, confidence = answer.get("choice"), answer.get("confidence")
    if not isinstance(choice, str) or choice not in labels:
        raise _BadResponse("the choice is not an option")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise _BadResponse("the confidence is not a number in [0, 1]")
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        raise _BadResponse("the model is not a string")
    return (
        choice,
        float(confidence),
        None if model is None else _bounded(model),
        _usage(payload.get("usage")),
    )


def _decide(
    choice: str,
    confidence: float,
    table: RouteTable,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    answer: dict[str, Any] = {"label": choice, "confidence": confidence}
    if choice == settings["escape_label"]:
        return {"decision": "pass", "reason": "escape", **answer}
    route = table.by_label()[choice]
    if route.kind == PASS_TARGET:
        return {"decision": "pass", "reason": "pass_route", **answer}
    if confidence < settings["threshold"]:
        return {"decision": "pass", "reason": "below_threshold", **answer}
    if route.kind == "profile":
        return {"decision": "route", "profile_id": route.name, **answer}
    return {"decision": "intent", "intent": route.name, **answer}


# --- the call ------------------------------------------------------------------------------------


def _shared_client() -> httpx.AsyncClient:
    """One pooled client per event loop: connection reuse saves the TLS handshake per turn."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient()
        _clients[loop] = client
    return client


async def _post(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    key: str,
    *,
    deadline: float,
    backoff: float,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    retried = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        response = await asyncio.wait_for(
            client.post(url, json=body, headers=headers, timeout=remaining), remaining
        )
        if response.status_code not in RETRY_STATUSES or retried:
            return response
        # One retry, only when the wait and a fair attempt still fit the budget.
        if deadline - time.monotonic() <= backoff * 2:
            return response
        retried = True
        await asyncio.sleep(backoff)


async def route(
    request: dict[str, Any],
    settings: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    table = load_routes(settings)
    body = build_body(request, settings, table)

    def done(answer: dict[str, Any]) -> dict[str, Any]:
        answer["calibrated"] = False
        answer["elapsed_ms"] = max(0, round((time.monotonic() - started) * 1000))
        return answer

    key = (os.environ if environ is None else environ).get(settings["api_key_env"], "")
    if not key.strip():
        return done({"decision": "pass", "reason": "missing_api_key"})
    url = str(settings["base_url"]).rstrip("/") + ENDPOINT
    try:
        response = await _post(
            client or _shared_client(),
            url,
            body,
            key.strip(),
            deadline=started + settings["timeout_ms"] / 1000,
            backoff=settings["retry_backoff_ms"] / 1000,
        )
    except (TimeoutError, httpx.TimeoutException):
        return done({"decision": "pass", "reason": "timeout"})
    except httpx.HTTPError as error:
        return done({"decision": "pass", "reason": f"network_error:{type(error).__name__}"})
    if response.status_code != 200:
        return done({"decision": "pass", "reason": f"http_{response.status_code}"})
    labels = {route.label for route in table.routes} | {settings["escape_label"]}
    try:
        choice, confidence, model, usage = _read(response.json(), labels)
    except ValueError:  # _BadResponse, or a body that is not JSON
        return done({"decision": "pass", "reason": "bad_response"})
    answer = _decide(choice, confidence, table, settings)
    if request.get("fast_path") is True:
        try:
            answer["assessment"] = read_assessment(
                response.json(), table, bool(request.get("history"))
            )
        except ValueError:  # _BadResponse: the route stands, the fast path does not
            answer.setdefault("reason", "bad_assessment")
    if model is not None:
        answer["model"] = model
    answer["usage"] = usage
    return done(answer)
