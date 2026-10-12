"""C3: the redelivery lease is configuration, with a documented default."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.composition import Settings


def _env(**extra: str) -> dict[str, str]:
    return dict(extra)


def test_the_lease_defaults_to_five_minutes() -> None:
    settings = Settings.from_env(_env(), env_file=Path("/nonexistent/.env"))

    assert settings.peer_lease_seconds == 300


def test_the_lease_can_be_set_from_the_environment() -> None:
    settings = Settings.from_env(
        _env(AGENT_CORE_PEER_LEASE_SECONDS="45"), env_file=Path("/nonexistent/.env")
    )

    assert settings.peer_lease_seconds == 45


@pytest.mark.parametrize("raw", ["0", "-5", "soon", ""])
def test_a_lease_that_cannot_be_a_positive_number_of_seconds_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="AGENT_CORE_PEER_LEASE_SECONDS"):
        Settings.from_env(
            _env(AGENT_CORE_PEER_LEASE_SECONDS=raw), env_file=Path("/nonexistent/.env")
        )


def test_the_redelivery_cap_defaults_to_three() -> None:
    settings = Settings.from_env(_env(), env_file=Path("/nonexistent/.env"))

    assert settings.peer_max_redeliveries == 3


def test_the_redelivery_cap_can_be_set_and_zero_means_never_rerun() -> None:
    settings = Settings.from_env(
        _env(AGENT_CORE_PEER_MAX_REDELIVERIES="0"), env_file=Path("/nonexistent/.env")
    )

    assert settings.peer_max_redeliveries == 0


@pytest.mark.parametrize("raw", ["-1", "many", ""])
def test_a_redelivery_cap_that_is_not_a_non_negative_whole_number_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="AGENT_CORE_PEER_MAX_REDELIVERIES"):
        Settings.from_env(
            _env(AGENT_CORE_PEER_MAX_REDELIVERIES=raw), env_file=Path("/nonexistent/.env")
        )
