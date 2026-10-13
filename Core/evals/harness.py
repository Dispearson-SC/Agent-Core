"""Scenarios, trials and the report (C32, D67).

A scenario is an async function: given a `ModelSource` it drives a multi-agent flow through the
production runner and returns the checks its outcome passed or failed. `run_eval` runs each
scenario `repetitions` times (a real model is not deterministic, so one pass proves little), records
every trial (passed, the individual checks, how long, how many model calls, any error) and returns a
`Report`. A trial that raises, or runs past the timeout, is a FAILED trial with its error recorded:
one bad repetition never aborts the run.

The report carries what happened and nothing about how the run was configured beyond the model
label and the repetition count: no environment, no keys, no prompts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic_ai.models import Model

__all__ = [
    "CheckResult",
    "ModelSource",
    "Outcome",
    "Report",
    "Scenario",
    "ScenarioReport",
    "Trial",
    "render_json",
    "render_markdown",
    "run_eval",
    "write_report",
]

DEFAULT_TRIAL_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a scenario run produced: its checks, and how many model calls it made (None when the
    source cannot say)."""

    checks: tuple[CheckResult, ...]
    model_calls: int | None = None


class ModelSource(Protocol):
    """Where a scenario's models come from: a real provider, or scripted fakes."""

    @property
    def label(self) -> str: ...

    def profile_model(self, agent: str) -> str:
        """The `model:` a scenario's profile for `agent` carries."""
        ...

    def build(self, model_id: str, base_url: str | None) -> Model:
        """The runner's `ModelFactory`."""
        ...

    def model_calls(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    run: Callable[[ModelSource], Awaitable[Outcome]]
    # The scripted models for `AGENT_CORE_EVAL_MODEL=scripted`: agent id -> a FRESH ScriptedModel.
    scripts: Callable[[], dict[str, Any]]


SourceFactory = Callable[[Scenario], ModelSource]


@dataclass(frozen=True, slots=True)
class Trial:
    index: int
    passed: bool
    checks: tuple[CheckResult, ...]
    duration_seconds: float
    model_calls: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    name: str
    description: str
    trials: tuple[Trial, ...]

    @property
    def passed(self) -> int:
        return sum(1 for trial in self.trials if trial.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.trials) if self.trials else 0.0


@dataclass(frozen=True, slots=True)
class Report:
    model: str
    repetitions: int
    started_at: str
    scenarios: tuple[ScenarioReport, ...] = field(default_factory=tuple)

    @property
    def pass_rate(self) -> float:
        trials = [t for s in self.scenarios for t in s.trials]
        return sum(1 for t in trials if t.passed) / len(trials) if trials else 0.0


async def _trial(scenario: Scenario, source: ModelSource, index: int, timeout: float) -> Trial:
    started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(scenario.run(source), timeout)
    except TimeoutError:
        return Trial(
            index, False, (), time.monotonic() - started, error=f"timed out after {timeout}s"
        )
    except Exception as error:
        # The type and message only: a traceback can carry request bodies.
        return Trial(
            index, False, (), time.monotonic() - started, error=f"{type(error).__name__}: {error}"
        )
    return Trial(
        index,
        all(check.passed for check in outcome.checks) and bool(outcome.checks),
        outcome.checks,
        time.monotonic() - started,
        model_calls=outcome.model_calls
        if outcome.model_calls is not None
        else source.model_calls(),
    )


async def run_eval(
    scenarios: Sequence[Scenario],
    source_for: SourceFactory,
    *,
    model_label: str,
    repetitions: int,
    trial_timeout: float = DEFAULT_TRIAL_TIMEOUT_SECONDS,
) -> Report:
    """Run every scenario `repetitions` times, each trial on a fresh `ModelSource`."""
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    reports: list[ScenarioReport] = []
    for scenario in scenarios:
        trials = [
            await _trial(scenario, source_for(scenario), index, trial_timeout)
            for index in range(1, repetitions + 1)
        ]
        reports.append(ScenarioReport(scenario.name, scenario.description, tuple(trials)))
    return Report(
        model=model_label,
        repetitions=repetitions,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        scenarios=tuple(reports),
    )


def render_json(report: Report) -> dict[str, Any]:
    return {
        "model": report.model,
        "repetitions": report.repetitions,
        "started_at": report.started_at,
        "pass_rate": report.pass_rate,
        "scenarios": [
            {
                "name": s.name,
                "description": s.description,
                "pass_rate": s.pass_rate,
                "passed": s.passed,
                "trials": [asdict(t) for t in s.trials],
            }
            for s in report.scenarios
        ],
    }


def render_markdown(report: Report) -> str:
    lines = [
        "# Eval report",
        "",
        f"- model: `{report.model}`",
        f"- repetitions per scenario: {report.repetitions}",
        f"- started: {report.started_at}",
        f"- overall pass rate: {report.pass_rate:.0%}",
        "",
        "| scenario | passed | pass rate |",
        "|---|---|---|",
    ]
    for s in report.scenarios:
        lines.append(f"| {s.name} | {s.passed}/{len(s.trials)} | {s.pass_rate:.0%} |")
    for s in report.scenarios:
        lines += ["", f"## {s.name}", "", s.description, ""]
        for trial in s.trials:
            verdict = "pass" if trial.passed else "FAIL"
            calls = "?" if trial.model_calls is None else str(trial.model_calls)
            lines.append(
                f"- trial {trial.index}: **{verdict}** ({trial.duration_seconds:.1f}s, "
                f"{calls} model calls)"
            )
            if trial.error:
                lines.append(f"  - error: {trial.error}")
            lines.extend(
                f"  - {'ok' if c.passed else 'FAILED'} {c.name}"
                + (f": {c.detail}" if c.detail else "")
                for c in trial.checks
            )
    return "\n".join(lines) + "\n"


def write_report(report: Report, directory: Path) -> tuple[Path, Path]:
    """`report.json` and `report.md` in `directory` (created)."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = directory / "report.json", directory / "report.md"
    json_path.write_text(json.dumps(render_json(report), indent=2) + "\n")
    markdown_path.write_text(render_markdown(report))
    return json_path, markdown_path
