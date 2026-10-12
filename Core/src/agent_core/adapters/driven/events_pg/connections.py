"""Driven adapter: the small connection pool of the event adapters (C1p, D55).

Phase:   Agent-comms v2, C1p
Tests:   Core/tests/unit/test_event_connections.py

The publisher writes one row per progress event (a tool call, a turn start, a turn end) and the
feed's reader polls; both used to open a connection per call. They now check one out of this pool,
which is OPENED ON FIRST USE: building an adapter connects to nothing (the composition root builds
before a database is required), and a process that never publishes never pays for the pool.

It is a pool of its own, not the domain pool or the audit pool: an event is a courtesy copy, so it
must neither queue behind a turn's connections nor take one from them. Small on purpose (a few
short statements). A checkout that times out raises, and the publisher already logs and drops an
event it cannot write.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from typing import Any, Final, Protocol

from psycopg_pool import ConnectionPool

__all__ = ["EventConnections"]

DEFAULT_MAX_SIZE: Final[int] = 4
DEFAULT_CHECKOUT_TIMEOUT_SECONDS: Final[float] = 5.0


class _PoolFactory(Protocol):
    def __call__(self, conninfo: str, **options: Any) -> Any: ...


def _connection_pool(conninfo: str, **options: Any) -> ConnectionPool[Any]:
    return ConnectionPool(conninfo, **options)


class EventConnections:
    """A lazily opened pool. Call it for a connection context manager; `close()` it at shutdown."""

    __slots__ = ("_conninfo", "_lock", "_max_size", "_factory", "_pool", "_timeout")

    def __init__(
        self,
        conninfo: str,
        *,
        max_size: int = DEFAULT_MAX_SIZE,
        checkout_timeout: float = DEFAULT_CHECKOUT_TIMEOUT_SECONDS,
        pool_factory: _PoolFactory = _connection_pool,
    ) -> None:
        self._conninfo = conninfo
        self._max_size = max_size
        self._timeout = checkout_timeout
        self._factory = pool_factory
        self._lock = threading.Lock()
        self._pool: Any = None

    @property
    def conninfo(self) -> str:
        return self._conninfo

    def __call__(self) -> AbstractContextManager[Any]:
        with self._lock:
            if self._pool is None:
                pool = self._factory(
                    self._conninfo, min_size=1, max_size=self._max_size, open=False
                )
                pool.open()
                self._pool = pool
            pool = self._pool
        return pool.connection(timeout=self._timeout)  # type: ignore[no-any-return]

    def close(self) -> None:
        """Close the pool if it was opened. Safe to call twice."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
