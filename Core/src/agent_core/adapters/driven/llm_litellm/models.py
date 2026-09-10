"""Driven adapter: the `Model` object a turn is actually run against.

Phase:   F0 (library mode) / D2 (proxy mode)
Tasks:   docs/TASKS.md#t-f0-04, docs/TASKS.md#t-d2-01
Used by: adapters/driven/agent_pydantic/runner.py (`litellm_model_factory`)

THE TWO MODES, AND WHY THEY ARE TWO FUNCTIONS
    `ModelGateway.base_url()` is the whole day-1/day-2 switch (ports/model_gateway.py).
    This module is what that switch selects between.

    DAY 1 - LIBRARY MODE (`base_url()` is None). No proxy process exists. LiteLLM is in
    this process as a LIBRARY, and what we use it for is RESOLUTION: given a profile's
    `minimax/MiniMax-M3`, litellm's own registry says the provider is `minimax`, the wire
    model name is `MiniMax-M3`, the endpoint is `https://api.minimax.io/v1` (or whatever
    `MINIMAX_API_BASE` overrides it to) and the credential is `MINIMAX_API_KEY`. Those
    four answers come from litellm rather than from a constant in this file, so a provider
    it already knows about needs no code here. The request itself then goes over the
    provider's OpenAI-compatible surface.

    DAY 2 - PROXY MODE (`base_url()` is set). The proxy IS the endpoint, and it routes by
    the full litellm model id - so the `minimax/` prefix that library mode strips is
    exactly what must be kept. Virtual keys, per-tenant budgets and spend tracking live
    there. `t-d2-01` owns it.

    That inversion - strip the prefix, keep the prefix - is why these are two functions
    rather than one with a flag. A single function would have to carry the difference as a
    conditional inside itself, and the conditional is the entire behaviour.

WHY THIS EXISTS, WHICH IS NOT A STYLE PREFERENCE
    Pydantic AI's `LiteLLMProvider` is an OpenAI-compatible HTTP client and needs a base
    URL. Day 1 has no proxy to point it at, so until this module existed the default
    factory REFUSED, and the only way to reach a provider was to inject a factory - which
    only a test ever did. Production could not make a model call at all.

    Refusing was still the right instinct, because both of the things this module resolves
    fall back to OpenAI when they are absent, silently:

      - a client built with `base_url=None` targets api.openai.com, so a MiniMax profile
        would send its traffic to OpenAI; and
      - a client built with `api_key=None` picks up `OPENAI_API_KEY`, so pointing it at
        MiniMax would hand a third party an OpenAI credential and read back as a 401.

    Neither fails a test. Both are refused by name below, and `ModelEndpointUnavailableError`
    is what a case that genuinely cannot be served still gets.

THE LIBRARY-MODE GOTCHA THAT DOES *NOT* APPLY HERE, AND WHEN IT WOULD
    docs/FIELD-NOTES.md records that on litellm 1.100.1 the minimax provider raises
    `UnsupportedParamsError` for `reasoning_effort` unless it is passed through as
    `allowed_openai_params=["reasoning_effort"]`, and that the usual `drop_params=True`
    workaround discards the parameter SILENTLY - which is worse, because a comparison
    between a "thinking" run and a default one then compares two identical runs.

    That validation happens inside `litellm.completion`, and this module does not call it:
    litellm answers questions here, it does not carry the request. So the gotcha is
    inapplicable TODAY. It becomes live the moment someone routes the request itself
    through `litellm.acompletion` - read the field note before doing that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.settings import ModelSettings


class ProviderChatConfig(Protocol):
    """The two answers litellm's per-provider chat configs give us.

    A `Protocol` rather than the concrete litellm class: those classes are internal, they
    share these two methods by convention rather than through a published base, and
    binding this file to one of them would turn a litellm upgrade into a type error
    instead of a runtime refusal that names the provider.
    """

    def get_api_base(self, api_base: str | None = None) -> str | None: ...

    def get_api_key(self, api_key: str | None = None) -> str | None: ...


class ModelEndpointUnavailableError(RuntimeError):
    """No endpoint, or no credential, can be resolved for the model a profile asked for.

    Raised BEFORE a client object exists, so nothing has been pointed anywhere. The
    alternative - building a client and letting it default - is the failure this whole
    module is arranged to prevent: it does not raise, it sends a request to the wrong
    host, or the right host with somebody else's key.

    The message names the environment variable to set. It never carries a credential.
    """


def _resolved(model_id: str) -> tuple[str, str]:
    """`(wire model name, provider token)` per litellm's own registry, or refuse.

    Split out so the refusal for an unknown provider is written once. A typo in a
    profile's `model:` must die here rather than become a request to whatever answers.
    """
    import litellm

    try:
        wire_model, provider, _key, _base = litellm.get_llm_provider(model_id)
    except Exception as unresolved:
        raise ModelEndpointUnavailableError(
            f"litellm resolves no provider for model {model_id!r}. A profile's `model:` "
            "must name a provider litellm knows, as `provider/model` - see "
            "docs/FIELD-NOTES.md for the ids verified against this version. "
            "docs/TASKS.md#t-f0-04"
        ) from unresolved
    return wire_model, provider


def library_mode_model(
    model_id: str, *, settings: ModelSettings | None = None
) -> OpenAIChatModel:
    """DAY 1. The provider's own OpenAI-compatible endpoint, resolved by litellm.

    `settings` is the seat for per-model options such as `max_tokens` or
    `openai_reasoning_effort`. It is not reachable from `ModelFactory`, whose shape is
    `(model_id, base_url) -> Model`; giving a PROFILE a model-settings field is a domain
    change that nothing has needed yet, and inventing one here would put a second source
    of truth for a profile's limits outside the profile.

    Imports are local: `pydantic_ai.providers.openai` pulls in the OpenAI SDK and litellm
    is not cheap either, so a process that supplies its own factory pays for neither.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    wire_model, provider = _resolved(model_id)
    config = _chat_config(wire_model, provider)

    # litellm's own resolution, so `MINIMAX_API_BASE` and its registered default both work
    # without this file knowing either one.
    api_base = config.get_api_base()
    api_key = config.get_api_key()

    if not api_base:
        raise ModelEndpointUnavailableError(
            f"litellm resolves no endpoint for provider {provider!r} (model {model_id!r}). "
            f"Set {provider.upper()}_API_BASE, or run the day-2 proxy and set "
            "AGENT_CORE_LITELLM_BASE_URL. Refusing to build a client that would default to "
            "api.openai.com. docs/TASKS.md#t-f0-04"
        )
    if not api_key:
        # An OpenAI-compatible client built with api_key=None reads OPENAI_API_KEY. Pointed
        # at another provider that TRANSMITS an OpenAI credential to a third party and comes
        # back as a 401 that reads like a configuration mistake. Never quote the value.
        raise ModelEndpointUnavailableError(
            f"litellm resolves no credential for provider {provider!r} (model {model_id!r}). "
            f"Set {provider.upper()}_API_KEY. Refusing to build a client that would fall "
            "back to OPENAI_API_KEY and send it to another provider. docs/TASKS.md#t-f0-04"
        )

    return OpenAIChatModel(
        # The WIRE name: litellm's `provider/` prefix is routing information for a proxy,
        # and a provider sent it as a model name answers 404 much later and much less
        # clearly. Proxy mode keeps the prefix for exactly the opposite reason.
        wire_model,
        provider=OpenAIProvider(base_url=api_base, api_key=api_key),
        settings=settings,
    )


def proxy_model(
    model_id: str, base_url: str, *, settings: ModelSettings | None = None
) -> OpenAIChatModel:
    """DAY 2. Pydantic AI talking to a LiteLLM PROXY. `t-d2-01` owns this path.

    The full litellm model id travels unchanged: the `provider/` prefix is how the proxy
    decides where to route, so stripping it here - the thing library mode must do - would
    break routing for every model that is not the proxy's default.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.litellm import LiteLLMProvider

    return OpenAIChatModel(
        model_id, provider=LiteLLMProvider(api_base=base_url), settings=settings
    )


def model_for(
    model_id: str, base_url: str | None, *, settings: ModelSettings | None = None
) -> OpenAIChatModel:
    """The day-1/day-2 switch, and the only place it is read.

    `base_url` is `ModelGateway.base_url()` verbatim. That single value being the entire
    day-2 code change is the promise ports/model_gateway.py makes; this function is where
    the promise is kept.
    """
    if base_url is None:
        return library_mode_model(model_id, settings=settings)
    return proxy_model(model_id, base_url, settings=settings)


def _chat_config(wire_model: str, provider: str) -> ProviderChatConfig:
    """litellm's config object for one provider, or refuse. See `ProviderChatConfig`."""
    from litellm.types.utils import LlmProviders
    from litellm.utils import ProviderConfigManager

    try:
        config = ProviderConfigManager.get_provider_chat_config(
            model=wire_model, provider=LlmProviders(provider)
        )
    except Exception as unresolved:
        raise ModelEndpointUnavailableError(
            f"litellm has no chat configuration for provider {provider!r}. "
            "docs/TASKS.md#t-f0-04"
        ) from unresolved

    if config is None or not hasattr(config, "get_api_base") or not hasattr(config, "get_api_key"):
        raise ModelEndpointUnavailableError(
            f"litellm's configuration for provider {provider!r} does not resolve an "
            "endpoint and a credential on its own. Point the day-2 proxy at it instead, or "
            f"set {provider.upper()}_API_BASE and {provider.upper()}_API_KEY. "
            "docs/TASKS.md#t-f0-04"
        )
    return cast("ProviderChatConfig", config)
