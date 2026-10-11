"""Pull-mode channels are configuration, and an unregistered one is refused up front.

Subject: composition.py (`Settings.pull_channels`, `build_channel_registry`) and
adapters/driving/http/routes.py (`create_app(channels=...)`).

A vertical's backend starts turns on its own channel and polls `GET /turns/{id}`. It used
to need a hard-coded Core entry; without one the turn failed in `_step_deliver`, AFTER the
model had been paid for. Now the channel is listed in `AGENT_CORE_PULL_CHANNELS`, and the
HTTP route refuses a channel the registry does not hold before the turn is started.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_core.adapters.driving.channels.registry import UnknownChannelError
from agent_core.adapters.driving.http.routes import create_app
from agent_core.composition import Settings, build_channel_registry
from agent_core.domain.turn import TurnId, TurnRequest

_NO_ENV_FILE = Path("/nonexistent/.env")


def _settings(**environ: str) -> Settings:
    return Settings.from_env(environ, env_file=_NO_ENV_FILE)


def test_pull_channels_are_read_from_the_environment() -> None:
    settings = _settings(AGENT_CORE_PULL_CHANNELS=" glazed, voice ,,glazed")

    assert settings.pull_channels == ("glazed", "voice")


def test_no_pull_channels_by_default() -> None:
    assert _settings().pull_channels == ()


def test_a_configured_pull_channel_is_registered() -> None:
    registry = build_channel_registry(_settings(AGENT_CORE_PULL_CHANNELS="glazed"))

    assert "glazed" in registry.channel_ids()
    assert {"http", "cli"} <= set(registry.channel_ids())
    registry.get("glazed")


def test_an_unconfigured_channel_is_still_unknown() -> None:
    registry = build_channel_registry(_settings())

    with pytest.raises(UnknownChannelError):
        registry.get("glazed")


def test_naming_an_always_on_channel_is_not_a_duplicate_error() -> None:
    registry = build_channel_registry(_settings(AGENT_CORE_PULL_CHANNELS="http,glazed"))

    assert registry.channel_ids().count("http") == 1


@dataclass
class _Handle:
    turn_id: TurnId


@dataclass
class _Starter:
    started: list[TurnRequest] = field(default_factory=list)

    async def __call__(self, request: TurnRequest) -> _Handle:
        self.started.append(request)
        return _Handle(turn_id=TurnId("t-1"))


_BODY = {"session_id": "s-1", "profile_id": "p", "text": "hi"}


def _post(client: TestClient, channel: str) -> int:
    headers = {"X-Subject-Id": "u", "X-Tenant-Id": "t", "X-Channel": channel}
    return client.post("/turns", json=_BODY, headers=headers).status_code


def test_a_turn_on_a_configured_pull_channel_is_accepted() -> None:
    registry = build_channel_registry(_settings(AGENT_CORE_PULL_CHANNELS="glazed"))
    starter = _Starter()
    client = TestClient(create_app(start_turn=starter, channels=registry))

    assert _post(client, "glazed") == 202
    assert len(starter.started) == 1


def test_a_turn_on_an_unregistered_channel_is_refused_before_anything_starts() -> None:
    registry = build_channel_registry(_settings())
    starter = _Starter()
    client = TestClient(create_app(start_turn=starter, channels=registry))

    assert _post(client, "glazed") == 422
    assert starter.started == []  # nothing enqueued, so no model call can follow


def test_the_default_channel_is_always_accepted() -> None:
    registry = build_channel_registry(_settings())
    starter = _Starter()
    client = TestClient(create_app(start_turn=starter, channels=registry))
    headers = {"X-Subject-Id": "u", "X-Tenant-Id": "t"}

    assert client.post("/turns", json=_BODY, headers=headers).status_code == 202
