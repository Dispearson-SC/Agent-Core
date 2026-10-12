"""The `AnswerCache` rules (C25, D57), run against the in-memory fake and the Postgres adapter."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_core.adapters.driven.peers.answer_cache import PgAnswerCache
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.composition import PoolConnections
from agent_core.domain.answer_cache import AnswerCacheKey, CachedAnswer
from tests.contract.harnesses import database_name_for
from tests.fakes import peer_world
from tests.fakes.answer_cache import InMemoryAnswerCache

pytestmark = pytest.mark.asyncio

_DATABASE = database_name_for("answer_cache")


def _key(
    question: str = "what is owed",
    *,
    tenant: str = "t",
    target: str = "billing",
    version: str = "v1",
) -> AnswerCacheKey:
    return AnswerCacheKey(tenant, target, "subject:u", question, version)


ANSWER = CachedAnswer("<untrusted>42</untrusted>")


@pytest.fixture(scope="module")
def pg_conninfo() -> Iterator[str]:
    if not peer_world.postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    asyncio.run(
        migrations.ensure_databases(
            peer_world.ADMIN_CONNINFO, app_database=_DATABASE, dbos_database=f"{_DATABASE}_dbos_sys"
        )
    )
    conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_DATABASE}\1", peer_world.ADMIN_CONNINFO)
    asyncio.run(migrations.apply_all_migrations(conninfo))
    yield conninfo


class AnswerCacheConformance:
    @pytest.fixture
    def cache(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    async def expire(self, cache: Any, seconds: float) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_a_stored_answer_is_returned_for_the_same_key(self, cache: Any) -> None:
        await cache.put(_key(), ANSWER, 60)

        assert await cache.get(_key()) == ANSWER

    async def test_an_unknown_key_is_a_miss(self, cache: Any) -> None:
        assert await cache.get(_key()) is None

    async def test_each_part_of_the_key_separates_entries(self, cache: Any) -> None:
        await cache.put(_key(), ANSWER, 60)

        assert await cache.get(_key(tenant="other")) is None
        assert await cache.get(_key(target="support")) is None
        assert await cache.get(_key(version="v2")) is None
        assert await cache.get(_key("what is paid")) is None
        assert (
            await cache.get(AnswerCacheKey("t", "billing", "subject:other", "what is owed", "v1"))
            is None
        )

    async def test_an_expired_answer_is_a_miss(self, cache: Any) -> None:
        await cache.put(_key(), ANSWER, 1)
        assert await cache.get(_key()) == ANSWER

        await self.expire(cache, 1.5)

        assert await cache.get(_key()) is None

    async def test_putting_again_replaces_the_answer_and_restarts_the_clock(
        self, cache: Any
    ) -> None:
        await cache.put(_key(), ANSWER, 1)
        await cache.put(_key(), CachedAnswer("newer", typed=True), 60)
        await self.expire(cache, 1.5)

        assert await cache.get(_key()) == CachedAnswer("newer", typed=True)

    async def test_invalidating_a_tenant_forgets_all_its_entries_and_only_its_own(
        self, cache: Any
    ) -> None:
        await cache.put(_key(), ANSWER, 60)
        await cache.put(_key(target="support"), ANSWER, 60)
        await cache.put(_key(tenant="other"), ANSWER, 60)

        removed = await cache.invalidate("t")

        assert removed == 2
        assert await cache.get(_key()) is None and await cache.get(_key(target="support")) is None
        assert await cache.get(_key(tenant="other")) == ANSWER

    async def test_invalidating_a_target_leaves_the_tenants_other_targets(self, cache: Any) -> None:
        await cache.put(_key(), ANSWER, 60)
        await cache.put(_key(target="support"), ANSWER, 60)

        assert await cache.invalidate("t", "billing") == 1

        assert await cache.get(_key()) is None
        assert await cache.get(_key(target="support")) == ANSWER

    async def test_invalidating_nothing_removes_nothing(self, cache: Any) -> None:
        assert await cache.invalidate("t") == 0


class TestInMemoryAnswerCache(AnswerCacheConformance):
    @pytest.fixture
    def cache(self) -> Any:
        self.now = [0.0]
        return InMemoryAnswerCache(clock=lambda: self.now[0])

    async def expire(self, cache: Any, seconds: float) -> None:
        self.now[0] += seconds


class TestPostgresAnswerCache(AnswerCacheConformance):
    @pytest.fixture
    def cache(self, pg_conninfo: str) -> Iterator[Any]:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            conn.execute("DELETE FROM peer_answer_cache")
        with ConnectionPool(pg_conninfo, min_size=1, max_size=2) as pool:
            yield PgAnswerCache(PoolConnections(pool))

    async def expire(self, cache: Any, seconds: float) -> None:
        time.sleep(seconds)  # the database's clock decides expiry

    async def test_calls_reuse_pooled_connections_instead_of_opening_one_each(
        self, cache: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_: Any, **__: Any) -> None:
            raise AssertionError("the cache opened a connection of its own")

        monkeypatch.setattr(psycopg, "connect", refuse)

        await cache.put(_key(), ANSWER, 60)
        assert await cache.get(_key()) == ANSWER
        assert await cache.invalidate("t") == 1

    async def test_the_table_stores_a_digest_and_never_the_question_in_clear(
        self, cache: Any, pg_conninfo: str
    ) -> None:
        """A cache table that held the question text would be a second copy of what customers
        asked, outside the transcript's retention. The digest is the key; the answer is the
        callee's own wrapped text."""
        await cache.put(_key("my secret question"), ANSWER, 60)

        with psycopg.connect(pg_conninfo) as conn:
            columns = {
                str(r[0])
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'peer_answer_cache'"
                ).fetchall()
            }
            dump = str(conn.execute("SELECT * FROM peer_answer_cache").fetchall())
        assert "question" not in columns and "my secret question" not in dump
