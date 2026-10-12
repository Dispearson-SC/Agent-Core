"""Driven adapter: the peer answer cache over Postgres (C25, D57).

Implements `ports/answer_cache.py`. Connections come from a `ConnectionFactory` (in production
`PoolConnections(domain_pool)`), every call is one short transaction in a thread (D13), and the
database's clock decides expiry so two processes agree on what is still fresh.

A `put` also removes the rows that have expired, in the same transaction: the table cannot grow
past what was written inside one TTL without anybody running a janitor. A cache is optional, so a
connection failure raises and the caller treats it as a miss or a skipped store.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Final

from agent_core.domain.answer_cache import AnswerCacheKey, CachedAnswer

__all__ = ["PgAnswerCache"]

ConnectionFactory = Callable[[], AbstractContextManager[Any]]

_GET_SQL: Final[str] = (
    "SELECT answer, typed FROM peer_answer_cache WHERE key_digest = %s AND expires_at > now()"
)
_PURGE_SQL: Final[str] = "DELETE FROM peer_answer_cache WHERE expires_at <= now()"
_PUT_SQL: Final[str] = """
    INSERT INTO peer_answer_cache (key_digest, tenant_id, target, answer, typed, expires_at)
    VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
    ON CONFLICT (key_digest) DO UPDATE
        SET answer = EXCLUDED.answer, typed = EXCLUDED.typed,
            created_at = now(), expires_at = EXCLUDED.expires_at
"""
_INVALIDATE_TENANT_SQL: Final[str] = "DELETE FROM peer_answer_cache WHERE tenant_id = %s"
_INVALIDATE_TARGET_SQL: Final[str] = (
    "DELETE FROM peer_answer_cache WHERE tenant_id = %s AND target = %s"
)


class PgAnswerCache:
    __slots__ = ("_connect",)

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    async def get(self, key: AnswerCacheKey) -> CachedAnswer | None:
        return await asyncio.to_thread(self._get_sync, key)

    def _get_sync(self, key: AnswerCacheKey) -> CachedAnswer | None:
        with self._connect() as conn:
            row = conn.execute(_GET_SQL, (key.digest(),)).fetchone()
        return None if row is None else CachedAnswer(str(row[0]), typed=bool(row[1]))

    async def put(self, key: AnswerCacheKey, answer: CachedAnswer, ttl_seconds: int) -> None:
        await asyncio.to_thread(self._put_sync, key, answer, ttl_seconds)

    def _put_sync(self, key: AnswerCacheKey, answer: CachedAnswer, ttl_seconds: int) -> None:
        with self._connect() as conn:
            conn.execute(_PURGE_SQL)
            conn.execute(
                _PUT_SQL,
                (
                    key.digest(),
                    key.tenant_id,
                    key.target,
                    answer.wrapped,
                    answer.typed,
                    ttl_seconds,
                ),
            )

    async def invalidate(self, tenant_id: str, target: str | None = None) -> int:
        return await asyncio.to_thread(self._invalidate_sync, tenant_id, target)

    def _invalidate_sync(self, tenant_id: str, target: str | None) -> int:
        with self._connect() as conn:
            if target is None:
                cursor = conn.execute(_INVALIDATE_TENANT_SQL, (tenant_id,))
            else:
                cursor = conn.execute(_INVALIDATE_TARGET_SQL, (tenant_id, target))
            return int(cursor.rowcount)
