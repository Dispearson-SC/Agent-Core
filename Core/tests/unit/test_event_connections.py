"""C1p (D55): the event adapters reuse pooled connections instead of connecting per call.

Subjects: adapters/driven/events_pg/connections.py (`EventConnections`), publisher.py and reader.py.
The LISTEN side (one long-lived listener) is tests/integration/test_event_feed.py.

What is defended: a publish and a read check a connection out of ONE small pool that is opened on
first use, never `psycopg.connect`; two adapters given the same `EventConnections` share it; and
`close()` closes the pool.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.events_pg.connections import EventConnections
from agent_core.adapters.driven.events_pg.publisher import PgEventPublisher
from agent_core.adapters.driven.events_pg.reader import PgEventReader
from agent_core.domain.events import EventKind, EventWindow, ProgressEvent, progress_event_id


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> _Result:
        self.statements.append(sql)
        return _Result([])


class _Pool:
    def __init__(self, conninfo: str, **options: Any) -> None:
        self.conninfo, self.options = conninfo, options
        self.opened = 0
        self.closed = 0
        self.checkouts = 0
        self.connections: list[_Connection] = []

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    @contextmanager
    def connection(self, timeout: float | None = None) -> Any:
        self.checkouts += 1
        connection = _Connection()
        self.connections.append(connection)
        yield connection


@pytest.fixture
def no_direct_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("an event adapter opened its own connection instead of the pool")

    monkeypatch.setattr(psycopg, "connect", refuse)


def _event(n: int) -> ProgressEvent:
    return ProgressEvent(
        event_id=progress_event_id("t", EventKind.TOOL_CALLED, str(n)),
        kind=EventKind.STARTED,
        tenant_id="t",
        session_id="s",
        turn_id="turn",
        agent="a",
        at=datetime(2026, 10, 12, tzinfo=UTC),
    )


def _connections() -> tuple[EventConnections, list[_Pool]]:
    made: list[_Pool] = []

    def factory(conninfo: str, **options: Any) -> _Pool:
        made.append(_Pool(conninfo, **options))
        return made[-1]

    return EventConnections("postgresql://x/y", pool_factory=factory), made


@pytest.mark.usefixtures("no_direct_connections")
def test_publishing_many_events_reuses_one_pool_opened_on_first_use() -> None:
    connections, made = _connections()
    publisher = PgEventPublisher(connections)

    assert made == [], "building the adapter must not create or open anything"
    for n in range(3):
        asyncio.run(publisher.publish(_event(n)))

    (pool,) = made
    assert pool.opened == 1 and pool.checkouts == 3
    assert pool.options.get("min_size") == 1 and pool.options.get("max_size", 0) <= 4


@pytest.mark.usefixtures("no_direct_connections")
def test_the_reader_and_the_publisher_share_the_pool_they_are_given() -> None:
    connections, made = _connections()
    publisher, reader = PgEventPublisher(connections), PgEventReader(connections)

    async def scenario() -> EventWindow:
        await publisher.publish(_event(0))
        assert await reader.latest_seq() == 0
        assert await reader.after(0) == ()
        return await reader.tree("t", "turn")

    window = asyncio.run(scenario())

    (pool,) = made
    assert window == EventWindow((), 0)
    assert pool.checkouts == 4, "one checkout per call, all from the same pool"


def test_closing_the_connections_closes_the_pool_once() -> None:
    connections, made = _connections()
    asyncio.run(PgEventPublisher(connections).publish(_event(0)))

    connections.close()
    connections.close()

    assert made[0].closed == 1


def test_an_adapter_built_from_a_conninfo_gets_its_own_connections() -> None:
    assert isinstance(PgEventPublisher("postgresql://x/y")._connections, EventConnections)  # noqa: SLF001


def test_the_container_shares_one_events_pool_and_closes_it_with_the_others() -> None:
    from typing import cast

    from tests.unit.test_composition import _composition, _RecordingPool

    container = _composition().build_container(pool_factory=cast(Any, _RecordingPool))

    shared = container.events_connections
    assert isinstance(shared, EventConnections)
    assert container.events._connections is shared  # noqa: SLF001
    assert container.event_feed._reader._connections is shared  # noqa: SLF001
    assert container.task_graph._connections is shared  # noqa: SLF001
    pool = _Pool("postgresql://x/y")
    shared._pool = pool  # noqa: SLF001

    container.close_pools()

    assert pool.closed == 1
