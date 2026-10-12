"""Run the `AgentMailbox` conformance suite against each adapter (`mailbox_conformance.py`)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations
from tests.contract.harnesses import A2AHarness, PgHarness, database_name_for
from tests.contract.mailbox_conformance import MailboxConformance, MailboxHarness
from tests.fakes import peer_world

pytestmark = pytest.mark.asyncio

_DATABASE = database_name_for("mailbox")


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


class TestPostgresMailbox(MailboxConformance):
    @pytest.fixture
    def harness(self, pg_conninfo: str) -> MailboxHarness:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            conn.execute("DELETE FROM peer_messages")
        return PgHarness(pg_conninfo)


class TestA2AMailbox(MailboxConformance):
    @pytest.fixture
    def harness(self) -> MailboxHarness:
        return A2AHarness()
