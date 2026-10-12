"""An in-memory `AnswerCache` with the same rules as the Postgres one (see
`tests/contract/test_answer_cache.py`, which runs both through one suite)."""

from __future__ import annotations

from collections.abc import Callable

from agent_core.domain.answer_cache import AnswerCacheKey, CachedAnswer


class InMemoryAnswerCache:
    def __init__(self, clock: Callable[[], float] = lambda: 0.0) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[AnswerCacheKey, CachedAnswer, float]] = {}
        self.gets = 0
        self.puts: list[tuple[AnswerCacheKey, CachedAnswer, int]] = []
        self.fail = False

    async def get(self, key: AnswerCacheKey) -> CachedAnswer | None:
        self.gets += 1
        if self.fail:
            raise RuntimeError("the cache is down")
        found = self._entries.get(key.digest())
        if found is None or found[2] <= self._clock():
            return None
        return found[1]

    async def put(self, key: AnswerCacheKey, answer: CachedAnswer, ttl_seconds: int) -> None:
        if self.fail:
            raise RuntimeError("the cache is down")
        self.puts.append((key, answer, ttl_seconds))
        self._entries[key.digest()] = (key, answer, self._clock() + ttl_seconds)

    async def invalidate(self, tenant_id: str, target: str | None = None) -> int:
        doomed = [
            digest
            for digest, (key, _, _) in self._entries.items()
            if key.tenant_id == tenant_id and (target is None or key.target == target)
        ]
        for digest in doomed:
            del self._entries[digest]
        return len(doomed)
