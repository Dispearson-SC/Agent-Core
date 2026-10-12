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
