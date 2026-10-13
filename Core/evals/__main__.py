"""Entry point of the eval harness: `python -m evals` (or `make eval`). Opt-in (C32, D67).

Environment (all optional; nothing runs unless asked to):

    AGENT_CORE_EVAL=1                    run against a REAL model (costs money). Without it, and
                                         without AGENT_CORE_EVAL_MODEL=scripted, the run is skipped.
    AGENT_CORE_EVAL_MODEL=<id>           the model: a litellm id (default `minimax/MiniMax-M3`) or
                                         `scripted` for the offline fake (needs no opt-in, no key)
    AGENT_CORE_EVAL_REPETITIONS=<n>      repetitions per scenario (default 3; a positive integer)
    AGENT_CORE_EVAL_SCENARIOS=a,b        only these scenario names
    AGENT_CORE_EVAL_REPORT_DIR=<dir>     where report.json and report.md go (default evals/reports)
    AGENT_CORE_EVAL_TRIAL_TIMEOUT=<s>    per-trial timeout (default 180)
    AGENT_CORE_EVAL_MIN_PASS_RATE=<0-1>  exit 1 when the overall pass rate is below this (default 0)

A real run needs the provider's key in the environment (`MINIMAX_API_KEY` for the default model).
A missing key is a clean skip naming the variable, never a failure and never a request.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_core.adapters.driven.llm_litellm.models import credential_present
from evals.harness import Scenario, run_eval, write_report
from evals.scenarios import SCENARIOS
from evals.sources import RealSource, ScriptedSource

DEFAULT_MODEL = "minimax/MiniMax-M3"
DEFAULT_REPETITIONS = 3
DEFAULT_TRIAL_TIMEOUT = 180.0
DEFAULT_REPORT_DIR = "evals/reports"
SCRIPTED = "scripted"


def _number(
    env: Mapping[str, str], name: str, default: float, *, what: str, ok: Callable[[float], bool]
) -> float:
    """Parse one numeric setting strictly: unset is the default, anything else must parse and pass
    `ok`, or the run stops naming the variable (a typo never falls back to a default silently)."""
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isnan(value) or not ok(value):
        raise SystemExit(f"{name} must be {what}, got {raw!r}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise SystemExit(f"{name} must be a positive whole number, got {raw!r}")
    return value


def main(env: Mapping[str, str]) -> int:
    # Every numeric setting is parsed first, so a typo is reported even when the run is skipped.
    repetitions = _positive_int(env, "AGENT_CORE_EVAL_REPETITIONS", DEFAULT_REPETITIONS)
    trial_timeout = _number(
        env,
        "AGENT_CORE_EVAL_TRIAL_TIMEOUT",
        DEFAULT_TRIAL_TIMEOUT,
        what="a positive number of seconds",
        ok=lambda v: 0 < v < math.inf,
    )
    floor = _number(
        env,
        "AGENT_CORE_EVAL_MIN_PASS_RATE",
        0,
        what="a number between 0 and 1",
        ok=lambda v: 0 <= v <= 1,
    )
    model = env.get("AGENT_CORE_EVAL_MODEL") or DEFAULT_MODEL
    scripted = model == SCRIPTED

    if not scripted:
        if env.get("AGENT_CORE_EVAL") != "1":
            print(
                "eval skipped: set AGENT_CORE_EVAL=1 to run against a real model (it costs money), "
                f"or AGENT_CORE_EVAL_MODEL={SCRIPTED} for the offline run."
            )
            return 0
        provider = model.partition("/")[0]
        variable = f"{provider.upper()}_API_KEY"
        if not credential_present(variable, env):
            print(f"eval skipped: no credential for {model!r}; set {variable}.")
            return 0

    wanted = {name for name in (env.get("AGENT_CORE_EVAL_SCENARIOS") or "").split(",") if name}
    scenarios = [s for s in SCENARIOS if not wanted or s.name in wanted]
    unknown = wanted - {s.name for s in SCENARIOS}
    if unknown:
        raise SystemExit(
            f"unknown scenario(s) {sorted(unknown)}; known: {[s.name for s in SCENARIOS]}"
        )

    def source_for(scenario: Scenario) -> RealSource | ScriptedSource:
        return ScriptedSource(scenario.scripts()) if scripted else RealSource(model)

    report = asyncio.run(
        run_eval(
            scenarios,
            source_for,
            model_label=model,
            repetitions=repetitions,
            trial_timeout=trial_timeout,
        )
    )
    json_path, markdown_path = write_report(
        report, Path(env.get("AGENT_CORE_EVAL_REPORT_DIR") or DEFAULT_REPORT_DIR)
    )
    print(f"eval finished: pass rate {report.pass_rate:.0%}; wrote {json_path} and {markdown_path}")
    return 0 if report.pass_rate >= floor else 1


if __name__ == "__main__":
    sys.exit(main(os.environ))
