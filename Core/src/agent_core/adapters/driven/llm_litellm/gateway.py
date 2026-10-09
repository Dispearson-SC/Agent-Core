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
    what `classify_error` is for, and why it was scheduled late rather than guessed.

ERROR CLASSIFICATION - t-later-03, written against BOTH live providers
    Every shape marked CAPTURED below was read off a real response on 2026-09-10, from the
    two providers docs/STATE.md lists as verified: `gemini/gemini-3-flash-preview` and
    `minimax/MiniMax-M3`. What they showed is why the port stopped leading with the status:

        HTTP 400  gemini   INVALID_ARGUMENT / reason=API_KEY_INVALID     credential
        HTTP 400  minimax  bad_request_error "unknown model ... (2013)"  route
        HTTP 401  minimax  authorized_error "login fail ... (1004)"      credential
        HTTP 404  gemini   NOT_FOUND "no longer available to new users"  route

    Two providers, four shapes, and the status agrees with the scope in exactly none of
    the crossings. Reading the status first is a coin flip between rotating a healthy key
    and failing over on a problem the fallback shares.

    Hermes' agent/error_classifier.py reaches the same conclusion from forty providers
    rather than two - it is a priority-ordered pipeline over an error's identity, with the
    status as one stage among seven, and its `_status_429` is where the upstream-wrapper
    rule here comes from. It is a field reference, not a dependency; nothing is copied.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from agent_core.ports.model_gateway import ModelAttempt, RecoveryStrategy

# --- Evidence vocabularies ----------------------------------------------------------
#
# Each tuple answers ONE question: whose failure is this? A token is a machine-readable
# identity the provider chose; a phrase is free text. Tokens are preferred because they do
# not move when a provider rewrites its prose, but both providers put load-bearing
# information in the message and nowhere else, so phrases cannot be dropped.

# The credential we sent is the problem. Another model on the same key fails too.
_CREDENTIAL_TOKENS = frozenset(
    {
        "api_key_invalid",  # CAPTURED - gemini, details[].reason
        "api_key_service_blocked",
        "authorized_error",  # CAPTURED - minimax, error.type
        "unauthenticated",
        "permission_denied",
        "insufficient_quota",
        "billing_not_active",
        "budget_exceeded",  # LiteLLM proxy, virtual-key budget (docs/DECISIONS.md#d5)
        "account_deactivated",
    }
)
_CREDENTIAL_PHRASES = (
    "api key not valid",  # CAPTURED - gemini
    "login fail",  # CAPTURED - minimax
    "invalid api key",
    "incorrect api key",
    "invalid authentication",
    "insufficient balance",
    "insufficient credits",
    "exceeded your current quota",
    "budget has been exceeded",
    "payment required",
    "billing hard limit",
)

# The route we asked for is the problem. Another key on the same route fails too.
_ROUTE_PHRASES = (
    "unknown model",  # CAPTURED - minimax
    "no longer available",  # CAPTURED - gemini
    "is not found for api version",  # CAPTURED - gemini
    "is not supported for",  # CAPTURED - gemini
    "model_not_found",
    "invalid model",
    "does not exist",
    "decommissioned",
)
# An aggregator or proxy reporting somebody else's failure. Our key authenticated fine;
# the hop behind it did not. Hermes' _is_openrouter_upstream_error is the same check.
_UPSTREAM_PHRASES = ("provider returned error", "no endpoints found", "upstream error")
_UPSTREAM_KEYS = ("provider_name", "upstream_provider")

# The REQUEST is the problem. Failing over just overflows the next provider too.
_OVERFLOW_TOKENS = frozenset(
    {"context_length_exceeded", "request_too_large", "string_above_max_length"}
)
_OVERFLOW_PHRASES = (
    "maximum context length",
    "context length exceeded",
    "reduce the length",
    "too many tokens",
    "request entity too large",
    "payload too large",
    "exceeds the maximum size",
)

# Retrying this request unchanged cannot work, whoever sends it.
_CONTENT_POLICY_TOKENS = frozenset({"content_filter", "prohibited_content", "blocklist"})
_CONTENT_POLICY_PHRASES = ("content policy", "content filter", "safety filter", "content risk")

# Nobody's fault, and it may well work in a second.
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 529})
_TRANSIENT_TOKENS = frozenset({"unavailable", "internal", "deadline_exceeded", "aborted"})
_TRANSIENT_PHRASES = (
    "overloaded",
    "try again later",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "connection error",
    "connection reset",
)

_JSON_START = re.compile(r"[{\[]")


@dataclass(frozen=True, slots=True)
class _Evidence:
    """One provider failure, flattened into the three things a verdict may consult.

    `status` is last on purpose and is the only one that can be absent without the
    classification collapsing - see the module docstring for why it is the weakest signal.
    """

    status: int | None
    identity: frozenset[str]
    message: str
    body: Mapping[str, Any]


def _first_json_object(text: str) -> Mapping[str, Any] | None:
    """Pull a provider body out of a stringified exception message.

    The common production shape is `litellm.APIError: MinimaxException - {...}`: LiteLLM
    re-raises with the upstream body pasted into the message and nothing on the exception
    itself. A classifier that only reads `.body` and `.status_code` sees an empty error,
    aborts every provider failure, and passes every unit test written with a tidy double.
    """
    decoder = json.JSONDecoder()
    for match in _JSON_START.finditer(text):
        try:
            parsed, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _body_of(error: Exception) -> Mapping[str, Any]:
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        return body
    response = getattr(error, "response", None)
    response_body = getattr(response, "body", None)
    if isinstance(response_body, Mapping):
        return response_body
    return _first_json_object(str(error)) or {}


def _error_object(body: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = body.get("error")
    return inner if isinstance(inner, Mapping) else {}


def _as_status(value: Any) -> int | None:
    """A status may arrive as an int, as `"401"`, or as something that is not one at all."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _status_of(error: Exception, body: Mapping[str, Any]) -> int | None:
    """Status from the exception, then from the body. Both providers put one in the body.

    Gemini carries it as `error.code`; MiniMax as `error.http_code`, a STRING. Neither is
    present on a LiteLLM-wrapped exception, which is why the body is consulted at all.
    """
    response = getattr(error, "response", None)
    candidates = (
        getattr(error, "status_code", None),
        getattr(response, "status_code", None),
        _error_object(body).get("code"),
        _error_object(body).get("http_code"),
        body.get("status_code"),
    )
    return next((status for status in map(_as_status, candidates) if status is not None), None)


def _detail_entries(body: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Google's `error.details[]`, where the machine-readable reason actually lives.

    The top-level `status` on a Gemini error is the gRPC bucket - INVALID_ARGUMENT covers
    a bad key AND a malformed request - so the ErrorInfo `reason` inside details is what
    separates them. CAPTURED: a bad key is INVALID_ARGUMENT with reason API_KEY_INVALID.
    """
    details = _error_object(body).get("details")
    if isinstance(details, list):
        for entry in details:
            if isinstance(entry, Mapping):
                yield entry


def _identity_of(body: Mapping[str, Any]) -> frozenset[str]:
    """Every machine-readable name the provider gave this failure, lowercased."""
    error = _error_object(body)
    tokens = {
        error.get("status"),
        error.get("type"),
        error.get("code"),
        error.get("reason"),
        body.get("type"),
        body.get("code"),
    }
    for entry in _detail_entries(body):
        tokens.add(entry.get("reason"))
        at_type = entry.get("@type")
        if isinstance(at_type, str):
            tokens.add(at_type.rsplit(".", 1)[-1])
    return frozenset(
        token.strip().lower()
        for token in tokens
        if isinstance(token, str) and token.strip()
    )


def _message_of(error: Exception, body: Mapping[str, Any]) -> str:
    """The body's own message plus the exception text, lowercased into one haystack."""
    parts = [str(error)]
    message = _error_object(body).get("message")
    if isinstance(message, str):
        parts.append(message)
    for entry in _detail_entries(body):
        detail_message = entry.get("message")
        if isinstance(detail_message, str):
            parts.append(detail_message)
    return " ".join(parts).lower()


def _evidence_of(error: Exception) -> _Evidence:
    body = _body_of(error)
    return _Evidence(
        status=_status_of(error, body),
        identity=_identity_of(body),
        message=_message_of(error, body),
        body=body,
    )


def _any_phrase(message: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in message for phrase in phrases)


def _quota_is_scoped_to_our_project(evidence: _Evidence) -> bool:
    """A Google `QuotaFailure` detail means OUR project ran out, not the model.

    This is the credential half of the port's "our 429 versus an upstream 429". A quota
    attached to the project follows the key to every other model, so failing over spends a
    second call to learn what this one already said.
    """
    return "quotafailure" in evidence.identity


def _names_the_credential(evidence: _Evidence) -> bool:
    return bool(
        evidence.identity & _CREDENTIAL_TOKENS
        or _any_phrase(evidence.message, _CREDENTIAL_PHRASES)
        or _quota_is_scoped_to_our_project(evidence)
    )


def _names_an_upstream(evidence: _Evidence) -> bool:
    metadata = _error_object(evidence.body).get("metadata")
    wrapped = isinstance(metadata, Mapping) and any(key in metadata for key in _UPSTREAM_KEYS)
    return wrapped or _any_phrase(evidence.message, _UPSTREAM_PHRASES)


def _names_the_attempted_route(evidence: _Evidence, attempt: ModelAttempt) -> bool:
    """A route verdict needs the body to blame THE MODEL WE ASKED FOR, by name.

    Both halves are required. A "not found" that names no model is a wrong URL or a proxy
    glitch (CAPTURED: Gemini's bare "Requested entity was not found"), and failing over on
    it reports a healthy route as broken while repeating the same mistake on the next
    provider. The model name alone is not enough either - an overflow message quotes it
    too. This is Hermes' `_status_404` rule, which keeps a generic 404 unclassified for
    exactly the same reason.
    """
    alias = attempt.model.rpartition("/")[2].strip().lower()
    named = bool(alias) and alias in evidence.message
    return named and (
        _any_phrase(evidence.message, _ROUTE_PHRASES) or "not found" in evidence.message
    )


def _is_transient(evidence: _Evidence, error: Exception) -> bool:
    """Nobody's credential and nobody's route - the provider is just having a moment.

    A bare 429 lands here deliberately: a rate limit that names neither our account nor an
    upstream is a backoff, NOT a rotation. Guessing ROTATE_KEY on it spends a credential
    every time a provider is merely busy.
    """
    transport = type(error).__name__.lower()
    return bool(
        evidence.status in _TRANSIENT_STATUSES
        or evidence.status == 429
        or evidence.identity & _TRANSIENT_TOKENS
        or _any_phrase(evidence.message, _TRANSIENT_PHRASES)
        or "timeout" in transport
        or "connection" in transport
    )


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

    def classify_error(self, error: Exception, attempt: ModelAttempt) -> RecoveryStrategy:
        """Priority-ordered, first match wins, and the status is consulted LAST.

        THE EVIDENCE EACH BRANCH REQUIRES - the expensive part, because a misclassification
        costs money and shows up in no test:

        1. COMPRESS needs the REQUEST named: a context or payload limit. First, because an
           overflow message quotes the model name and the limit, and would otherwise read
           as a route problem. Never fail over on one - the next provider overflows too.
        2. ROTATE_KEY needs THE CREDENTIAL WE SENT named: the key, the account, the
           balance, the virtual-key budget, or a quota scoped to our project. CAPTURED
           evidence: `reason=API_KEY_INVALID` (gemini) and `type=authorized_error`
           (minimax). The test for this branch is "another model on the same key would
           fail too".
           When `attempt.credential_refreshed` is already set, the same evidence means the
           opposite: the rotation was tried and did not help, so ABORT rather than loop.
           Nothing in any provider's body can tell us this - it is why the port grew
           `ModelAttempt` (docs/TASKS.md#t-later-03).
        3. FALLBACK_MODEL needs THE ROUTE named and the credential accepted: the body
           blames `attempt.model` by name, or it wraps an upstream provider we never
           authenticated against. The test is "another key on the same route would fail
           too".
        4. ABORT for a content-policy rejection: the request cannot succeed unchanged, and
           the second provider's filter is not obviously looser.
        5. RETRY for transient failures owned by nobody - 5xx, UNAVAILABLE, overloaded,
           timeouts, and a rate limit that names neither our account nor an upstream.
        6. ABORT for everything else.

        NEITHER SCOPE IS A FALLTHROUGH. If no evidence names the credential and none names
        the route, neither strategy is earned and step 6 takes it. Rotating a healthy key
        spends a credential and fixes nothing; failing over on a shared problem spends a
        second call. The default is loud, per the port: unrecognised is ABORT, not RETRY,
        because an unknown error retried in a loop is how one bad request becomes a bill.
        """
        evidence = _evidence_of(error)

        if evidence.identity & _OVERFLOW_TOKENS or _any_phrase(evidence.message, _OVERFLOW_PHRASES):
            return RecoveryStrategy.COMPRESS
        if evidence.status == 413:
            return RecoveryStrategy.COMPRESS

        if _names_the_credential(evidence):
            if attempt.credential_refreshed:
                return RecoveryStrategy.ABORT
            return RecoveryStrategy.ROTATE_KEY

        if _names_an_upstream(evidence) or _names_the_attempted_route(evidence, attempt):
            return RecoveryStrategy.FALLBACK_MODEL

        if evidence.identity & _CONTENT_POLICY_TOKENS or _any_phrase(
            evidence.message, _CONTENT_POLICY_PHRASES
        ):
            return RecoveryStrategy.ABORT

        if _is_transient(evidence, error):
            return RecoveryStrategy.RETRY

        return RecoveryStrategy.ABORT

