"""Tenant isolation of the Postgres pending-input buffer.

Phase:   F2 (durability / coalescing)
Tasks:   docs/TASKS.md#t-f2-04
Covers:  adapters/driven/persistence_pg/pending_input_repository.py

WHAT IS BEING DEFENDED
    A `SessionId` is unique only WITHIN a tenant. `append` has always stored the
    `tenant_id`, but `drain` deleted on `session_id` alone, so two tenants that both use
    the session id `s-1` drained each other's buffered sentences into their own turn: one
    customer's message answered inside another customer's conversation.

    Same class as the tenant holes already closed in `t-d2-06` (a `RuleSet` that recorded
    no tenant) and `t-f8-02` (a `KnowledgeBase` told to filter on a value its contract
    never carried). The lesson each of those wrote down applies unchanged here: a filter a
    caller can forget is not a boundary, and a missing predicate returns MORE rows, not
    fewer - so nothing fails, nothing warns, and the breach is invisible until it is a
    customer's message in a stranger's transcript.

WHY THIS ASSERTION IS THE FALSIFIABLE ONE
    The buffer's window key (the DBOS partition) and the audit rows both already carry the
    tenant; the buffer was the one place it was dropped. A test that drains a single tenant
    and checks its own rows come back passes with or without the predicate. Only two
    tenants sharing ONE session id can tell the two implementations apart, because only
    then does the broken statement return a strict superset.

    Needs a real Postgres and is skipped when one is not reachable - the same pattern as
    `test_pending_input.py` and `test_conversation_repository.py`.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.pending_input_repository import (
    PgPendingInputBuffer,
    apply_pending_input_migration,
)
from agent_core.domain.turn import SessionRef, UserInput

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


def _prepared_app_conninfo() -> str:
    app_db = "agent_core_pending_input_test"
    dbos_db = "agent_core_pending_input_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(apply_pending_input_migration(app_conninfo))
    return app_conninfo


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_drain_never_returns_another_tenants_buffered_input() -> None:
    """Two tenants, ONE session id: each drain returns only its own tenant's rows.

    The session id is deliberately identical for both tenants - that is the whole point.
    A `session_id`-only `DELETE ... RETURNING` returns four rows to the first caller and
    zero to the second; the assertions below are written so the first failure is the
    leak itself (`texts_for_a` carrying tenant B's sentence) rather than a count.
    """
    app_conninfo = _prepared_app_conninfo()
    buffer = PgPendingInputBuffer(app_conninfo)

    shared_session_id = f"s-{uuid.uuid4()}"
    session_a = SessionRef(session_id=shared_session_id, tenant_id="t-a")  # type: ignore[arg-type]
    session_b = SessionRef(session_id=shared_session_id, tenant_id="t-b")  # type: ignore[arg-type]

    asyncio.run(buffer.append(session_a, UserInput(text="tenant A: hola")))
    asyncio.run(buffer.append(session_b, UserInput(text="tenant B: mi tarjeta 4111")))
    asyncio.run(buffer.append(session_a, UserInput(text="tenant A: sobre mi pedido 123")))

    texts_for_a = [message.text for message in asyncio.run(buffer.drain(session_a))]

    assert "tenant B: mi tarjeta 4111" not in texts_for_a, (
        "cross-tenant leak: tenant A's drain returned a message buffered by tenant B "
        "under the same session id"
    )
    assert texts_for_a == ["tenant A: hola", "tenant A: sobre mi pedido 123"], (
        "tenant A must get exactly its own rows, in arrival order"
    )

    # Draining A must not have consumed B's row either. A leak that merely reordered
    # would be caught above; a leak that DELETED B's row without returning it would only
    # show here, as an empty drain for a tenant that is still waiting on its turn.
    texts_for_b = [message.text for message in asyncio.run(buffer.drain(session_b))]
    assert texts_for_b == ["tenant B: mi tarjeta 4111"], (
        "tenant A's drain must leave tenant B's buffered input untouched"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_drain_of_an_unbuffered_tenant_is_empty_not_the_other_tenants_rows() -> None:
    """A tenant that buffered nothing drains empty, even when its session id is in use.

    The mirror of the test above, and the one that fails LOUDEST under the broken
    statement: tenant B never appended anything at all, so any non-empty result is
    unambiguously another tenant's data rather than a mis-ordering or an off-by-one.
    """
    app_conninfo = _prepared_app_conninfo()
    buffer = PgPendingInputBuffer(app_conninfo)

    shared_session_id = f"s-{uuid.uuid4()}"
    session_a = SessionRef(session_id=shared_session_id, tenant_id="t-a")  # type: ignore[arg-type]
    session_b = SessionRef(session_id=shared_session_id, tenant_id="t-b")  # type: ignore[arg-type]

    asyncio.run(buffer.append(session_a, UserInput(text="tenant A only")))

    assert asyncio.run(buffer.drain(session_b)) == (), (
        "a tenant that buffered nothing must drain empty - anything returned here "
        "belongs to another tenant"
    )
    assert [message.text for message in asyncio.run(buffer.drain(session_a))] == [
        "tenant A only"
    ], "and tenant A's own row must still be there to drain"
