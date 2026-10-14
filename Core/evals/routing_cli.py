"""Entry point of the routing benchmark: `python -m evals.routing_cli`.

Runs a labelled question set (evals/routing.py) with routing OFF and ON and writes `routing.json`
and `routing.md`. Environment:

    AGENT_CORE_EVAL_ROUTING_SET=<file>     REQUIRED: the question set (.yaml/.yml/.jsonl)
    AGENT_CORE_EVAL_ROUTER=<name>          `keyword` (default): the set's own `keyword_router`
                                           rules, offline, no opt-in. Any other name is a router
                                           an enabled addon provides (`provides.routers`), loaded
                                           exactly as Core loads it (AGENT_CORE_ADDONS,
                                           AGENT_CORE_ADDONS_DIR, AGENT_CORE_ADDONS_CONFIG and
                                           AGENT_CORE_ADDON__<ADDON>__<KEY>); it may call a
                                           classifier, so it needs AGENT_CORE_EVAL=1.
    AGENT_CORE_EVAL_ROUTER_TIMEOUT_MS=<n>  the router timeout (default 1500, as production)
    AGENT_CORE_EVAL_TURNS_URL=<url>        optional: ALSO run every question as a whole turn
                                           against a running Agent-Core (POST /turns, poll
                                           GET /turns/{id} until `finished`; `waiting` or an
                                           unknown status is a failed turn, never timed) to
                                           time the whole turn. Needs
                                           AGENT_CORE_EVAL=1 (a real model answers; it costs).
    AGENT_CORE_EVAL_TURNS_TENANT / _SUBJECT / _CHANNEL
                                           the identity headers of those turns (default `eval`,
                                           `eval-bench`, `http`)
    AGENT_CORE_EVAL_TURN_TIMEOUT=<s>       per-turn timeout (default 180)
    AGENT_CORE_EVAL_REPORT_DIR=<dir>       where the report goes (default evals/reports)
    AGENT_CORE_EVAL_MIN_ACCURACY=<0-1>     exit 1 when routing-on accuracy is below it (default 0)

A whole turn runs on the profile its route names (`profile:<id>`), else on the set's entry
profile: production routing lands only on profiles a channel may start, so a target posted
directly is the turn routing would have run. An `intent:` route is posted to the entry profile so
the DEPLOYMENT's router and intent plan answer it (D97: routing and `routing.intents` on there);
after it finished, the driver reads the turn's snapshot from `GET /turns/{id}/events` and the
report counts plan / fallback / no plan. Routing on in the deployment also routes the baseline's
turns, so compare turn latency across two runs (deployment routing off, then on).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.routing.addon_router import AddonTurnRouter
from agent_core.ports.turn_router import TurnRouter
from evals.routing import (
    KeywordRouter,
    Question,
    QuestionSetError,
    load_question_set,
    run_routing,
    write_routing_report,
)

__all__ = [
    "DONE_STATUSES",
    "IN_PROGRESS_STATUSES",
    "STOPPED_STATUSES",
    "HttpTurnDriver",
    "TurnNotFinishedError",
    "UnknownTurnStatusError",
    "main",
]

KEYWORD: Final = "keyword"
DEFAULT_REPORT_DIR: Final = "evals/reports"
DEFAULT_TURN_TIMEOUT: Final = 180.0
# What `GET /turns/{id}` may say, explicitly (slice 35): a turn is timed only when done, polled
# again only while it is in progress, and a failed turn when it stopped or says anything else.
DONE_STATUSES: Final = frozenset({"finished"})
IN_PROGRESS_STATUSES: Final = frozenset({"running"})
STOPPED_STATUSES: Final = frozenset({"waiting"})

_LOG = logging.getLogger(__name__)


class TurnNotFinishedError(RuntimeError):
    """The turn stopped somewhere other than `finished`: waiting on a person, or a status this
    driver does not know. Not a turn time; the benchmark counts it as a failed turn."""


class UnknownTurnStatusError(TurnNotFinishedError):
    """The poll read a status outside `DONE_STATUSES`, `IN_PROGRESS_STATUSES` and
    `STOPPED_STATUSES` (or none): a contract this driver does not know, never read as done."""


class HttpTurnDriver:
    """A `TurnDriver` over a running Agent-Core's HTTP API: start the turn, poll until it reaches
    a terminal status. Only `finished` is a timed turn; `waiting` (on a person, who will not come
    during a benchmark) or an unknown status raises `TurnNotFinishedError`, so the run records it
    as a failed turn instead of timing half a turn."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        headers: Mapping[str, str],
        poll_seconds: float = 0.25,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT,
    ) -> None:
        self._client = client
        self._headers = dict(headers)
        self._poll = poll_seconds
        self._timeout = timeout_seconds

    async def run(self, question: Question, route: str, entry_profile: str) -> str | None:
        """Run the turn; for an intent route, return how the deployment's plan ended."""
        profile = route.removeprefix("profile:") if route.startswith("profile:") else entry_profile
        started = await self._client.post(
            "/turns",
            headers=self._headers,
            json={
                "session_id": f"eval-{question.id}-{uuid.uuid4().hex[:8]}",
                "profile_id": profile,
                "text": question.text,
            },
        )
        started.raise_for_status()
        turn_id = started.json()["turn_id"]
        deadline = time.monotonic() + self._timeout
        while True:
            polled = await self._client.get(f"/turns/{turn_id}", headers=self._headers)
            polled.raise_for_status()
            status = polled.json().get("status")
            if not isinstance(status, str):
                raise UnknownTurnStatusError(f"turn {turn_id} has an unknown status {status!r}")
            if status in DONE_STATUSES:
                return await self._plan_or_none(turn_id) if route.startswith("intent:") else None
            if status in STOPPED_STATUSES:
                raise TurnNotFinishedError(f"turn {turn_id} stopped {status!r}, not finished")
            if status not in IN_PROGRESS_STATUSES:
                raise UnknownTurnStatusError(f"turn {turn_id} has an unknown status {status!r}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"turn {turn_id} still running after {self._timeout}s")
            await asyncio.sleep(self._poll)


    async def _plan_or_none(self, turn_id: str) -> str | None:
        """`_plan_of`, or None when the feed cannot be read: the turn already finished, and how its
        plan ended is a diagnostic that must never turn a finished turn into a failed one."""
        try:
            return await self._plan_of(turn_id)
        except (httpx.HTTPError, json.JSONDecodeError):
            # Only what the feed can do wrong; an unexpected shape is already None in `_plan_of`,
            # and a bug in this driver surfaces instead of reading as an unreadable feed.
            _LOG.warning("could not read the plan of turn %s; reporting none", turn_id)
            return None

    async def _plan_of(self, turn_id: str) -> str | None:
        """The `plan` of the turn in the first snapshot of its event feed (D97), or None."""
        async with self._client.stream(
            "GET", f"/turns/{turn_id}/events", headers=self._headers
        ) as response:
            response.raise_for_status()
            event = ""
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line.removeprefix("event:").strip()
                elif line.startswith("data:") and event == "snapshot":
                    return _plan_in(json.loads(line.removeprefix("data:")), turn_id)
        return None


def _plan_in(snapshot: object, turn_id: str) -> str | None:
    """The `plan` of `turn_id` in a decoded snapshot; None for any shape that does not hold one."""
    turns = snapshot.get("turns") if isinstance(snapshot, dict) else None
    for turn in turns if isinstance(turns, list) else ():
        if isinstance(turn, dict) and turn.get("turn_id") == turn_id:
            plan = turn.get("plan")
            return plan if isinstance(plan, str) else None
    return None


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isnan(value) or not low <= value <= high:
        raise SystemExit(f"{name} must be a number between {low} and {high}, got {raw!r}")
    return value


def _addon_router(env: Mapping[str, str], name: str) -> TurnRouter:
    contributions = register_addons(
        load_addons_from_environment(env),
        builtin_toolsets={},
        builtin_fragments_dir=None,
        reserved_channels=(),
    )
    if name not in contributions.routers:
        known = ", ".join(sorted(contributions.routers)) or "none"
        raise SystemExit(f"no enabled addon provides the router {name!r} (enabled: {known})")
    return AddonTurnRouter(contributions.routers)


def main(env: Mapping[str, str]) -> int:
    set_path = env.get("AGENT_CORE_EVAL_ROUTING_SET")
    if not set_path:
        raise SystemExit("AGENT_CORE_EVAL_ROUTING_SET must name a question set file")
    timeout_ms = int(_number(env, "AGENT_CORE_EVAL_ROUTER_TIMEOUT_MS", 1500, 1, 600_000))
    turn_timeout = _number(env, "AGENT_CORE_EVAL_TURN_TIMEOUT", DEFAULT_TURN_TIMEOUT, 0.1, 3600)
    floor = _number(env, "AGENT_CORE_EVAL_MIN_ACCURACY", 0, 0, 1)
    try:
        question_set = load_question_set(Path(set_path))
    except QuestionSetError as error:
        raise SystemExit(str(error)) from None

    router_name = env.get("AGENT_CORE_EVAL_ROUTER") or KEYWORD
    turns_url = env.get("AGENT_CORE_EVAL_TURNS_URL")
    if (router_name != KEYWORD or turns_url) and env.get("AGENT_CORE_EVAL") != "1":
        print(
            "routing eval skipped: a real router or whole turns need AGENT_CORE_EVAL=1 (they "
            f"may call a classifier or a model), or AGENT_CORE_EVAL_ROUTER={KEYWORD} without "
            "AGENT_CORE_EVAL_TURNS_URL for the offline run."
        )
        return 0
    notes: list[str] = []
    router: TurnRouter
    if router_name == KEYWORD:
        if not question_set.keyword_rules:
            raise SystemExit(f"{set_path} has no keyword_router rules for the offline router")
        router = KeywordRouter(question_set.keyword_rules)
        notes.append("keyword router: the set's own rules, an offline floor, not a real router")
    else:
        router = _addon_router(env, router_name)
    if question_set.synthetic:
        notes.append("synthetic questions: replace or extend them with recorded ones")

    async def run() -> Any:
        if not turns_url:
            return await run_routing(
                question_set,
                router=router,
                router_name=router_name,
                timeout_ms=timeout_ms,
                notes=notes,
            )
        headers = {
            "X-Tenant-Id": env.get("AGENT_CORE_EVAL_TURNS_TENANT") or "eval",
            "X-Subject-Id": env.get("AGENT_CORE_EVAL_TURNS_SUBJECT") or "eval-bench",
            "X-Channel": env.get("AGENT_CORE_EVAL_TURNS_CHANNEL") or "http",
        }
        async with httpx.AsyncClient(base_url=turns_url, timeout=30.0) as client:
            driver = HttpTurnDriver(client, headers=headers, timeout_seconds=turn_timeout)
            return await run_routing(
                question_set,
                router=router,
                router_name=router_name,
                timeout_ms=timeout_ms,
                turn=driver,
                notes=notes,
            )

    comparison = asyncio.run(run())
    json_path, markdown_path = write_routing_report(
        comparison, Path(env.get("AGENT_CORE_EVAL_REPORT_DIR") or DEFAULT_REPORT_DIR)
    )
    routed = comparison.routed
    print(
        f"routing eval finished: accuracy {comparison.baseline.accuracy:.0%} off -> "
        f"{routed.accuracy:.0%} on, fallback {routed.fallback_rate:.0%}, abstention "
        f"{routed.abstention_rate:.0%}; wrote {json_path} and {markdown_path}"
    )
    return 0 if routed.accuracy >= floor else 1


if __name__ == "__main__":
    sys.exit(main(os.environ))
