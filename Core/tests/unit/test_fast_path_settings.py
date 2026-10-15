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


# --- the composer --------------------------------------------------------------------------------

from agent_core.composition import refuse_unknown_composer  # noqa: E402
from agent_core.domain.profile import AgentProfile  # noqa: E402


def test_the_composer_is_the_entry_profile_unless_one_is_named() -> None:
    assert _settings().fast_path_composer is None
    assert _settings(AGENT_CORE_FAST_PATH_COMPOSER=" writer ").fast_path_composer == "writer"
    assert _settings(AGENT_CORE_FAST_PATH_COMPOSER="").fast_path_composer is None


def test_a_composer_that_is_not_a_loaded_profile_stops_startup() -> None:
    profiles = {"writer": AgentProfile(id="writer", persona="w", model="m")}
    refuse_unknown_composer(profiles, "writer")
    refuse_unknown_composer(profiles, None)
    with pytest.raises(ValueError, match="AGENT_CORE_FAST_PATH_COMPOSER.*ghost"):
        refuse_unknown_composer(profiles, "ghost")


# --- the answer cache ----------------------------------------------------------------------------


def test_the_composite_answer_cache_is_off_unless_a_ttl_is_set() -> None:
    assert _settings().fast_path_cache_seconds == 0
    assert _settings(AGENT_CORE_FAST_PATH_CACHE_SECONDS="300").fast_path_cache_seconds == 300


@pytest.mark.parametrize("raw", ["-1", "86401", "soon"])
def test_a_bad_cache_ttl_stops_startup(raw: str) -> None:
    with pytest.raises(ValueError, match="AGENT_CORE_FAST_PATH_CACHE_SECONDS"):
        _settings(AGENT_CORE_FAST_PATH_CACHE_SECONDS=raw)


def test_the_route_choice_threshold_is_read_from_the_environment_and_checked() -> None:
    settings = _settings(AGENT_CORE_FAST_PATH_ROUTE_THRESHOLD="0.85")
    assert fast_path_thresholds_for(settings).route_intent == 0.85
    assert fast_path_thresholds_for(_settings()).route_intent == 0.7
    with pytest.raises(ValueError, match="AGENT_CORE_FAST_PATH_ROUTE_THRESHOLD"):
        _settings(AGENT_CORE_FAST_PATH_ROUTE_THRESHOLD="1.5")


def test_the_p14_coverage_thresholds_are_read_from_the_environment_and_checked() -> None:
    thresholds = fast_path_thresholds_for(
        _settings(
            AGENT_CORE_FAST_PATH_UNCOVERED_THRESHOLD="0.75",
            AGENT_CORE_FAST_PATH_ROUTE_COVERS_THRESHOLD="0.9",
        )
    )
    assert (thresholds.uncovered, thresholds.route_covers) == (0.75, 0.9)
    defaults = fast_path_thresholds_for(_settings())
    assert (defaults.uncovered, defaults.route_covers) == (0.6, 0.95)
    for name in (
        "AGENT_CORE_FAST_PATH_UNCOVERED_THRESHOLD",
        "AGENT_CORE_FAST_PATH_ROUTE_COVERS_THRESHOLD",
    ):
        with pytest.raises(ValueError, match=name):
            _settings(**{name: "2"})
