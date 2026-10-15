"""The fast path's answer cache key (Phase 5, S10, roadmap 5.8). Pure.

A composite's reply may be remembered (`AGENT_CORE_FAST_PATH_CACHE_SECONDS`, off by default) in the
answer cache Core already has (`ports/answer_cache.py`, D57). Only a composite's reply is stored:
a model turn's or a route's never is. This module says what the reply is keyed by.

Its own module because `domain/profile.py` imports `domain/routing.py`, which imports
`domain/fast_path.py`.
"""

from __future__ import annotations

from agent_core.domain.answer_cache import AnswerCacheKey, normalize_question
from agent_core.domain.fast_path import CompositeCall
from agent_core.domain.profile import AgentProfile, profile_content_hash
from agent_core.domain.turn import TurnRequest

__all__ = ["composite_cache_key"]


def composite_cache_key(
    request: TurnRequest, composite: CompositeCall, composer: AgentProfile
) -> AnswerCacheKey:
    """Where a composite's reply is remembered (S10, roadmap 5.8): the tenant; the intents it
    answered; the case (the session) and the caller - the person, its channel, its roles (sorted)
    and whom it acts for (P13: a policy may branch on any of them, so two callers differing in one
    never share a reply); the normalised question; and a version made of the caller's data
    version and the composer's content hash. A newer data version, another case or a changed
    composer never reads an older reply; the TTL bounds the rest."""
    caller = request.caller
    acting_for = "" if caller.on_behalf_of is None else caller.on_behalf_of.subject_id
    return AnswerCacheKey(
        tenant_id=str(request.session.tenant_id),
        target="fast_path:" + ",".join(composite.intents),
        audience=(
            f"session:{request.session.session_id}|subject:{caller.subject_id}"
            f"|channel:{caller.channel}|roles:{','.join(sorted(caller.roles))}"
            f"|for:{acting_for}"
        ),
        question=normalize_question(request.input.text),
        version=f"{request.data_version or ''}|"
        f"{composer.content_hash or profile_content_hash(composer)}",
    )
