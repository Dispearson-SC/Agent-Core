"""Port: ModelGateway - which model and provider am I talking to?

Phase:      F0 (library) / D2 (proxy)
Tasks:      docs/TASKS.md#t-f1-10
Adapter:    adapters/driven/llm_litellm/
Per-vertical: NO

THIS PORT IS THE DAY-2 PAYOFF - THE WHOLE REASON IT EXISTS
    Day 1: LiteLLM imported in-process. Day 2: LiteLLM as a separate proxy with virtual
    keys and per-tenant budgets.

    That migration must be A CHANGE OF base_url AND NOTHING ELSE. If use cases called
    `litellm.completion()` directly, day 2 would be a cross-cutting refactor. This port
    is what turns it into a config edit. docs/ARCHITECTURE.md#11.

    Verified: LiteLLM's database is OPTIONAL. Without it you still get the
    OpenAI-compatible API, routing and failover; you lose virtual keys, budgets and spend
    tracking. A request carrying a virtual key against a DB-less proxy fails with
    "No connected db.", and NO budget enforces anything. docs/DECISIONS.md#d5.

THE LIMIT OF WHAT "SUPPORTED" MEANS - schedule this, do not discover it
    LiteLLM covers 100+ providers, OpenRouter, Kimi, GLM and MiniMax among them. But
    "supported" means the REQUEST FORMAT is translated. It does NOT mean failure modes
    are normalised.

    An aggregator's upstream 429 and your own key's 429 arrive as the same 429, and the
    correct responses are opposite: one needs a different model, the other a rotated
    credential. That is why `classify_error` is on this port and why it is scheduled
    LATE - five strategies, not Hermes' twenty-five reasons.

    Scheduling it late was right, and the second provider proved it harder than expected.
    See `classify_error` below: the two live providers do not even agree on which STATUS
    a dead credential gets.

WIDENED ONCE, BY t-later-03 - `classify_error` now takes a `ModelAttempt`
    `t-f1-10` froze this port. The widening is deliberate and its regression lock lives in
    `tests/unit/test_classify_error.py`, whose NEGATIVE case is the pre-widening signature,
    per the `t-f1-05` precedent - a lock that only checked the new shape would accept the
    old one too.

    The reason is in this module's own pseudo-code, which prescribed `401 / 403 AFTER
    REFRESH -> ABORT` while the frozen signature `classify_error(error)` could never be
    handed "after refresh". No provider puts it in a body; the same 401 arrives on the
    first attempt and on the second. A port that cannot be handed what its own contract
    requires is itself the defect - `t-f1-04` and `t-f1-05` one layer down, again.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RecoveryStrategy(StrEnum):
    """The five strategies. Deliberately NOT Hermes' twenty-five reasons.

    Hermes' error_classifier.py has ~25 failure reasons because it earned every one in
    production across forty providers. We start with the five ACTIONS those reasons map
    to, and add a reason only when an incident produces one.

    RETRY            transient; same request, backoff.
    ROTATE_KEY       this credential is the problem (401, 402, our own 429).
    FALLBACK_MODEL   this route is the problem; the key is healthy (upstream 429, 404).
    COMPRESS         the request is too large (context overflow, 413) - compact, do NOT
                     failover. Failing over on an overflow just overflows the next
                     provider too.
    ABORT            retrying unchanged cannot work (content policy, permanent auth).
    """

    RETRY = "retry"
    ROTATE_KEY = "rotate_key"
    FALLBACK_MODEL = "fallback_model"
    COMPRESS = "compress"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class ModelAttempt:
    """What the CALLER knows about the failed request and the exception cannot say.

    Two fields, and each one exists because a branch of `classify_error` is unreachable
    without it. Nothing else belongs here: this is evidence, not a request record.

    model
        The route that was attempted, as the profile names it (`provider/model`). It is
        what makes FALLBACK_MODEL meaningful at all - you fall back FROM something - and
        it is how a 404 or an "unknown model" is checked against the route we actually
        asked for. A 404 naming some other resource is a wrong URL, not a dead model, and
        failing over on it blames a healthy route while repeating the same mistake.

    credential_refreshed
        Whether this credential was already rotated or refreshed for this request. The
        port's own table separates `401/402 -> ROTATE_KEY` from `401/403 after refresh ->
        ABORT`, and "after refresh" appears in no provider's body. Without it the recovery
        loop rotates, fails identically, rotates again, and burns the pool on a credential
        that was never going to work.

    Frozen because a classifier must not be able to edit its own evidence.
    """

    model: str
    credential_refreshed: bool = False


class ModelGateway(Protocol):
    def model_id_for(self, profile_model: str) -> str:
        """Map a profile's model name to the wire model id.

        Day 1 this is near-identity. Day 2 it may prefix a proxy route. Keeping it behind
        a method means the day-2 change does not touch any caller.
        """
        ...

    def base_url(self) -> str | None:
        """None on day 1 (library mode). On day 2, the proxy URL.

        THIS SINGLE RETURN VALUE IS THE ENTIRE DAY-2 CODE CHANGE.
        """
        ...

    def classify_error(self, error: Exception, attempt: ModelAttempt) -> RecoveryStrategy:
        """Which of the five recoveries this failure earns. Evidence first, status last.

        THE STATUS CODE IS THE LAST EVIDENCE, NOT THE FIRST
            The earlier version of this docstring was a status table - `401/402 ->
            ROTATE_KEY`, `404 model -> FALLBACK_MODEL`. Probed against both live providers
            on 2026-09-10 it gets the two most common failures wrong, because neither of
            them is in it:

                HTTP 400  gemini    reason=API_KEY_INVALID        the CREDENTIAL is dead
                HTTP 400  minimax   "unknown model '...' (2013)"  the ROUTE is dead

            One status, opposite recoveries. A status table cannot be repaired by adding
            400 to it; it has to stop leading with the status.

        WHAT EACH BRANCH NEEDS BEFORE IT MAY BE CHOSEN
            ROTATE_KEY needs evidence scoped to THE CREDENTIAL WE SENT: the provider names
                the key, the account, the project's quota, the balance or the virtual-key
                budget. Gemini says so with `reason=API_KEY_INVALID` or a `QuotaFailure`
                whose violations are per-project; MiniMax with `type=authorized_error` or
                an insufficient balance. Another model on the same key would fail too -
                that is the test for this branch.
            FALLBACK_MODEL needs evidence scoped to THE ROUTE WE ASKED FOR, with the
                credential accepted: the body names `attempt.model` as unknown, retired or
                unsupported, or it wraps an upstream provider we never authenticated
                against. Another key on the same route would fail too.
            COMPRESS needs evidence scoped to THE REQUEST: a context or payload limit.
                Never fail over on an overflow - the next provider overflows as well.
            RETRY needs evidence that the failure is TRANSIENT and belongs to nobody:
                5xx, UNAVAILABLE, overloaded, a timeout, or a rate limit that names
                neither our account nor an upstream.
            ABORT is everything else, and specifically a credential failure arriving when
                `attempt.credential_refreshed` is already true.

        NEITHER SCOPE MAY BE A FALLTHROUGH. An error carrying no credential evidence and
        no route evidence is not a coin flip between them. Rotating a healthy key spends a
        credential and fixes nothing; failing over on a problem the fallback shares spends
        a second call and the first provider's goodwill. Neither shows up in a green suite
        - this is the silent-bug table in CLAUDE.md, not a style preference.

        UNRECOGNISED MUST BE ABORT, not RETRY. An unknown error retried in a loop is how
        a single bad request becomes a bill. Fail loudly and add the case.

        When something odd appears, check ../../Hermes-Core/Hermes/agent/error_classifier.py
        first - it probably already has the case with a comment explaining why. Its
        `_status_429` is where the upstream-wrapper rule above comes from.
        """
        ...
