"""Failing-first test for the LiteLLM `ModelGateway` adapter (day-1 library mode).

Phase:   F0 (library)
Tasks:   docs/TASKS.md#t-f0-04
Adapter: adapters/driven/llm_litellm/gateway.py

Day 1 only, per ports/model_gateway.py: `base_url()` must be `None` because LiteLLM runs
in-process with no proxy, and `model_id_for()` must round-trip a profile's model name
(day 1 mapping is near-identity; day 2 may prefix a proxy route, but that is not this
task). `classify_error` is scheduled late and is out of scope here.
"""

from __future__ import annotations

import pytest

from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway


@pytest.mark.phase("F0")
def test_base_url_is_none_in_library_mode() -> None:
    gateway = LiteLLMGateway()

    assert gateway.base_url() is None, (
        "Day 1 is library mode: LiteLLM runs in-process with no separate proxy, so "
        "base_url() must be None. A non-None default here would make day 2's migration "
        "look like it already shipped."
    )


@pytest.mark.phase("F0")
def test_model_id_for_round_trips_a_profile_model_name() -> None:
    gateway = LiteLLMGateway()

    profile_model = "openrouter/anthropic/claude-3.5-sonnet"

    assert gateway.model_id_for(profile_model) == profile_model, (
        "Day 1's mapping is near-identity (see ports/model_gateway.py): a profile's "
        "model name must come back unchanged until day 2 introduces a proxy route."
    )
