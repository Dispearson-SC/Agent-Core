"""`classify_error` - the five strategies, decided by evidence rather than by status code.

Phase:   Later
Tasks:   docs/TASKS.md#t-later-03
Port:    ports/model_gateway.py
Adapter: adapters/driven/llm_litellm/gateway.py

WHY THIS ANCHOR WAS GATED ON A SECOND LIVE PROVIDER
    `ports/model_gateway.py` warns that "supported" means the REQUEST FORMAT is translated
    and NOT that failure modes are normalised. With one provider that warning is a
    prediction; with two it is measurable, and the measurement is worse than the warning
    suggested. Probed live on 2026-09-10 against both providers `docs/STATE.md` lists:

        HTTP 400  gemini/gemini-3-flash-preview   "API key not valid."        CREDENTIAL
        HTTP 400  minimax/MiniMax-M3              "unknown model '...'"       ROUTE
        HTTP 401  minimax/MiniMax-M3              "login fail ... (1004)"     CREDENTIAL
        HTTP 404  gemini/gemini-2.5-flash         "no longer available"       ROUTE

    The same HTTP 400 is a dead credential on one provider and a dead route on the other,
    and the two correct responses are opposite: rotate the key, or fall back to another
    model. The port's own pseudo-code - `401 / 402 -> ROTATE_KEY`, `404 model ->
    FALLBACK_MODEL` - was written against one provider and misclassifies BOTH of the 400s
    above, because 400 is not in its table at all. That is precisely the overfitting the
    gate existed to prevent.

    So: the status code is the LAST evidence consulted, never the first.

THE RULE THE WHOLE FILE IS ABOUT
    ROTATE_KEY and FALLBACK_MODEL are never a fallthrough. Each one needs POSITIVE evidence
    of its own scope, and an error carrying neither is not a coin flip - it is RETRY when it
    is transient and ABORT when it is not. Rotating a healthy key costs a credential and
    proves nothing; failing over on a problem the fallback shares costs a second failed call
    and the first provider's goodwill. Neither shows up in a green suite.

WHAT THE BODIES BELOW ARE
    Every body marked CAPTURED is the verbatim response of a real request made while
    writing this test. The two marked CONSTRUCTED are assembled from each provider's
    documented error format because a rate limit cannot be provoked on demand; they are
    labelled so nobody later reads them as measurements.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway
from agent_core.ports.model_gateway import ModelAttempt, ModelGateway, RecoveryStrategy

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"

GEMINI = "gemini/gemini-3-flash-preview"
MINIMAX = "minimax/MiniMax-M3"


# --- The error shapes, as the two live providers actually emit them -----------------

# CAPTURED - gemini/gemini-3-flash-preview, deliberately malformed key, HTTP 400.
GEMINI_BAD_CREDENTIAL: dict[str, Any] = {
    "error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            },
            {
                "@type": "type.googleapis.com/google.rpc.LocalizedMessage",
                "locale": "en-US",
                "message": "API key not valid. Please pass a valid API key.",
            },
        ],
    }
}

# CAPTURED - minimax, healthy key, a model that does not exist. HTTP 400, same as above.
MINIMAX_DEAD_ROUTE: dict[str, Any] = {
    "type": "error",
    "error": {
        "type": "bad_request_error",
        "message": "invalid params, unknown model 'minimax-nosuchmodel-xyz' (2013)",
        "http_code": "400",
    },
    "request_id": "0000000000000000000000000000000",
}

# CAPTURED - minimax, deliberately malformed key. HTTP 401 - the status the port expected.
MINIMAX_BAD_CREDENTIAL: dict[str, Any] = {
    "type": "error",
    "error": {
        "type": "authorized_error",
        "message": (
            "login fail: Please carry the API secret key in the 'Authorization' field "
            "of the request header (1004)"
        ),
        "http_code": "401",
    },
    "request_id": "0000000000000000000000000000000",
}

# CAPTURED - gemini, healthy key, the retired model docs/FIELD-NOTES.md warns about.
GEMINI_DEAD_ROUTE: dict[str, Any] = {
    "error": {
        "code": 404,
        "message": (
            "This model models/gemini-2.5-flash is no longer available to new users. "
            "Please update your code to use models/gemini-3.6-flash for the latest "
            "features and improvements."
        ),
        "status": "NOT_FOUND",
    }
}

# CAPTURED - gemini, a path that resolves to nothing. Names no model, so it is not a route
# verdict: a generic 404 is a wrong URL or a proxy glitch, and falling over on it would
# blame a healthy model.
GEMINI_GENERIC_NOT_FOUND: dict[str, Any] = {
    "error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}
}

# CONSTRUCTED from Google's documented QuotaFailure shape - a 429 whose violations are
# scoped to OUR project. The credential is what ran out, so another model on the same key
# runs out too.
GEMINI_PROJECT_QUOTA: dict[str, Any] = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota. Please check your plan and billing.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": (
                            "generativelanguage.googleapis.com/generate_content_requests"
                        ),
                        "quotaId": "GenerateRequestsPerMinutePerProjectPerModel",
                    }
                ],
            }
        ],
    }
}

# CONSTRUCTED from the aggregator wrapper ports/model_gateway.py names in its own docstring:
# the SAME 429, but the failure belongs to an upstream the key never authenticated against.
UPSTREAM_RATE_LIMIT: dict[str, Any] = {
    "error": {
        "code": 429,
        "message": "Provider returned error",
        "metadata": {"provider_name": "minimax", "raw": "rate limit reached for MiniMax-M3"},
    }
}


class _ProviderError(Exception):
    """A provider failure in the shape the OpenAI-compatible SDKs hand one over.

    `status_code` plus a parsed `body`; the message is the JSON, because that is what
    LiteLLM puts there when it re-raises.
    """

    def __init__(self, status_code: int | None, body: dict[str, Any] | None) -> None:
        super().__init__(json.dumps(body) if body is not None else "")
        self.status_code = status_code
        self.body = body


class _WrappedProviderError(Exception):
    """The other real shape: LiteLLM stringifies the provider body into the message.

    No `.body`, no `.status_code` - everything the classifier needs is inside `str(error)`.
    This is the common case in production and the reason evidence extraction cannot simply
    read attributes.
    """


def _wrapped(prefix: str, body: dict[str, Any]) -> _WrappedProviderError:
    return _WrappedProviderError(f"litellm.APIError: {prefix} - {json.dumps(body)}")


@pytest.fixture
def gateway() -> LiteLLMGateway:
    return LiteLLMGateway()


# --- The anchor assertion ------------------------------------------------------------


@pytest.mark.phase("Later")
def test_one_http_status_two_scopes_two_strategies(gateway: LiteLLMGateway) -> None:
    """HTTP 400 from Gemini is a dead credential; HTTP 400 from MiniMax is a dead route.

    Both bodies were captured from live requests on the same afternoon. If a classifier
    reads the status first it cannot tell them apart, and whichever answer it picks is
    wrong half the time: it rotates a healthy key, or it fails over onto a second provider
    with a credential that was never the problem.
    """
    credential = gateway.classify_error(
        _ProviderError(400, GEMINI_BAD_CREDENTIAL), ModelAttempt(model=GEMINI)
    )
    route = gateway.classify_error(
        _ProviderError(400, MINIMAX_DEAD_ROUTE),
        ModelAttempt(model="minimax/MiniMax-NoSuchModel-xyz"),
    )

    # The headline claim first, deliberately: it is the one that must hold before either
    # specific verdict matters, and asserting the two values first would let a type checker
    # narrow this to a constant and quietly stop reading it as a test.
    assert credential is not route, (
        "The same HTTP status classified the same way from two providers. Status is not "
        "evidence; it is the last thing to look at, after the provider's own identity for "
        "the failure."
    )
    assert credential is RecoveryStrategy.ROTATE_KEY, (
        "Gemini reports an invalid API key as HTTP 400 / INVALID_ARGUMENT with "
        "reason=API_KEY_INVALID - not the 401 the port's pseudo-code expected. The "
        f"evidence names the credential, so the strategy is ROTATE_KEY, not {credential}."
    )
    assert route is RecoveryStrategy.FALLBACK_MODEL, (
        "MiniMax reports an unknown model as HTTP 400 too, and rotating the key cannot "
        f"make a model exist. The evidence names the route, so FALLBACK_MODEL, not {route}."
    )


@pytest.mark.phase("Later")
def test_the_mirror_case_each_provider_on_the_other_s_status(gateway: LiteLLMGateway) -> None:
    """MiniMax says credential with a 401; Gemini says route with a 404. Also unshared.

    Together with the test above this is the whole point: the four live shapes use four
    combinations of (status, scope), and only scope predicts the strategy.
    """
    credential = gateway.classify_error(
        _ProviderError(401, MINIMAX_BAD_CREDENTIAL), ModelAttempt(model=MINIMAX)
    )
    route = gateway.classify_error(
        _ProviderError(404, GEMINI_DEAD_ROUTE), ModelAttempt(model="gemini/gemini-2.5-flash")
    )

    assert credential is RecoveryStrategy.ROTATE_KEY, f"401 authorized_error, got {credential}"
    assert route is RecoveryStrategy.FALLBACK_MODEL, (
        "docs/FIELD-NOTES.md records this exact body: a model the listing advertises and "
        f"the account cannot call. The key is healthy; the route is gone. Got {route}."
    )


@pytest.mark.phase("Later")
def test_the_same_429_splits_on_whose_limit_was_hit(gateway: LiteLLMGateway) -> None:
    """Our project's quota rotates; an upstream provider's rate limit falls back.

    This is the case ports/model_gateway.py argues in prose - "an aggregator's upstream 429
    and your own key's 429 arrive as the same 429, and the correct responses are opposite".
    The two bodies are CONSTRUCTED (a rate limit cannot be provoked on demand), but the
    branch they exercise is the one that costs money when it is wrong.
    """
    ours = gateway.classify_error(
        _ProviderError(429, GEMINI_PROJECT_QUOTA), ModelAttempt(model=GEMINI)
    )
    theirs = gateway.classify_error(
        _ProviderError(429, UPSTREAM_RATE_LIMIT), ModelAttempt(model=MINIMAX)
    )

    assert ours is RecoveryStrategy.ROTATE_KEY, (
        "A QuotaFailure whose violations are scoped PerProject is our credential running "
        f"out. Another model on the same key runs out too. Got {ours}."
    )
    assert theirs is RecoveryStrategy.FALLBACK_MODEL, (
        "An upstream wrapper names a provider our key never authenticated against. "
        f"Rotating would burn a healthy credential on someone else's saturation. Got {theirs}."
    )


# --- The anti-guess rules ------------------------------------------------------------


@pytest.mark.phase("Later")
def test_a_status_with_no_scope_is_not_a_guess(gateway: LiteLLMGateway) -> None:
    """No credential evidence and no route evidence means neither strategy is earned."""
    bare_429 = gateway.classify_error(
        _ProviderError(429, {"error": {"message": "Too many requests"}}),
        ModelAttempt(model=GEMINI),
    )
    bare_400 = gateway.classify_error(
        _ProviderError(400, {"error": {"message": "Bad Request"}}), ModelAttempt(model=MINIMAX)
    )

    assert bare_429 is RecoveryStrategy.RETRY, (
        "A rate limit that names neither our account nor an upstream is transient until "
        f"something says otherwise - back off on the same request. Got {bare_429}."
    )
    assert bare_400 is RecoveryStrategy.ABORT, (
        "An unrecognised 400 must ABORT. Retrying an unknown error in a loop is how one "
        f"bad request becomes a bill - ports/model_gateway.py says so outright. Got {bare_400}."
    )


@pytest.mark.phase("Later")
def test_a_404_that_does_not_name_the_attempted_route_is_not_a_fallback(
    gateway: LiteLLMGateway,
) -> None:
    """Gemini's generic "Requested entity was not found" blames nothing we asked for.

    Treating every 404 as a dead model fails over on a wrong URL or a proxy glitch, retries
    the same mistake against the second provider, and reports the healthy model as broken.
    """
    strategy = gateway.classify_error(
        _ProviderError(404, GEMINI_GENERIC_NOT_FOUND), ModelAttempt(model=GEMINI)
    )

    assert strategy is RecoveryStrategy.ABORT, (
        "This 404 names no model and carries no upstream. There is no evidence the route "
        f"is the problem, so there is no fallback verdict to make. Got {strategy}."
    )


@pytest.mark.phase("Later")
def test_a_credential_failure_after_a_refresh_aborts_instead_of_rotating_again(
    gateway: LiteLLMGateway,
) -> None:
    """`401/403 after refresh -> ABORT` is in the port's pseudo-code and was unreachable.

    "After refresh" is state the exception cannot carry: the same 401 body arrives whether
    it is the first attempt or the second. The pre-widening signature `classify_error(error)`
    could not be handed it, so this branch could not exist - which is why the port grew
    `ModelAttempt`. Without it the recovery loop rotates, fails, rotates, and burns the pool.
    """
    first = gateway.classify_error(
        _ProviderError(401, MINIMAX_BAD_CREDENTIAL), ModelAttempt(model=MINIMAX)
    )
    after_refresh = gateway.classify_error(
        _ProviderError(401, MINIMAX_BAD_CREDENTIAL),
        ModelAttempt(model=MINIMAX, credential_refreshed=True),
    )

    assert first is RecoveryStrategy.ROTATE_KEY, f"expected a first rotation, got {first}"
    assert after_refresh is RecoveryStrategy.ABORT, (
        "A credential that still fails after it was refreshed is not a rotation candidate; "
        f"it is a permanent auth failure. Got {after_refresh}."
    )


@pytest.mark.phase("Later")
def test_an_overflow_compresses_and_never_fails_over(gateway: LiteLLMGateway) -> None:
    """Failing over on an overflow just overflows the next provider too."""
    strategy = gateway.classify_error(
        _ProviderError(
            400,
            {
                "error": {
                    "message": (
                        "This model's maximum context length is 245760 tokens. "
                        "However, your messages resulted in 261000 tokens."
                    ),
                    "code": "context_length_exceeded",
                }
            },
        ),
        ModelAttempt(model=MINIMAX),
    )

    assert strategy is RecoveryStrategy.COMPRESS, (
        "An overflow is a property of the request, not of the route or the credential. "
        f"COMPRESS, then send the same request smaller. Got {strategy}."
    )


@pytest.mark.phase("Later")
def test_a_transient_server_failure_retries(gateway: LiteLLMGateway) -> None:
    strategy = gateway.classify_error(
        _ProviderError(503, {"error": {"status": "UNAVAILABLE", "message": "model overloaded"}}),
        ModelAttempt(model=GEMINI),
    )

    assert strategy is RecoveryStrategy.RETRY, f"503 UNAVAILABLE is a backoff, got {strategy}"


@pytest.mark.phase("Later")
def test_an_error_with_no_status_and_no_body_aborts(gateway: LiteLLMGateway) -> None:
    """Unrecognised MUST be ABORT - the one default the port states as a rule."""
    strategy = gateway.classify_error(RuntimeError("something odd"), ModelAttempt(model=GEMINI))

    assert strategy is RecoveryStrategy.ABORT, (
        f"An unrecognised error must fail loudly so the case gets added. Got {strategy}."
    )


@pytest.mark.phase("Later")
def test_the_evidence_is_found_when_the_body_is_only_inside_the_message(
    gateway: LiteLLMGateway,
) -> None:
    """LiteLLM commonly re-raises with the provider body stringified into the message.

    A classifier that only reads `.body` and `.status_code` sees nothing at all here and
    aborts every provider error in production while every unit test passes - exactly the
    shape of silent bug CLAUDE.md's table is about.
    """
    credential = gateway.classify_error(
        _wrapped("GeminiException", GEMINI_BAD_CREDENTIAL), ModelAttempt(model=GEMINI)
    )
    route = gateway.classify_error(
        _wrapped("MinimaxException", MINIMAX_DEAD_ROUTE),
        ModelAttempt(model="minimax/MiniMax-NoSuchModel-xyz"),
    )

    assert credential is RecoveryStrategy.ROTATE_KEY, f"got {credential} from a wrapped body"
    assert route is RecoveryStrategy.FALLBACK_MODEL, f"got {route} from a wrapped body"


# --- The regression lock on the widening ---------------------------------------------
#
# t-f1-10 froze this port; t-later-03 widened it. Per the t-f1-05 precedent the lock's
# NEGATIVE case is the PRE-widening shape, so the widening cannot be quietly undone - a
# lock that only checked the new signature would accept the old one just as happily.

WIDENED_STUB = """
from __future__ import annotations

from agent_core.ports.model_gateway import ModelAttempt, ModelGateway, RecoveryStrategy


class StubGateway:
    def model_id_for(self, profile_model: str) -> str:
        raise NotImplementedError

    def base_url(self) -> str | None:
        raise NotImplementedError

    def classify_error(self, error: Exception, attempt: ModelAttempt) -> RecoveryStrategy:
        raise NotImplementedError


gateway: ModelGateway = StubGateway()
"""

PRE_WIDENING_STUB = """
from __future__ import annotations

from agent_core.ports.model_gateway import ModelGateway, RecoveryStrategy


class GatewayClassifyingOnTheErrorAlone:
    def model_id_for(self, profile_model: str) -> str:
        raise NotImplementedError

    def base_url(self) -> str | None:
        raise NotImplementedError

    def classify_error(self, error: Exception) -> RecoveryStrategy:
        raise NotImplementedError


gateway: ModelGateway = GatewayClassifyingOnTheErrorAlone()
"""


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module against the real port.

    Written outside the repository tree on purpose: a fixture that deliberately fails to
    type-check must never be picked up by the project-wide mypy run.
    """
    module = tmp_path / "snippet.py"
    module.write_text(source, encoding="utf-8")

    env = dict(os.environ)
    env["MYPYPATH"] = str(SRC_DIR)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=str(CORE_DIR),
        env=env,
        check=False,
    )


@pytest.mark.phase("Later")
def test_a_gateway_taking_the_attempt_satisfies_the_port(tmp_path: Path) -> None:
    result = _type_check(WIDENED_STUB, tmp_path)

    assert result.returncode == 0, (
        "The widened signature must be what ModelGateway accepts.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("Later")
def test_the_pre_widening_gateway_is_no_longer_a_model_gateway(tmp_path: Path) -> None:
    """The negative case IS the old signature - that is what makes this a lock.

    A gateway handed only the exception cannot answer the port's own `401/403 after
    refresh -> ABORT` branch, because "after refresh" is not in any provider's body. A port
    that cannot be handed what its own docstring requires is the defect (t-f1-04, t-f1-05).
    """
    result = _type_check(PRE_WIDENING_STUB, tmp_path)

    assert result.returncode != 0, (
        "ModelGateway accepted a gateway whose classify_error takes only the exception. "
        "The widening has been undone and the refresh branch is unreachable again."
    )
    assert "Incompatible types in assignment" in result.stdout, (
        "Expected the assignment to ModelGateway to be the rejected expression.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("Later")
def test_the_attempt_carries_the_route_and_the_refresh_state_and_is_immutable() -> None:
    """Two fields, both of them things the exception cannot say.

    `model` is what makes FALLBACK_MODEL meaningful - you fall back FROM something, and a
    404 is only a route verdict when it names the route we asked for. `credential_refreshed`
    is the caller's own history. Frozen because a classifier must not edit its evidence.
    """
    attempt = ModelAttempt(model=GEMINI)

    assert attempt.model == GEMINI
    assert attempt.credential_refreshed is False, (
        "A first attempt must default to 'not yet refreshed'; defaulting the other way "
        "would abort on the very first rotatable 401."
    )
    with pytest.raises(AttributeError):
        attempt.model = MINIMAX  # type: ignore[misc]


@pytest.mark.phase("Later")
def test_classify_error_is_still_sync_and_still_returns_the_port_s_enum() -> None:
    """The widening added a seat; it must not have changed what the method is.

    D13: pure lookups stay sync. And a caller must never be handed a status code to
    interpret itself - that is the thing the port exists to stop.
    """
    signature = inspect.signature(ModelGateway.classify_error)

    assert not inspect.iscoroutinefunction(ModelGateway.classify_error), (
        "classify_error inspects an exception already in memory. Making it async puts an "
        "await on every model failure for nothing."
    )
    assert signature.return_annotation in (RecoveryStrategy, "RecoveryStrategy"), (
        f"classify_error must return RecoveryStrategy, not {signature.return_annotation!r}"
    )
