"""The two model endpoints LiteLLM can serve, and the one it must refuse.

Phase:   F0 (library mode) / D2 (proxy mode)
Tasks:   docs/TASKS.md#t-f0-04, docs/TASKS.md#t-d2-01
Covers:  adapters/driven/llm_litellm/models.py

WHY THIS FILE EXISTS AT ALL
    Until this landed, `ModelGateway.base_url()` returning None on day 1 meant NOTHING in
    production could build a model: the runner's default factory refused, and only a test
    injecting its own factory could reach a provider. A model endpoint that exists solely
    inside a test is not an endpoint.

    So day 1 has a real one now, and the assertions below are about the one property that
    decides whether it is safe: WHICH HOST the client is pointed at, and WHOSE credential
    it carries. Both silently default to OpenAI when they are not set explicitly, and a
    MiniMax profile reaching api.openai.com - or reaching MiniMax with an OpenAI key -
    fails in ways that cost money and leak a credential rather than raising.

NO NETWORK, NO COST
    Nothing here calls a provider. A model object is constructed and its client is
    inspected. The credentials are obviously-fake placeholders set by `monkeypatch`; the
    real key lives in a gitignored `.env`, is never read here, and is never asserted on.
"""

from __future__ import annotations

import pytest

from agent_core.adapters.driven.llm_litellm import models

pytestmark = [pytest.mark.phase("F0")]

# Obviously not a credential. Set into the environment by monkeypatch so litellm's own
# resolution has something to find, and asserted on so "the provider's key was used"
# is a fact rather than a hope.
_FAKE_MINIMAX_KEY = "sk-fake-minimax-for-tests"
_FAKE_OPENAI_KEY = "sk-fake-openai-for-tests"

_MINIMAX_MODEL = "minimax/MiniMax-M3"


@pytest.fixture(autouse=True)
def _clean_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither provider's credential leaks in from the developer's own shell.

    Without this the refusal test passes or fails depending on whose laptop it runs on,
    which is the least useful shape a security assertion can take.
    """
    for name in (
        "MINIMAX_API_KEY",
        "MINIMAX_API",
        "MINIMAX_API_BASE",
        "GEMINI_API_KEY",
        "GEMINI_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_library_mode_targets_the_providers_own_endpoint_not_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day 1: no proxy, and the client still reaches MiniMax.

    The endpoint is not hard-coded here or in the adapter - litellm's own provider config
    resolves it, so the value is the one litellm itself would have used.
    """
    monkeypatch.setenv("MINIMAX_API_KEY", _FAKE_MINIMAX_KEY)

    model = models.library_mode_model(_MINIMAX_MODEL)

    assert "minimax" in str(model.client.base_url)
    assert "openai.com" not in str(model.client.base_url)
    # The wire model name, with litellm's routing prefix stripped: a provider that is sent
    # `minimax/MiniMax-M3` as a model name answers 404, much later and much less clearly.
    assert model.model_name == "MiniMax-M3"


def test_library_mode_carries_the_providers_own_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", _FAKE_MINIMAX_KEY)

    model = models.library_mode_model(_MINIMAX_MODEL)

    assert model.client.api_key == _FAKE_MINIMAX_KEY


@pytest.mark.silent
def test_a_provider_with_no_credential_is_refused_rather_than_given_the_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SILENT-BUG AREA: the mirror image of the bug this whole change is about.

    An OpenAI-compatible client built with `api_key=None` falls back to `OPENAI_API_KEY`.
    Pointed at MiniMax that sends an OpenAI credential to a third party over TLS and gets
    back a 401 - a leak that reads as a configuration error. Nothing fails a test when it
    happens, so the refusal has to be asserted.
    """
    monkeypatch.setenv("OPENAI_API_KEY", _FAKE_OPENAI_KEY)

    with pytest.raises(models.ModelEndpointUnavailableError) as raised:
        models.library_mode_model(_MINIMAX_MODEL)

    message = str(raised.value)
    assert "MINIMAX_API_KEY" in message, "the refusal must name the variable to set"
    assert _FAKE_OPENAI_KEY not in message, "a refusal must never quote a credential"


def test_a_model_no_provider_claims_is_refused() -> None:
    """A typo in a profile's `model:` must not become a request to whatever answers."""
    with pytest.raises(models.ModelEndpointUnavailableError):
        models.library_mode_model("not-a-provider/not-a-model")


def test_proxy_mode_targets_the_proxy_and_keeps_the_routing_prefix() -> None:
    """Day 2: the proxy is the endpoint and it routes by the full litellm model id.

    Stripping the prefix here would be actively wrong - the prefix is how the proxy knows
    which provider to route to. This is the opposite of what library mode needs, which is
    why the two modes are two functions and not one with a flag.
    """
    model = models.proxy_model(_MINIMAX_MODEL, "http://litellm.internal:4000")

    assert "litellm.internal:4000" in str(model.client.base_url)
    assert model.model_name == _MINIMAX_MODEL


def test_the_dispatcher_picks_the_mode_from_the_gateways_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ModelGateway.base_url()` is the whole day-1/day-2 switch - ports/model_gateway.py."""
    monkeypatch.setenv("MINIMAX_API_KEY", _FAKE_MINIMAX_KEY)

    day_one = models.model_for(_MINIMAX_MODEL, None)
    day_two = models.model_for(_MINIMAX_MODEL, "http://litellm.internal:4000")

    assert "minimax" in str(day_one.client.base_url)
    assert "litellm.internal:4000" in str(day_two.client.base_url)


_GEMINI_MODEL = "gemini/gemini-3-flash-preview"
_FAKE_GEMINI_KEY = "sk-fake-gemini-for-tests"


def test_gemini_resolves_in_library_mode_through_its_openai_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """litellm's Gemini config has no `get_api_base`; Google's OpenAI-compatible route
    is used instead, with the wire model name and the `GEMINI_API_KEY` credential."""
    monkeypatch.setenv("GEMINI_API_KEY", _FAKE_GEMINI_KEY)

    model = models.library_mode_model(_GEMINI_MODEL)

    assert "generativelanguage.googleapis.com" in str(model.client.base_url)
    assert model.client.api_key == _FAKE_GEMINI_KEY
    assert model.model_name == "gemini-3-flash-preview"


def test_gemini_base_can_be_overridden_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", _FAKE_GEMINI_KEY)
    monkeypatch.setenv("GEMINI_API_BASE", "http://gemini.internal:9000/v1")

    model = models.library_mode_model(_GEMINI_MODEL)

    assert "gemini.internal:9000" in str(model.client.base_url)


@pytest.mark.silent
def test_gemini_without_a_credential_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _FAKE_OPENAI_KEY)

    with pytest.raises(models.ModelEndpointUnavailableError) as raised:
        models.library_mode_model(_GEMINI_MODEL)

    assert "GEMINI_API_KEY" in str(raised.value)
    assert _FAKE_OPENAI_KEY not in str(raised.value)


def test_minimax_accepts_the_repository_credential_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MINIMAX_API` (the repository's name) works even when it was never mapped to
    `MINIMAX_API_KEY`; the documented `MINIMAX_API_KEY` wins when both are set."""
    monkeypatch.setenv("MINIMAX_API", _FAKE_MINIMAX_KEY)

    assert models.library_mode_model(_MINIMAX_MODEL).client.api_key == _FAKE_MINIMAX_KEY

    monkeypatch.setenv("MINIMAX_API_KEY", "sk-fake-documented-name")
    assert models.library_mode_model(_MINIMAX_MODEL).client.api_key == "sk-fake-documented-name"


def test_credential_aliases_are_reported_by_name_only() -> None:
    assert models.credential_present("MINIMAX_API_KEY", {"MINIMAX_API": "x"})
    assert models.credential_present("MINIMAX_API_KEY", {"MINIMAX_API_KEY": "x"})
    assert not models.credential_present("MINIMAX_API_KEY", {})
    assert not models.credential_present("GEMINI_API_KEY", {"MINIMAX_API": "x"})
