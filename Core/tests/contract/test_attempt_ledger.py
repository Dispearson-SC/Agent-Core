"""The `AttemptLedger` rules (C5), run against the in-memory fake and the Postgres adapter."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.attempt_ledger import PgAttemptLedger
from agent_core.composition import PoolConnections
from tests.contract.harnesses import database_name_for
from tests.fakes import peer_world
from tests.fakes.attempt_ledger import InMemoryAttemptLedger

pytestmark = pytest.mark.asyncio

_DATABASE = database_name_for("attempts")


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


class AttemptLedgerConformance:
    @pytest.fixture
    def ledger(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_every_entry_of_a_body_that_does_not_wait_counts(self, ledger: Any) -> None:
        assert [await ledger.begin("wf") for _ in range(4)] == [1, 2, 3, 4]

    async def test_workflows_are_counted_apart(self, ledger: Any) -> None:
        assert await ledger.begin("a") == 1
        assert await ledger.begin("b") == 1
        assert await ledger.begin("a") == 2

    async def test_an_entry_after_a_wait_starts_the_count_again(self, ledger: Any) -> None:
        await ledger.begin("wf")
        await ledger.begin("wf")
        await ledger.park("wf")  # killed while parked on a person

        assert await ledger.begin("wf") == 1, "a restart out of a wait is not a failed attempt"
        assert await ledger.begin("wf") == 2

    async def test_a_wake_ends_the_wait_so_a_crash_after_it_counts(self, ledger: Any) -> None:
        await ledger.begin("wf")
        await ledger.park("wf")
        await ledger.resume("wf")  # the answer arrived; the process dies working on it

        assert await ledger.begin("wf") == 2

    async def test_a_forgotten_workflow_starts_from_nothing(self, ledger: Any) -> None:
        await ledger.begin("wf")
        await ledger.begin("wf")
        await ledger.forget("wf")
        await ledger.forget("never-counted")  # idempotent

        assert await ledger.begin("wf") == 1

    async def test_parking_an_unknown_workflow_is_harmless(self, ledger: Any) -> None:
        await ledger.park("ghost")
        await ledger.resume("ghost")

        assert await ledger.begin("ghost") == 1


class TestInMemoryAttemptLedger(AttemptLedgerConformance):
    @pytest.fixture
    def ledger(self) -> Any:
        return InMemoryAttemptLedger()


class TestPostgresAttemptLedger(AttemptLedgerConformance):
    @pytest.fixture
    def ledger(self, pg_conninfo: str) -> Iterator[Any]:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            conn.execute("DELETE FROM workflow_attempts")
        with ConnectionPool(pg_conninfo, min_size=1, max_size=2) as pool:
            yield PgAttemptLedger(PoolConnections(pool))

    async def test_calls_reuse_pooled_connections_instead_of_opening_one_each(
        self, ledger: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D33 (R4): the ledger is touched at every workflow entry and around every wait; a new
        TCP connection and login per call is the cost this pool removes."""

        def refuse(*_: Any, **__: Any) -> None:
            raise AssertionError("the ledger opened a connection of its own")

        monkeypatch.setattr(psycopg, "connect", refuse)

        assert [await ledger.begin("wf") for _ in range(3)] == [1, 2, 3]
        await ledger.park("wf")
        await ledger.resume("wf")
        await ledger.forget("wf")


class TestLedgerUnderPoolSaturation:
    """C1k (R3-002): the ledger shares `domain_pool` with the request path. Behind a saturated
    pool it must give up in a moment (the workflow body then fails open), not wait out the pool's
    default 30 s checkout timeout at every workflow entry."""

    async def test_a_saturated_pool_makes_the_ledger_fail_fast(self, pg_conninfo: str) -> None:
        import time

        from agent_core.composition import attempt_ledger_for

        with ConnectionPool(pg_conninfo, min_size=1, max_size=1, timeout=30) as pool:
            with pool.connection():  # the only connection is busy with somebody else
                ledger = attempt_ledger_for(pool)
                started = time.monotonic()
                with pytest.raises(Exception, match="(?i)timeout|couldn't get a connection"):
                    await ledger.begin("wf")
                elapsed = time.monotonic() - started

        assert elapsed < 5, f"waited {elapsed:.1f}s for a connection"
