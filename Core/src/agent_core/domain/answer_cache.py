"""The peer answer cache: what a cached answer is keyed by, and who may share one (C25, D57).

Pure: no I/O, no framework. The port is `ports/answer_cache.py`.

A specialist asked the same thing twice within a short while (a voice user repeating a question, an
orchestrator asking again after a retry) pays for a whole turn each time. This remembers the
answer so the second ask costs a lookup. It is OFF unless both the deployment and the answering
profile opt in, and it is built so that it can only ever return what the same ask would have
returned a moment ago, to the same kind of caller:

  * the KEY is (tenant, target, audience, normalised question, version). The tenant and target are
    plain; the AUDIENCE is who may share the answer (the asking subject by default, or the asking
    roles when the profile says its answers do not depend on who asks); the VERSION is the answering
    profile's content hash plus an operator-set data version, so a changed profile or a data load
    makes every older entry unreachable instead of stale.
  * only an answer that is `ok` is ever stored: an error, a timeout, a refusal, a cancellation or
    a typed answer that broke its schema is never remembered, so a bad moment is not replayed.
  * the policy gate runs BEFORE the lookup (`turn_workflow._step_ask_peers`), so a peer the asker
    may not ask stays refused whatever is in the cache.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "MAX_CACHED_CHARS",
    "MAX_TTL_SECONDS",
    "AnswerCacheKey",
    "AnswerCacheScope",
    "CachedAnswer",
    "audience_for",
    "normalize_question",
]

MAX_TTL_SECONDS: Final[int] = 86_400
"""The longest an answer may be remembered: a day. A profile asking for more is refused at load."""

MAX_CACHED_CHARS: Final[int] = 50_000
"""The longest wrapped answer that is stored; a bigger one is simply not cached."""

_EDGE_PUNCTUATION: Final[str] = " ?!.¿¡"


class AnswerCacheScope(StrEnum):
    """Who may be handed an answer another caller caused to be remembered.

    SUBJECT  - only the same asking subject (the default: safe for an answer that depends on who
               asks, such as "my balance").
    ROLES    - any asking subject of the tenant with the same set of roles: for a specialist whose
               answers do not depend on the person (a catalogue, a policy lookup).
    """

    SUBJECT = "subject"
    ROLES = "roles"


def normalize_question(question: str) -> str:
    """The question as the cache compares it: Unicode-folded, case-folded, whitespace collapsed
    and trailing/leading punctuation dropped, so "What is the TOTAL for order 7 ?" and "what is
    the total for order 7" are one question. Words are never reordered or removed."""
    folded = unicodedata.normalize("NFKC", question).casefold()
    return " ".join(folded.split()).strip(_EDGE_PUNCTUATION)


def audience_for(scope: AnswerCacheScope, asker: str, subject_id: str, roles: Iterable[str]) -> str:
    """Who an answer is for under `scope`: the asking AGENT (a callee may tell one asker more than
    another) and then the subject, or the sorted roles."""
    if scope is AnswerCacheScope.SUBJECT:
        return f"{asker}|subject:{subject_id}"
    return f"{asker}|roles:" + ",".join(sorted(set(roles)))


@dataclass(frozen=True, slots=True)
class AnswerCacheKey:
    """What identifies a cached answer. `question` is already normalised."""

    tenant_id: str
    target: str
    audience: str
    question: str
    version: str

    def digest(self) -> str:
        """A fixed-size storage key. The parts are joined by a separator no part can contain
        (U+001F), so two different keys can never collide by concatenation."""
        joined = "\x1f".join(
            (self.tenant_id, self.target, self.audience, self.question, self.version)
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    """A remembered answer as the mailbox port returned it: already wrapped as untrusted content
    (non-negotiable #10), and whether it was a schema-checked typed answer."""

    wrapped: str
    typed: bool = False
