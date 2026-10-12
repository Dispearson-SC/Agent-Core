"""Port: AnswerCache - remembered peer answers (C25, D57).

Phase:   Agent-comms v2, wave 4
Adapter: adapters/driven/peers/answer_cache.py (Postgres)
Per-vertical: NO

A cache, not a source of truth: every verb may be unavailable and the caller treats that as a miss
(`get` that raises is a miss, `put` that raises stores nothing), so a cache outage costs latency and
never an answer. The rules about WHAT may be stored and WHO may be handed it are not here: they are
`domain/answer_cache.py` and the call sites in the peer workflow.

`get(key)`
    The answer stored under the key that has not expired, or None. Exact match on the key digest;
    there is no fuzzy lookup.
`put(key, answer, ttl_seconds)`
    Remember it for `ttl_seconds` (replacing an older entry for the same key). The adapter's clock
    decides expiry, not the caller's.
`invalidate(tenant_id, target=None)`
    Forget everything remembered for that tenant, or only for one target agent. Returns how many
    entries were removed. This is the hook an operator or a data-load job calls; a tenant is
    required so one call can never clear another tenant's entries.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.answer_cache import AnswerCacheKey, CachedAnswer

__all__ = ["AnswerCache"]


class AnswerCache(Protocol):
    async def get(self, key: AnswerCacheKey) -> CachedAnswer | None: ...

    async def put(self, key: AnswerCacheKey, answer: CachedAnswer, ttl_seconds: int) -> None: ...

    async def invalidate(self, tenant_id: str, target: str | None = None) -> int: ...
