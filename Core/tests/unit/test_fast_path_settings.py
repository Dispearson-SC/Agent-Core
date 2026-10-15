"""The fast path's deployment settings (Phase 5, S10).

Subject: composition.py (`Settings.fast_path*`, `fast_path_thresholds_for`).

THE RULES UNDER TEST. `AGENT_CORE_FAST_PATH` is off unless set to shadow or on (any case); anything
else stops startup. The thresholds have defaults and are read strictly: a value outside [0, 1], or
a `max_intents` below 1, stops startup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.composition import Settings, fast_path_thresholds_for
from agent_core.domain.fast_path import FastPathMode, FastPathThresholds

_NO_ENV_FILE = Path("/nonexistent/.env")


def _settings(**env: str) -> Settings:
    return Settings.from_env(env, env_file=_NO_ENV_FILE)


def test_the_fast_path_is_off_by_default_with_the_default_thresholds() -> None:
    settings = _settings()
    assert settings.fast_path is FastPathMode.OFF
    assert fast_path_thresholds_for(settings) == FastPathThresholds()


@pytest.mark.parametrize(("raw", "mode"), [("shadow", "shadow"), (" ON ", "on"), ("off", "off")])
def test_the_switch_reads_off_shadow_or_on(raw: str, mode: str) -> None:
    assert _settings(AGENT_CORE_FAST_PATH=raw).fast_path is FastPathMode(mode)


def test_an_unknown_mode_stops_startup() -> None:
    with pytest.raises(ValueError, match="AGENT_CORE_FAST_PATH"):
        _settings(AGENT_CORE_FAST_PATH="maybe")


def test_the_thresholds_are_read_from_the_environment() -> None:
    settings = _settings(
        AGENT_CORE_FAST_PATH_INTENT_THRESHOLD="0.6",
        AGENT_CORE_FAST_PATH_ACTION_THRESHOLD="0.4",
        AGENT_CORE_FAST_PATH_FOLLOW_UP_THRESHOLD="0.2",
        AGENT_CORE_FAST_PATH_COMPLEXITY_THRESHOLD="0.7",
        AGENT_CORE_FAST_PATH_MAX_INTENTS="4",
    )
    assert fast_path_thresholds_for(settings) == FastPathThresholds(0.6, 0.4, 0.2, 0.7, 4)


@pytest.mark.parametrize(
    "env",
    [
        {"AGENT_CORE_FAST_PATH_INTENT_THRESHOLD": "1.5"},
        {"AGENT_CORE_FAST_PATH_ACTION_THRESHOLD": "high"},
        {"AGENT_CORE_FAST_PATH_MAX_INTENTS": "0"},
    ],
)
def test_a_bad_threshold_stops_startup(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _settings(**env)


# --- the recent conversation the follow-up question reads ----------------------------------------

from agent_core.composition import recent_history_lines  # noqa: E402


def test_the_recent_conversation_is_the_last_texts_of_both_sides_oldest_first_and_short() -> None:
    rows = [  # newest first, as the query returns them
        {"kind": "response", "parts": [{"part_kind": "text", "content": "Order 12 trays."}]},
        {"kind": "request", "parts": [
            {"part_kind": "tool-return", "content": "{...}"},
        ]},
        {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "get_x"}]},
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "What do I order?"}]},
        {"kind": "response", "parts": [{"part_kind": "text", "content": "x" * 500}]},
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "Sales?"}]},
        "not a message",
    ]  # fmt: skip

    lines = recent_history_lines(rows, limit=3, max_chars=40)

    assert lines == (
        "agent: " + "x" * 30 + "...",
        "person: What do I order?",
        "agent: Order 12 trays.",
    )
    assert recent_history_lines([], limit=3) == ()
