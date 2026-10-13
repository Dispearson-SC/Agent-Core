"""The `ToolJournal` rules (C30), run against the in-memory fake and the Postgres adapter."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.tool_journal import PgToolJournal
from agent_core.composition import PoolConnections
from agent_core.domain.turn import TurnId
from agent_core.ports.tool_journal import ToolJournalUnavailableError, ToolRunState
from tests.contract.harnesses import database_name_for
from tests.fakes import peer_world
from tests.fakes.tool_journal import FakeToolJournal

pytestmark = pytest.mark.asyncio

_DATABASE = database_name_for("tooljournal")
T1, T2 = TurnId("turn-1"), TurnId("turn-2")


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
    asyncio.run(migrations.run_migrations(conninfo))
    yield conninfo


class ToolJournalConformance:
    @pytest.fixture
    def journal(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_the_first_begin_is_fresh_and_a_repeat_sees_it_started(
        self, journal: Any
    ) -> None:
        assert await journal.begin(T1, "call-1", "send") is ToolRunState.FRESH
        assert await journal.begin(T1, "call-1", "send") is ToolRunState.STARTED

    async def test_a_finished_call_is_seen_finished(self, journal: Any) -> None:
        await journal.begin(T1, "call-1", "send")
        await journal.finish(T1, "call-1")
        await journal.finish(T1, "call-1")  # idempotent

        assert await journal.begin(T1, "call-1", "send") is ToolRunState.FINISHED

    async def test_calls_and_turns_are_kept_apart(self, journal: Any) -> None:
        await journal.begin(T1, "call-1", "send")

        assert await journal.begin(T1, "call-2", "send") is ToolRunState.FRESH
        assert await journal.begin(T2, "call-1", "send") is ToolRunState.FRESH

    async def test_an_abandoned_call_is_fresh_again(self, journal: Any) -> None:
        await journal.begin(T1, "call-1", "send")
        await journal.abandon(T1, "call-1")
        await journal.abandon(T1, "never-begun")  # idempotent

        assert await journal.begin(T1, "call-1", "send") is ToolRunState.FRESH

    async def test_finishing_a_call_nobody_began_is_harmless(self, journal: Any) -> None:
        await journal.finish(T1, "ghost")

        assert await journal.begin(T1, "ghost", "send") is ToolRunState.FRESH

    async def test_two_attempts_racing_for_one_call_cannot_both_see_it_fresh(
        self, journal: Any
    ) -> None:
        states = await asyncio.gather(*(journal.begin(T1, "call-1", "send") for _ in range(5)))

        assert states.count(ToolRunState.FRESH) == 1


class TestFakeToolJournal(ToolJournalConformance):
    @pytest.fixture
    def journal(self) -> Any:
        return FakeToolJournal()


class TestPostgresToolJournal(ToolJournalConformance):
    @pytest.fixture
    def journal(self, pg_conninfo: str) -> Iterator[Any]:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            conn.execute("DELETE FROM tool_executions")
        with ConnectionPool(pg_conninfo, min_size=1, max_size=6) as pool:
            yield PgToolJournal(PoolConnections(pool))

    async def test_a_saturated_pool_fails_in_the_defined_way(self, pg_conninfo: str) -> None:
        """R4-journal-pool-checkout: a checkout that times out surfaces as
        `ToolJournalUnavailableError`, not a bare pool error, for begin, finish and abandon."""
        with ConnectionPool(pg_conninfo, min_size=1, max_size=1) as pool:
            journal = PgToolJournal(PoolConnections(pool, timeout=0.1))
            with pool.connection():  # the only connection is taken
                with pytest.raises(ToolJournalUnavailableError):
                    await journal.begin(T1, "call-1", "send")
                with pytest.raises(ToolJournalUnavailableError):
                    await journal.finish(T1, "call-1")
                with pytest.raises(ToolJournalUnavailableError):
                    await journal.abandon(T1, "call-1")
