"""Driven adapter: ModelGateway over LiteLLM.

Phase:   F0 (library) / D2 (proxy)
Tasks:   docs/TASKS.md#t-f0-04
Implements: ports/model_gateway.py

DAY 1 - LIBRARY MODE
    `base_url()` returns None. LiteLLM is imported in-process. No extra process, no extra
    database. Verified: LiteLLM's DB is optional; without it you still get the
    OpenAI-compatible surface, routing and failover.

DAY 2 - PROXY MODE
    `base_url()` returns the proxy URL. THAT IS THE ENTIRE CODE CHANGE. Virtual keys,
    per-tenant budgets and spend tracking then live in the proxy, not in our code.

    If day 2 turns out to touch anything else, ports/model_gateway.py was leaking and the
    fix belongs there, not here.

PROVIDER COVERAGE (verified): OpenRouter, Moonshot AI (Kimi), Z.AI (Zhipu/GLM), MiniMax,
DeepSeek, xAI, Groq, Together, Fireworks, Nebius, Ollama, vLLM - 100+, plus a JSON form
for any OpenAI-compatible endpoint.

WHAT "SUPPORTED" DOES NOT COVER
    Request format is translated; FAILURE MODES ARE NOT. An aggregator's upstream 429 and
    your own key's 429 arrive identical, and the right responses are opposite. That is
    what `classify_error` is for, and why it is scheduled late rather than guessed now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.ports.model_gateway import RecoveryStrategy


class LiteLLMGateway:
    """`ModelGateway` over LiteLLM. Day 1 (library mode) only - see the module docstring.

    `proxy_base_url` stays `None` on day 1: LiteLLM is imported in-process, no separate
    proxy runs. Day 2 flips it to the proxy URL - per ports/model_gateway.py, that single
    return value is meant to be the entire code change.
    """

    def __init__(self, proxy_base_url: str | None = None) -> None:
        self._proxy_base_url = proxy_base_url

    def model_id_for(self, profile_model: str) -> str:
        # Day 1: near-identity, per ports/model_gateway.py.
        if self._proxy_base_url is None:
            return profile_model

        # Day 2: strip the provider segment. Verified against the live proxy
        # (docs/TASKS.md#t-d2-01): `GET /team/info` lists a team's `models` as the bare
        # alias - e.g. "MiniMax-M3" - never the provider-qualified "minimax/MiniMax-M3"
        # a profile's `model:` carries. A profile does not change between day 1 and day 2
        # (docs/ROADMAP.md's D2 table), so the reshaping happens here, once, rather than
        # asking every profile to carry two names. Callers never see the difference
        # because they only ever ask the port.
        _, _, alias = profile_model.partition("/")
        return alias or profile_model

    def base_url(self) -> str | None:
        return self._proxy_base_url

    def classify_error(self, error: Exception) -> RecoveryStrategy:
        # Scheduled late (docs/TASKS.md#t-f0-04): needs a second live provider before the
        # five strategies in ports/model_gateway.py can be assigned with confidence.
        raise NotImplementedError

