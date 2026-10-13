"""C31: the LiteLLM adapter's model, exercised end to end against a recorded provider exchange.

Subjects: adapters/driven/llm_litellm/models.py (`model_for(..., http_client=)`),
tests/support/http_cassette.py (`CassetteTransport`), tests/fixtures/cassettes/*.json.

`test_litellm_models.py` inspects the model object; this file RUNS it. A pydantic-ai agent talks to
`model_for("minimax/MiniMax-M3", None)` through an HTTP client whose transport answers from a JSON
cassette, so the request the adapter really builds (URL, body) is compared with a recorded one and
the response it parses is a recorded one, with no network. The sockets are closed for the test: a
request the cassette does not hold fails as a cassette miss, never as a call to a provider.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from pydantic_ai import Agent

from agent_core.adapters.driven.llm_litellm import models
from tests.support.http_cassette import CassetteMiss, CassetteTransport

CASSETTES = Path(__file__).resolve().parent.parent / "fixtures" / "cassettes"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # litellm fetches its model-cost map from the internet the first time it is imported. Use the
    # copy it ships, and import it BEFORE the sockets are closed so the test itself is the first
    # thing that must not touch the network.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm  # noqa: F401

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-fake-minimax-for-tests")
    monkeypatch.delenv("AGENT_CORE_RECORD_CASSETTES", raising=False)


def _client(name: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=CassetteTransport(CASSETTES / name))


def test_the_adapter_replays_a_recorded_exchange_with_no_network() -> None:
    model = models.model_for(
        "minimax/MiniMax-M3", None, http_client=_client("minimax_chat_hello.json")
    )

    result = asyncio.run(Agent(model).run("Say hello"))

    assert result.output == "Hello from the cassette!"
    assert result.usage.total_tokens == 10


def test_a_request_the_cassette_does_not_hold_is_a_miss_not_a_network_call() -> None:
    model = models.model_for(
        "minimax/MiniMax-M3", None, http_client=_client("minimax_chat_hello.json")
    )

    with pytest.raises(Exception) as failure:  # noqa: PT011 - the SDK wraps what the transport raises
        asyncio.run(Agent(model).run("Say something else"))

    cause: BaseException | None = failure.value
    while cause is not None and not isinstance(cause, CassetteMiss):
        cause = cause.__cause__ or cause.__context__
    assert isinstance(cause, CassetteMiss), "the request ended at the cassette, not at a socket"
    assert "no recorded interaction" in str(cause)


def test_proxy_mode_keeps_the_routing_prefix_on_the_wire() -> None:
    """Day 2: the request goes to the proxy with the full litellm model id (the prefix a proxy
    routes by), which library mode strips. The cassette holds that exact body."""
    model = models.model_for(
        "minimax/MiniMax-M3",
        "http://litellm.test:4000",
        http_client=_client("litellm_proxy_hello.json"),
    )

    result = asyncio.run(Agent(model).run("Say hello"))

    assert result.output == "Hello via the proxy cassette!"
