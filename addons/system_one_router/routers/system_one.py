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
_ROUTE_KEYS: Final = frozenset({"label", "description", "target"})
_FILE_KEYS: Final = frozenset({"routes", "instructions", "escape_description"})
_ADDONS_ROOT: Final = Path(__file__).resolve().parents[2]


class SystemOneConfigError(ValueError):
    """The router's configuration (settings or routes file) cannot work."""


@dataclass(frozen=True, slots=True)
class Route:
    label: str
    description: str
    kind: str  # "profile", "intent" or "pass"
    name: str


@dataclass(frozen=True, slots=True)
class RouteTable:
    routes: tuple[Route, ...]
    instructions: str | None
    escape_description: str | None

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
        if target == PASS_TARGET:
            routes.append(Route(label, description, PASS_TARGET, ""))
            continue
        match = _TARGET.match(target) if isinstance(target, str) else None
        if match is None:
            raise SystemOneConfigError(
                f"{where}: target must be profile:<id>, intent:<name> or {PASS_TARGET}"
            )
        routes.append(Route(label, description, match.group(1), match.group(2)))
    instructions = document.get("instructions")
    escape = document.get("escape_description")
    return RouteTable(
        routes=tuple(routes),
        instructions=None if instructions is None else _text(instructions, "instructions"),
        escape_description=None if escape is None else _text(escape, "escape_description"),
    )


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
    return {
        "state": build_state(request, list(settings["state_fields"])),
        "model": settings["model"],
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": table.instructions or settings["instructions"],
                "criteria": criteria,
            }
        },
    }


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
    if model is not None:
        answer["model"] = model
    answer["usage"] = usage
    return done(answer)
