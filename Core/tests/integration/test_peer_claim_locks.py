"""C1m: a claim never holds one turn's lock while it waits for another's.

Two workers claiming for different targets can scan the same two asking turns in opposite order. If
a claim took a lock for every turn it scanned (including the turns it then skipped) and kept it
until commit, each could hold one and wait for the other: a deadlock. The test pins the
precondition instead of racing for the symptom: while a claim waits on a turn that somebody else
holds, it must hold no other turn's lock.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterator

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations
from tests.contract.harnesses import PgHarness, database_name_for
from tests.fakes import peer_world

_DATABASE = database_name_for("claim_locks")
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_TRY_LOCK = "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))"


@pytest.fixture(scope="module")
def conninfo() -> Iterator[str]:
    if not peer_world.postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    asyncio.run(
        migrations.ensure_databases(
            peer_world.ADMIN_CONNINFO, app_database=_DATABASE, dbos_database=f"{_DATABASE}_dbos_sys"
        )
    )
    info = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_DATABASE}\1", peer_world.ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(info))
    yield info


def test_a_claim_waiting_on_one_turn_holds_no_other_turns_lock(conninfo: str) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("DELETE FROM peer_messages")
    harness = PgHarness(conninfo)

    async def seed() -> tuple[str, str]:
        await harness.ask("running", turn_id="turn-capped", request_key="a-0", max_concurrent=1)
        assert await harness.claim() is not None, "turn-capped is now at its cap of one"
        await harness.ask("waits", turn_id="turn-capped", request_key="a-1", max_concurrent=1)
        busy = await harness.ask("busy", turn_id="turn-busy", request_key="b-0", max_concurrent=1)
        return busy, "turn-busy"

    busy_ask, busy_turn = asyncio.run(seed())

    claimed: list[object] = []
    # `turn-busy` is held by "another worker" (this connection), so the claim for its ask waits.
    other = psycopg.connect(conninfo)
    try:
        other.execute(_LOCK, (busy_turn,))
        worker = threading.Thread(target=lambda: claimed.append(asyncio.run(harness.claim())))
        worker.start()
        time.sleep(1.0)  # the claim has scanned the capped turn and is now waiting on the busy one
        probe = psycopg.connect(conninfo)
        try:
            row = probe.execute(_TRY_LOCK, ("turn-capped",)).fetchone()
            assert row is not None and row[0] is True, (
                "the waiting claim still holds the lock of a turn it skipped: with a second worker "
                "waiting the other way round, that is a deadlock"
            )
        finally:
            probe.rollback()
            probe.close()
        other.rollback()
        worker.join(timeout=30)
    finally:
        other.close()

    assert not worker.is_alive()
    assert len(claimed) == 1 and claimed[0] is not None
    assert getattr(claimed[0], "correlation_id", None) == busy_ask
