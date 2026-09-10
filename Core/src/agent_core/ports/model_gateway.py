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
"""

from __future__ import annotations

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

    def classify_error(self, error: Exception) -> RecoveryStrategy:
        """PSEUDO-CODE - implement LATE, after a second provider is in play.

        Priority-ordered matching, first match wins. Start with the coarse cases and let
        production teach you the rest:
            context overflow / 413            -> COMPRESS
            401 / 403 after refresh           -> ABORT
            401 / 402 / our 429               -> ROTATE_KEY
            upstream 429 / 404 model          -> FALLBACK_MODEL
            500 / 502 / 503 / timeout         -> RETRY
            content policy                    -> ABORT
            anything unrecognised             -> ABORT

        UNRECOGNISED MUST BE ABORT, not RETRY. An unknown error retried in a loop is how
        a single bad request becomes a bill. Fail loudly and add the case.

        When something odd appears, check ../../Hermes-Core/Hermes/agent/error_classifier.py
        first - it probably already has the case with a comment explaining why.
        """
        ...
