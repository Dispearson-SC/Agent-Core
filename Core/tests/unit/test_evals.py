"""C32: the eval harness (`Core/evals/`), driven by the scripted model so it costs nothing.

Subjects: evals/harness.py (`run_eval`, the report), evals/scenarios.py (the front-desk scenario),
evals/__main__.py (`main(env)`: the opt-in switches), the `make eval` target.

The harness is NOT collected by the default suite (`testpaths = ["tests"]`): its scenarios run
against a real model when asked to, take minutes and cost money. These tests prove the machinery
with `AGENT_CORE_EVAL_MODEL=scripted`: a scenario runs N times, each trial is checked, and a JSON
and a Markdown report are written. They never call a provider and never need a key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from evals.__main__ import main

CORE = Path(__file__).resolve().parent.parent.parent


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"AGENT_CORE_EVAL_REPORT_DIR": str(tmp_path), **extra}


def test_without_the_opt_in_the_eval_is_skipped_cleanly_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_env(tmp_path))

    assert code == 0
    assert "skipped" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_opted_in_but_without_the_providers_key_it_is_skipped_naming_the_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_env(tmp_path, AGENT_CORE_EVAL="1", AGENT_CORE_EVAL_MODEL="minimax/MiniMax-M3"))

    out = capsys.readouterr().out
    assert code == 0 and "skipped" in out and "MINIMAX_API_KEY" in out
    assert list(tmp_path.iterdir()) == []


def test_a_scripted_run_writes_a_json_and_a_markdown_report(tmp_path: Path) -> None:
    code = main(_env(tmp_path, AGENT_CORE_EVAL_MODEL="scripted", AGENT_CORE_EVAL_REPETITIONS="3"))

    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text())
    (scenario,) = report["scenarios"]
    assert scenario["name"] == "front_desk_asks_inventory"
    assert len(scenario["trials"]) == 3 and scenario["pass_rate"] == 1.0
    assert report["model"] == "scripted" and report["repetitions"] == 3
    markdown = (tmp_path / "report.md").read_text()
    assert "front_desk_asks_inventory" in markdown and "3/3" in markdown


def test_a_scenario_whose_checks_fail_is_reported_as_failed_not_as_a_crash(tmp_path: Path) -> None:
    import asyncio

    from evals.harness import CheckResult, Outcome, Scenario, run_eval
    from evals.sources import ScriptedSource

    async def wrong(source: object) -> Outcome:
        return Outcome((CheckResult("a", True), CheckResult("b", False, "the answer was 41")))

    async def boom(source: object) -> Outcome:
        raise RuntimeError("the provider fell over")

    scenarios = [
        Scenario("wrong_answer", "d", wrong, dict),  # type: ignore[arg-type]
        Scenario("crashes", "d", boom, dict),  # type: ignore[arg-type]
    ]

    report = asyncio.run(
        run_eval(scenarios, lambda s: ScriptedSource({}), model_label="scripted", repetitions=2)
    )

    wrong_report, crash_report = report.scenarios
    assert [t.passed for t in wrong_report.trials] == [False, False]
    assert wrong_report.trials[0].checks[1].detail == "the answer was 41"
    assert crash_report.pass_rate == 0.0
    assert crash_report.trials[0].error == "RuntimeError: the provider fell over"
    assert report.pass_rate == 0.0


def test_a_trial_that_never_finishes_is_a_failed_trial_with_a_timeout() -> None:
    import asyncio

    from evals.harness import Outcome, Scenario, run_eval
    from evals.sources import ScriptedSource

    async def hang(source: object) -> Outcome:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    report = asyncio.run(
        run_eval(
            [Scenario("hangs", "d", hang, dict)],  # type: ignore[arg-type]
            lambda s: ScriptedSource({}),
            model_label="scripted",
            repetitions=1,
            trial_timeout=0.05,
        )
    )

    (trial,) = report.scenarios[0].trials
    assert not trial.passed and trial.error is not None and "timed out" in trial.error


def test_the_pass_rate_floor_turns_a_weak_run_into_a_failing_exit_code(tmp_path: Path) -> None:
    scripted = {"AGENT_CORE_EVAL_MODEL": "scripted", "AGENT_CORE_EVAL_REPETITIONS": "1"}

    assert main(_env(tmp_path, AGENT_CORE_EVAL_MIN_PASS_RATE="1", **scripted)) == 0
    assert main(_env(tmp_path, AGENT_CORE_EVAL_MIN_PASS_RATE="1.5", **scripted)) == 1


def test_an_unknown_scenario_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown scenario"):
        main(_env(tmp_path, AGENT_CORE_EVAL_MODEL="scripted", AGENT_CORE_EVAL_SCENARIOS="nope"))


def test_the_default_suite_does_not_collect_the_evals() -> None:
    import tomllib

    config = tomllib.loads((CORE / "pyproject.toml").read_text())

    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_eval_skips_without_keys_and_runs_one_scenario_with_the_scripted_model(
    tmp_path: Path,
) -> None:
    """The acceptance test: the real target, in a subprocess, with the interpreter of this run."""
    base = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "AGENT_CORE_EVAL_REPORT_DIR": str(tmp_path),
    }

    def make(**extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["make", "-C", str(CORE.parent), "eval", f"PY={sys.executable}"],  # noqa: S607
            env={**base, **extra},
            capture_output=True,
            text=True,
            check=False,
        )

    skipped = make()
    assert skipped.returncode == 0 and "eval skipped" in skipped.stdout
    assert list(tmp_path.iterdir()) == []

    ran = make(AGENT_CORE_EVAL_MODEL="scripted", AGENT_CORE_EVAL_REPETITIONS="1")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert (tmp_path / "report.json").exists() and (tmp_path / "report.md").exists()
