"""Where a scenario's models come from (C32, D67)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic_ai.models import Model

from agent_core.adapters.driven.agent_pydantic.runner import litellm_model_factory

__all__ = ["RealSource", "ScriptedSource"]

SCRIPTED_PREFIX = "scripted/"


class RealSource:
    """One real provider model for every agent of a scenario (litellm resolves the endpoint and the
    credential, exactly as production does)."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id

    @property
    def label(self) -> str:
        return self._model_id

    def profile_model(self, agent: str) -> str:
        return self._model_id

    def build(self, model_id: str, base_url: str | None) -> Model:
        return litellm_model_factory(model_id, base_url)

    def model_calls(self) -> int | None:
        return None


class ScriptedSource:
    """A scripted fake per agent (`tests/fakes/scripted_model.py`): no network, no key, the same
    answer every repetition. `scripts` maps agent id to a ScriptedModel."""

    def __init__(self, scripts: Mapping[str, Any]) -> None:
        self._scripts = dict(scripts)

    @property
    def label(self) -> str:
        return "scripted"

    def profile_model(self, agent: str) -> str:
        return f"{SCRIPTED_PREFIX}{agent}"

    def build(self, model_id: str, base_url: str | None) -> Model:
        agent = model_id.removeprefix(SCRIPTED_PREFIX)
        model: Model = self._scripts[agent].model
        return model

    def model_calls(self) -> int | None:
        return sum(int(model.call_count) for model in self._scripts.values())
