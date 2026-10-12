"""C5: the poison-turn budget is configuration, with a documented default of 10."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.composition import Settings

_NO_FILE = Path("/nonexistent/.env")


def test_the_budget_defaults_to_ten_attempts() -> None:
    assert Settings.from_env({}, env_file=_NO_FILE).max_turn_attempts == 10


def test_the_budget_can_be_set_from_the_environment() -> None:
    settings = Settings.from_env({"AGENT_CORE_MAX_TURN_ATTEMPTS": "3"}, env_file=_NO_FILE)

    assert settings.max_turn_attempts == 3


@pytest.mark.parametrize("raw", ["0", "-5", "many", "", "2.5"])
def test_a_budget_that_cannot_be_a_positive_whole_number_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="AGENT_CORE_MAX_TURN_ATTEMPTS"):
        Settings.from_env({"AGENT_CORE_MAX_TURN_ATTEMPTS": raw}, env_file=_NO_FILE)


# --- D33 (R1-001): retiring a workflow of another application version is opt-in ----------------


def test_retiring_workflows_of_other_versions_is_off_by_default() -> None:
    assert Settings.from_env({}, env_file=_NO_FILE).retire_other_versions is False


@pytest.mark.parametrize(
    ("raw", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)]
)
def test_retiring_workflows_of_other_versions_can_be_switched_on(raw: str, expected: bool) -> None:
    settings = Settings.from_env({"AGENT_CORE_RETIRE_OTHER_VERSIONS": raw}, env_file=_NO_FILE)

    assert settings.retire_other_versions is expected


@pytest.mark.parametrize("raw", ["", "maybe", "2"])
def test_a_retire_flag_that_is_no_boolean_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="AGENT_CORE_RETIRE_OTHER_VERSIONS"):
        Settings.from_env({"AGENT_CORE_RETIRE_OTHER_VERSIONS": raw}, env_file=_NO_FILE)
