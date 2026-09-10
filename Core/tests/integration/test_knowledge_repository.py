"""Integration tests for the Postgres-backed `KnowledgeBase`.

Phase:   F8 - Knowledge: Skills and FULL_TEXT
Tasks:   docs/TASKS.md#t-f8-04
Covers:  adapters/driven/knowledge_pg/repository.py
         adapters/driven/persistence_pg/knowledge_migration.py

WHAT IS BEING DEFENDED

    1. THE TENANT PREDICATE IS IN THE SQL. `ports/knowledge_base.py` instructs its adapter
       to "filter by tenant IN THE QUERY, never post-retrieval - cross-tenant leakage
       through a shared index is the classic multi-tenant RAG breach". A Python filter
       applied to rows already fetched produces the SAME visible answer on a passing test
       and the WRONG one the day the filter is edited, skipped by an early return, or
       bypassed by a second code path. So this suite does not assert on the returned rows
       alone: it records the exact statements the adapter hands to psycopg and asserts the
       predicate is in every one of them, with the tenant among the parameters.

       That is the shape `docs/TASKS.md#t-f8-08` asks for - "a behavioural case in the
       adapter suite proving the SQL that `t-f8-04` actually emits carries the predicate".
       The structural half runs without any infrastructure at all, because the assertion it
       makes is the one that must never be allowed to lapse quietly.

    2. A DOCUMENT EFFECTIVE IN THE FUTURE IS NOT RETURNED. `KnowledgeAdmin.upsert` calls an
       `effective_from` in the future "a SCHEDULED change - new hours starting Monday", and
       says retrieval already filters on it so no separate scheduler is needed. If this
       filter is missing, an administrator who schedules Monday's price list gets it quoted
       to customers today, and nothing raises: the query returns MORE rows, not fewer.

       The assertion is written as a pair on purpose. It first asserts the CURRENT document
       comes back, then that the future one does not. An adapter that returns nothing at
       all passes the second half alone, which is exactly the green-on-empty test
       `docs/WAVES.md` refuses.

The structural test needs no infrastructure and always runs. The rest need a real Postgres
and skip cleanly without one, the same pattern as test_human_gateway.py.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.knowledge_pg import repository as repository_module
from agent_core.adapters.driven.knowledge_pg.repository import PgKnowledgeBase
from agent_core.adapters.driven.persistence_pg import knowledge_migration, migrations
from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    TenantKnowledgePolicy,
)
from agent_core.domain.turn import TenantId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TENANT_A = TenantId("t-knowledge-a")
_TENANT_B = TenantId("t-knowledge-b")
_PRICING = CollectionId("pricing")

# The same doc id in both tenants. Document ids are guessable, and the port says so: "A
# `DocId` from another tenant is exactly the 'missing' case". Reusing one id here means a
# missing predicate cannot hide behind an id that happened not to collide.
_SHARED_DOC_ID = DocId("doc-pricing-current")


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


class _RecordingConnection:
    """A psycopg connection that keeps every statement and parameter set it is handed.

    This exists so the tenant assertion can be made about the SQL rather than about the
    rows. Rows cannot tell the two implementations apart: a query narrowed by tenant and a
    query narrowed afterwards in Python return the same list on a good day.
    """

    def __init__(self, conninfo: str, log: list[tuple[str, Any]]) -> None:
        self._conninfo = conninfo
        self._log = log
        self._connection: psycopg.Connection[Any] | None = None

    def __enter__(self) -> _RecordingConnection:
        connection = psycopg.connect(self._conninfo)
        connection.__enter__()
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.__exit__(exc_type, exc, traceback)

    def execute(self, sql: str, params: Any = None) -> Any:
        self._log.append((sql, params))
        assert self._connection is not None
        return self._connection.execute(sql, params)


def _recording_factory(
    conninfo: str, log: list[tuple[str, Any]]
) -> Any:  # a ConnectionFactory over the recorder
    def connect() -> _RecordingConnection:
        return _RecordingConnection(conninfo, log)

    return connect


def _policy(tenant_id: TenantId, *, min_score: float = 0.0) -> TenantKnowledgePolicy:
    return TenantKnowledgePolicy(
        enabled=True,
        collections=(_PRICING,),
        min_score=min_score,
        tenant_id=tenant_id,
    )


def _insert_doc(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    doc_id: str,
    title: str,
    body: str,
    effective_from: datetime,
) -> None:
    """Seed one document straight through SQL.

    Deliberately NOT through a write adapter: `KnowledgeBase` has no write method and
    never will (non-negotiable #8), and the admin adapter is not this anchor's file.
    """
    conn.execute(
        """
        INSERT INTO knowledge_docs
            (tenant_id, doc_id, collection, title, body, version, effective_from)
        VALUES (%s, %s, %s, %s, %s, 1, %s)
        ON CONFLICT (tenant_id, doc_id) DO UPDATE
            SET title = EXCLUDED.title,
                body = EXCLUDED.body,
                effective_from = EXCLUDED.effective_from
        """,
        (tenant_id, doc_id, str(_PRICING), title, body, effective_from),
    )


def _sql_constants() -> list[str]:
    """Every module-level SQL string the repository can hand to Postgres."""
    return [
        value
        for name, value in vars(repository_module).items()
        if isinstance(value, str) and not name.startswith("__") and "knowledge_docs" in value
    ]


def _statements_touching_knowledge(log: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [(sql, params) for sql, params in log if "knowledge_docs" in sql]


def test_every_statement_the_repository_can_emit_names_the_tenant_in_its_where_clause() -> None:
    """No infrastructure needed, and that is the point: this must never lapse quietly.

    A read path against `knowledge_docs` that does not carry `tenant_id = %s` is the
    multi-tenant RAG breach the port docstring names, and it fails no other test - a
    missing predicate returns MORE rows, and more rows look like a better search engine.
    """
    constants = _sql_constants()

    assert constants, (
        "the repository module exposes no SQL constant against `knowledge_docs`; the "
        "tenant predicate cannot be asserted against SQL that does not exist yet"
    )
    for sql in constants:
        assert "tenant_id = %s" in sql, (
            "a statement against `knowledge_docs` carries no tenant predicate:\n"
            f"{sql}\n"
            "Filtering by tenant after the rows are fetched is the classic multi-tenant "
            "RAG breach; ports/knowledge_base.py forbids it by name."
        )


@pytest.fixture(scope="module")
def app_conninfo() -> Iterator[str]:
    if not _postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    app_db = "agent_core_knowledge_test"
    dbos_db = "agent_core_knowledge_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(conninfo))
    asyncio.run(knowledge_migration.apply_knowledge_migration(conninfo))
    yield conninfo


@pytest.fixture
def seeded(app_conninfo: str) -> Iterator[str]:
    """Tenant A's current and scheduled price lists, plus tenant B's own current one."""
    now = datetime.now(UTC)
    with psycopg.connect(app_conninfo) as conn:
        conn.execute(
            "DELETE FROM knowledge_docs WHERE tenant_id IN (%s, %s)",
            (_TENANT_A, _TENANT_B),
        )
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_A),
            doc_id=str(_SHARED_DOC_ID),
            title="Standard delivery pricing",
            body="Standard delivery costs 40 pesos across the whole city.",
            effective_from=now - timedelta(days=7),
        )
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_A),
            doc_id=f"doc-pricing-monday-{uuid.uuid4()}",
            title="Standard delivery pricing from Monday",
            body="Standard delivery costs 55 pesos across the whole city.",
            effective_from=now + timedelta(days=3),
        )
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_B),
            doc_id=str(_SHARED_DOC_ID),
            title="Standard delivery pricing",
            body="Standard delivery costs 900 pesos across the whole city.",
            effective_from=now - timedelta(days=7),
        )
    yield app_conninfo


def test_a_document_whose_effective_from_is_in_the_future_is_not_retrieved(seeded: str) -> None:
    """A scheduled price change must not be quoted before it takes effect.

    `KnowledgeAdmin.upsert` promises that "retrieval already filters on it, so this needs
    no separate scheduler". This is the test that makes that promise true; without the
    predicate an administrator preparing Monday's prices changes today's.
    """
    base = PgKnowledgeBase(lambda: psycopg.connect(seeded))
    hits = asyncio.run(base.search(_policy(_TENANT_A), "standard delivery pricing"))

    titles = [hit.title for hit in hits]

    assert "Standard delivery pricing" in titles, (
        "the document that is in force right now was not retrieved at all, so the "
        f"assertion below would pass on an empty result and prove nothing; got {titles!r}"
    )
    assert "Standard delivery pricing from Monday" not in titles, (
        "a document whose `effective_from` has not arrived was retrieved; a scheduled "
        f"change is being quoted to customers before it takes effect. Got {titles!r}"
    )

    context = asyncio.run(base.full_context(_policy(_TENANT_A)))
    assert "55 pesos" not in context, (
        "the future price reached `full_context`, which is injected into the system "
        "prompt - the agent would quote it confidently for the whole conversation"
    )
    assert "40 pesos" in context, "the current price is missing from the injected context"


def test_the_tenant_narrowing_is_a_predicate_in_the_sql_and_not_a_python_filter(
    seeded: str,
) -> None:
    """Assert on the statements, not only on the rows.

    Rows cannot distinguish a query narrowed by tenant from one narrowed afterwards in
    Python; both return tenant A's document on a good day. The difference only shows up on
    a bad day, in production, as one business reading another's prices. So the recorder
    below reads what actually went to Postgres.
    """
    log: list[tuple[str, Any]] = []
    base = PgKnowledgeBase(_recording_factory(seeded, log))

    hits = asyncio.run(base.search(_policy(_TENANT_A), "standard delivery pricing"))
    asyncio.run(base.get(_policy(_TENANT_A), _SHARED_DOC_ID))
    asyncio.run(base.full_context(_policy(_TENANT_A)))

    statements = _statements_touching_knowledge(log)
    assert statements, "the repository reached Postgres without naming `knowledge_docs`"

    for sql, params in statements:
        assert "tenant_id = %s" in sql, (
            f"a statement reached Postgres with no tenant predicate:\n{sql}"
        )
        assert params is not None and _TENANT_A in tuple(params), (
            "the tenant predicate is in the SQL but the tenant was never bound to it; "
            f"parameters were {params!r}"
        )

    # And the behaviour that predicate buys: tenant B's document shares tenant A's doc id
    # and its collection, and is invisible from A either way in.
    bodies = [hit.excerpt for hit in hits]
    assert not any("900 pesos" in body for body in bodies), (
        "tenant B's price list was returned to tenant A - the shared index leaked"
    )
    doc = asyncio.run(base.get(_policy(_TENANT_A), _SHARED_DOC_ID))
    assert doc is not None and "40 pesos" in doc.body, (
        "tenant A could not fetch its own document by the id it shares with tenant B"
    )
    assert asyncio.run(base.get(_policy(_TENANT_B), _SHARED_DOC_ID)) is not None, (
        "the shared id resolves for neither tenant, so the previous assertion proved "
        "nothing about the predicate"
    )
    other = asyncio.run(base.get(_policy(_TENANT_B), _SHARED_DOC_ID))
    assert other is not None and "900 pesos" in other.body, (
        "the same DocId returned the same document to both tenants; the predicate is "
        "absent or is being applied to rows already fetched"
    )


def test_a_collection_the_policy_does_not_grant_returns_nothing(seeded: str) -> None:
    """An agent asking for a corpus it may not read gets an empty result, not an error.

    `collection` is a permission boundary, and the port is explicit that an error message
    "confirms the collection exists, which is itself a leak".
    """
    base = PgKnowledgeBase(lambda: psycopg.connect(seeded))
    denied = TenantKnowledgePolicy(
        enabled=True, collections=(CollectionId("delivery"),), min_score=0.0, tenant_id=_TENANT_A
    )

    assert asyncio.run(base.search(denied, "standard delivery pricing")) == ()
    assert asyncio.run(base.get(denied, _SHARED_DOC_ID)) is None
    assert asyncio.run(base.full_context(denied)) == ""
