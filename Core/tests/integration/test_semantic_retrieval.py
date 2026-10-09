"""Integration tests for the pgvector-backed SEMANTIC `KnowledgeBase`.

Phase:   D2 - Multi-tenancy / SEMANTIC retrieval
Tasks:   docs/TASKS.md#t-d2-05
Covers:  adapters/driven/knowledge_pg/semantic.py
         adapters/driven/persistence_pg/semantic_migration.py

WHAT IS BEING DEFENDED, IN TWO HALVES

    HALF ONE - SEMANTIC RETRIEVAL FINDS DOCUMENTS BY MEANING, NOT BY KEYWORD, AND CARRIES
    THE SAME TENANT PREDICATE FULL_TEXT DOES.
        The seeded document and the query share not one word, so `PgKnowledgeBase.search`
        (FULL_TEXT) is asserted to return nothing for that exact query first - proving the
        pair is a genuine test of "by meaning" rather than an accident keyword search would
        also have passed. `semantic.py` is then handed a fake `EmbedQuery` mapping the query
        close to one document's stored embedding and far from another's, and must retrieve
        the related one and exclude the unrelated one. That is not a claim the fake embedder
        understands anything: it pins the one property the ADAPTER must have - ranking by
        embedding distance rather than lexical overlap - so a real model can be plugged in
        later without another change here.

        The tenant half is asserted against the STATEMENT psycopg receives, not against the
        rows, because rows cannot distinguish a tenant-scoped nearest-neighbour scan from
        one narrowed in Python afterwards, and a shared vector index is exactly the shape
        `ports/knowledge_base.py` calls the classic multi-tenant RAG breach.

    HALF TWO - A DATABASE WITHOUT PGVECTOR MUST STILL START, AND SEMANTIC MUST REFUSE
    LOUDLY RATHER THAN HALF-WORK.
        This half exists because it already went wrong. `migrations.discover_app_migrations`
        pkgutil-imports every module of the persistence package and applies every
        module-level `Migration` it finds, so a `CREATE EXTENSION vector` written as a
        discoverable migration takes the WHOLE startup path down on any deployment whose
        server has no pgvector - which is the cluster this suite runs against
        (docs/FIELD-NOTES.md, "The local cluster has no pgvector. The remote one does.").

        F8 calls SEMANTIC "one config line per collection plus pgvector in the existing
        instance". Opt-in per collection means the SCHEMA is opt-in too, so three things are
        asserted here: the semantic migration is NOT in the discovered set, startup against
        a pgvector-less database really does succeed, and both remaining doors are loud -
        enabling SEMANTIC where pgvector is unavailable raises at the point of enabling, and
        searching before the schema exists raises instead of returning zero rows. Silent
        degradation is the one unacceptable outcome: a retrieval that quietly returns
        nothing is indistinguishable from an empty corpus, and nothing anywhere reports it.

The structural tests need no infrastructure and always run. The pgvector-less tests need a
reachable Postgres and skip cleanly without one. The pgvector tests additionally require the
`vector` extension to be AVAILABLE on the server and skip where it is not - never weakened to
pass, because an assertion that passes without pgvector proves nothing about pgvector.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.knowledge_pg import semantic as semantic_module
from agent_core.adapters.driven.knowledge_pg.repository import PgKnowledgeBase
from agent_core.adapters.driven.knowledge_pg.semantic import PgSemanticKnowledgeBase
from agent_core.adapters.driven.persistence_pg import migrations, semantic_migration
from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    RetrievalMode,
    TenantKnowledgePolicy,
)
from agent_core.domain.turn import TenantId
from agent_core.ports.embedder import Embedding

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# Two databases, because the two halves need opposite schemas. The "plain" one is what a
# deployment that never enables SEMANTIC gets: the discovered migration set and nothing
# else. The "enabled" one additionally had the opt-in migration applied by hand, which is
# precisely what enabling SEMANTIC means.
_PLAIN_DATABASE = "agent_core_semantic_plain_test"
_ENABLED_DATABASE = "agent_core_semantic_enabled_test"

_TENANT_A = TenantId("t-semantic-a")
_TENANT_B = TenantId("t-semantic-b")
_RETURNS = CollectionId("returns")

_SHARED_DOC_ID = DocId("doc-returns-policy")

# Shares not one word (case-insensitively) with either seeded document's title or body, so
# FULL_TEXT's `plainto_tsquery` has nothing to match - see HALF ONE above.
_MEANING_QUERY = "get my money back"

# SQL that only a server with pgvector can execute. Matched by the four constructs the
# extension actually introduces rather than by the substring "vector", because `to_tsvector`
# contains it and migration 0013's full-text index is not a pgvector dependency at all - a
# guard that flags the built-in text search would be noise nobody keeps reading.
_NEEDS_PGVECTOR = re.compile(
    r"CREATE\s+EXTENSION[^;]*\bvector\b"  # the extension itself
    r"|USING\s+(?:hnsw|ivfflat)\b"  # its index access methods
    r"|::\s*vector\b"  # a cast to its type
    r"|\bvector\s*\(\s*\d+\s*\)",  # a column of its type
    re.IGNORECASE,
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _pgvector_available() -> bool:
    """Whether THIS server could `CREATE EXTENSION vector` at all.

    `pg_available_extensions` rather than `pg_extension`: the question is what the server
    is capable of, not what some database already installed. A capability belongs to a
    named database's server, never to "Postgres" - docs/FIELD-NOTES.md.
    """
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        return row is not None
    except psycopg.OperationalError:
        return False


def _conninfo_for(database: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{database}\1", _ADMIN_CONNINFO)


def _recreate_empty_database(database: str) -> str:
    """A database with nothing in it - not even `schema_migrations`.

    Dropped and recreated rather than truncated because the claim under test is what a
    FRESH DEPLOYMENT gets, and a leftover `schema_migrations` row would make the applier
    skip the very migration being asserted about.
    """
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{database}"')
    return _conninfo_for(database)


def _embedding_dimensions() -> int:
    """The dimension the opt-in migration fixes on the column.

    Read off the production module rather than restated here: pgvector fixes the dimension
    per column, so a literal in this file would silently stop matching the schema the day
    the default moves, and every insert below would fail for a reason that has nothing to
    do with what is being tested.
    """
    dimensions = getattr(semantic_migration, "EMBEDDING_DIM", None)
    assert isinstance(dimensions, int), (
        "semantic_migration.py does not say what dimension the embedding column has, so "
        "this suite cannot build a vector the column would accept"
    )
    return dimensions


def _topic_vector(axis: int) -> list[float]:
    """A unit vector on one axis of a tiny hand-built embedding space.

    Standing in for a real model (see `semantic.py`'s finding: no `ports/` protocol
    produces an embedding today). "Close" and "far" only need to be true relative to each
    other for the adapter's ranking to be provable.
    """
    values = [0.0] * _embedding_dimensions()
    values[axis] = 1.0
    return values


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


class _FakeEmbedder:
    """The local stub of `ports/embedder.py`, standing in for a real embedding model.

    Was a bare async callable until `t-d2-07` gave the question a port: nothing in `ports/`
    produced an embedding, so this adapter took an injected callable that no protocol
    owned. docs/WAVES.md rule 4 - the port moved and its local stubs move with it, in the
    same change.

    Every query in this suite asks about the refund topic, so every query embeds to the
    same point in the fake space. The model id is fake and deliberately says so: it exists
    to travel with the vector, not to be believed.
    """

    async def embed(self, text: str) -> Embedding:
        return Embedding(model="fake/embedding-1", vector=tuple(_topic_vector(0)))


_fake_embedder = _FakeEmbedder()


class _RecordingConnection:
    """A psycopg connection that keeps every statement and parameter set it is handed.

    Same shape as `test_knowledge_repository.py`'s recorder, for the same reason: rows
    cannot tell a tenant-scoped query from a Python filter applied afterwards, so the
    assertion has to be made about the SQL.
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


def _recording_factory(conninfo: str, log: list[tuple[str, Any]]) -> Any:
    def connect() -> _RecordingConnection:
        return _RecordingConnection(conninfo, log)

    return connect


def _policy(
    tenant_id: TenantId, *, mode: RetrievalMode = RetrievalMode.SEMANTIC, min_score: float = 0.0
) -> TenantKnowledgePolicy:
    return TenantKnowledgePolicy(
        enabled=True,
        collections=(_RETURNS,),
        mode=mode,
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
    embedding: Sequence[float] | None,
    effective_from: datetime,
) -> None:
    """Seed one document straight through SQL - `KnowledgeBase` has no write method and
    never will (non-negotiable #8), and the admin adapter is not this anchor's file."""
    conn.execute(
        """
        INSERT INTO knowledge_docs
            (tenant_id, doc_id, collection, title, body, version, effective_from, embedding)
        VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
        ON CONFLICT (tenant_id, doc_id) DO UPDATE
            SET title = EXCLUDED.title,
                body = EXCLUDED.body,
                effective_from = EXCLUDED.effective_from,
                embedding = EXCLUDED.embedding
        """,
        (
            tenant_id,
            doc_id,
            str(_RETURNS),
            title,
            body,
            effective_from,
            None if embedding is None else _vector_literal(embedding),
        ),
    )


# --------------------------------------------------------------------------------------
# HALF TWO, structural - no infrastructure at all, and that is the point.
# --------------------------------------------------------------------------------------


def _sql_constants() -> list[str]:
    """Every module-level SQL string the semantic adapter can hand to Postgres."""
    return [
        value
        for name, value in vars(semantic_module).items()
        if isinstance(value, str) and not name.startswith("__") and "knowledge_docs" in value
    ]


def test_every_statement_the_semantic_adapter_can_emit_names_the_tenant_in_its_where_clause() -> (
    None
):
    """SEMANTIC must carry the SAME predicate FULL_TEXT does, asserted with no database.

    A nearest-neighbour scan over `knowledge_docs` with no `tenant_id = %s` predicate is
    the multi-tenant RAG breach `ports/knowledge_base.py` names, and a shared vector index
    is exactly the shape it warns about. It fails no other test, because a missing
    predicate returns MORE rows, not fewer.
    """
    constants = _sql_constants()

    assert constants, (
        "the semantic adapter module exposes no SQL constant against `knowledge_docs`; the "
        "tenant predicate cannot be asserted against SQL that does not exist yet"
    )
    for sql in constants:
        assert "tenant_id = %s" in sql, (
            "a statement against `knowledge_docs` carries no tenant predicate:\n"
            f"{sql}\n"
            "SEMANTIC must carry the SAME predicate FULL_TEXT does - enabling it per "
            "collection is configuration, not a new place to get the tenant boundary wrong."
        )


def test_the_pgvector_schema_is_not_in_the_set_startup_discovers_and_applies() -> None:
    """The regression that broke the build, pinned where it costs nothing to check.

    `discover_app_migrations()` imports every module of the persistence package and
    production applies everything it returns, in order, against whatever database it was
    pointed at. A migration that cannot succeed on a valid deployment target must therefore
    not be in that set at all: on a server with no pgvector, one `CREATE EXTENSION vector`
    is not a failed SEMANTIC feature, it is a system that will not start.

    SEMANTIC is opt-in per collection (F8), so its schema is opt-in too - applied
    deliberately by whoever enables it, never by discovery.
    """
    discovered = migrations.discover_app_migrations()

    offenders = [
        migration.id
        for migration in discovered
        if _NEEDS_PGVECTOR.search(migration.sql) or migration.id.startswith("0021")
    ]
    assert offenders == [], (
        "startup discovery would apply a pgvector-dependent migration: "
        f"{offenders}. Migration discovery applies every module-level `Migration` in the "
        "persistence package, so this one statement takes the WHOLE startup path down on "
        "any server without pgvector - which is the cluster this suite runs against. The "
        "SEMANTIC schema is opt-in and must be applied by whoever enables SEMANTIC."
    )


# --------------------------------------------------------------------------------------
# HALF TWO, behavioural - needs a Postgres, and specifically does NOT need pgvector.
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_startup_succeeds_against_a_database_whose_server_has_no_pgvector() -> None:
    """A deployment that never enables SEMANTIC starts cleanly, schema and all.

    The real startup applier against a genuinely empty database. On the local cluster there
    is no `vector.control` at all, so this is not a simulation of the failure that happened
    - it is the failure that happened.
    """
    conninfo = _recreate_empty_database(_PLAIN_DATABASE)

    try:
        asyncio.run(migrations.apply_all_migrations(conninfo))
    except Exception as exc:  # the claim is that NOTHING escapes
        pytest.fail(
            "startup failed against a database whose server has no pgvector: "
            f"{type(exc).__name__}: {exc}. Every other anchor's tests start here too, so a "
            "migration that cannot run on this server is not a broken feature, it is a "
            "broken system."
        )

    with psycopg.connect(conninfo) as conn:
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}
        knowledge_docs = conn.execute("SELECT to_regclass('public.knowledge_docs')").fetchone()

    assert knowledge_docs is not None and knowledge_docs[0] is not None, (
        "startup did not create knowledge_docs, so this proves nothing about the schema "
        "surviving a pgvector-less server"
    )
    assert not [id_ for id_ in applied if id_.startswith("0021")], (
        f"the opt-in SEMANTIC migration was applied by startup: {sorted(applied)}"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
@pytest.mark.skipif(
    _pgvector_available(), reason="this server HAS pgvector; enabling cannot fail here"
)
def test_enabling_semantic_where_pgvector_is_unavailable_fails_at_the_point_of_enabling() -> None:
    """Loud at the moment of enabling, never a retrieval that returns nothing forever.

    A `CREATE EXTENSION vector` that fails with the server's raw error is already loud, but
    it is loud about the wrong thing: the operator reads "could not open extension control
    file" and not "this server cannot serve SEMANTIC". The named error is the contract.
    """
    conninfo = _recreate_empty_database(_ENABLED_DATABASE)
    asyncio.run(migrations.apply_all_migrations(conninfo))

    unavailable = getattr(semantic_migration, "PgVectorUnavailableError", None)
    assert isinstance(unavailable, type) and issubclass(unavailable, Exception), (
        "semantic_migration.py names no error for 'this server cannot serve SEMANTIC', so "
        "enabling it on a server without pgvector can only fail as a raw driver error - "
        "which reads as a bug in the schema rather than as an unsupported deployment."
    )

    with pytest.raises(unavailable) as raised:
        asyncio.run(semantic_migration.apply_semantic_migration(conninfo))

    assert "vector" in str(raised.value).lower(), (
        f"the refusal does not name the missing extension: {raised.value!r}"
    )

    with psycopg.connect(conninfo) as conn:
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    assert not [id_ for id_ in applied if id_.startswith("0021")], (
        "a failed enable still recorded its migration id, so a later retry would be "
        "skipped and the column would never exist"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_semantic_search_refuses_loudly_when_the_opt_in_schema_was_never_applied() -> None:
    """Zero rows is the one answer SEMANTIC must never give here.

    An empty tuple from a knowledge base is a VALID answer - `KnowledgePolicy.min_score`'s
    docstring says so - which is exactly why it cannot double as "this deployment was never
    set up". Indistinguishable from an empty corpus, it would be believed for as long as
    nobody happened to check the column existed.
    """
    conninfo = _recreate_empty_database(_PLAIN_DATABASE)
    asyncio.run(migrations.apply_all_migrations(conninfo))

    unavailable = getattr(semantic_module, "SemanticRetrievalUnavailableError", None)
    assert isinstance(unavailable, type) and issubclass(unavailable, Exception), (
        "semantic.py names no error for 'SEMANTIC is configured but its schema is not "
        "there', so the only outcomes left are a raw driver error or - far worse - an "
        "empty result that looks exactly like an empty corpus."
    )

    semantic = PgSemanticKnowledgeBase(lambda: psycopg.connect(conninfo), _fake_embedder)

    with pytest.raises(unavailable) as raised:
        asyncio.run(semantic.search(_policy(_TENANT_A), _MEANING_QUERY))

    message = str(raised.value).lower()
    assert "0021" in message or "semantic" in message, (
        f"the refusal does not say what is missing or how to enable it: {raised.value!r}"
    )


# --------------------------------------------------------------------------------------
# HALF ONE - needs pgvector on the server, and is skipped rather than weakened without it.
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def semantic_conninfo() -> Iterator[str]:
    if not _postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    if not _pgvector_available():
        pytest.skip("this server has no pgvector; SEMANTIC cannot be enabled here")
    conninfo = _recreate_empty_database(_ENABLED_DATABASE)
    asyncio.run(migrations.apply_all_migrations(conninfo))
    asyncio.run(semantic_migration.apply_semantic_migration(conninfo))
    yield conninfo


@pytest.fixture
def seeded(semantic_conninfo: str) -> Iterator[str]:
    """Tenant A gets a refund-topic doc and an unrelated shipping-topic doc; tenant B shares
    the refund doc's id, collection, text AND embedding, so a leak through the shared vector
    index would be a duplicate row rather than a merely missing one."""
    now = datetime.now(UTC)
    with psycopg.connect(semantic_conninfo) as conn:
        conn.execute(
            "DELETE FROM knowledge_docs WHERE tenant_id IN (%s, %s)",
            (str(_TENANT_A), str(_TENANT_B)),
        )
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_A),
            doc_id=str(_SHARED_DOC_ID),
            title="Purchase satisfaction",
            body="We will reimburse the full purchase price if you are unsatisfied.",
            embedding=_topic_vector(0),
            effective_from=now - timedelta(days=7),
        )
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_A),
            doc_id="doc-shipping-speed",
            title="Shipping speed",
            body="Packages ship within two business days from our warehouse.",
            embedding=_topic_vector(1),
            effective_from=now - timedelta(days=7),
        )
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_B),
            doc_id=str(_SHARED_DOC_ID),
            title="Purchase satisfaction",
            body="We will reimburse the full purchase price if you are unsatisfied.",
            embedding=_topic_vector(0),
            effective_from=now - timedelta(days=7),
        )
    yield semantic_conninfo


def test_semantic_retrieval_finds_a_document_sharing_no_words_with_the_query(
    seeded: str,
) -> None:
    """SEMANTIC ranks by embedding distance, so a query sharing not one word with a
    document's text still retrieves it - the opposite of FULL_TEXT, which requires lexical
    overlap through `plainto_tsquery`/`@@` and returns nothing for this exact pair."""
    full_text_hits = asyncio.run(
        PgKnowledgeBase(lambda: psycopg.connect(seeded)).search(
            _policy(_TENANT_A, mode=RetrievalMode.FULL_TEXT), _MEANING_QUERY
        )
    )
    assert full_text_hits == (), (
        "the query and the seeded document share no words, so FULL_TEXT must return "
        "nothing here - if it does not, this pair is not a valid test of 'by meaning "
        f"rather than by keyword'. Got {[hit.title for hit in full_text_hits]!r}"
    )

    semantic = PgSemanticKnowledgeBase(lambda: psycopg.connect(seeded), _fake_embedder)
    hits = asyncio.run(semantic.search(_policy(_TENANT_A, min_score=0.5), _MEANING_QUERY))

    titles = [hit.title for hit in hits]
    assert "Purchase satisfaction" in titles, (
        "the semantically related document was not retrieved by meaning, even though the "
        f"query and its embedding were made deliberately close; got {titles!r}"
    )
    assert "Shipping speed" not in titles, (
        "the unrelated document (a far embedding) was retrieved alongside the related one; "
        f"the ranking is not discriminating by meaning at all. Got {titles!r}"
    )


def test_the_tenant_predicate_is_the_same_shape_full_text_uses(seeded: str) -> None:
    """Assert on the statement psycopg actually receives, not only on the rows.

    Rows cannot distinguish a tenant-scoped nearest-neighbour scan from one narrowed
    afterwards in Python, and on a good day both return the same document.
    """
    log: list[tuple[str, Any]] = []
    semantic = PgSemanticKnowledgeBase(_recording_factory(seeded, log), _fake_embedder)

    hits = asyncio.run(semantic.search(_policy(_TENANT_A), _MEANING_QUERY))

    statements = [(sql, params) for sql, params in log if "knowledge_docs" in sql]
    assert statements, "the semantic adapter reached Postgres without naming knowledge_docs"
    for sql, params in statements:
        assert "tenant_id = %s" in sql, (
            f"a statement reached Postgres with no tenant predicate:\n{sql}"
        )
        assert params is not None and str(_TENANT_A) in tuple(params), (
            "the tenant predicate is in the SQL but the tenant was never bound to it; "
            f"parameters were {params!r}"
        )

    # And the behaviour that predicate buys: tenant B's row shares tenant A's doc id,
    # collection and embedding, and must still be invisible to tenant A's search.
    doc_ids = [str(hit.doc_id) for hit in hits]
    assert doc_ids.count(str(_SHARED_DOC_ID)) <= 1, (
        "the shared doc id came back more than once for a single tenant's query - a "
        "cross-tenant row leaked through the shared vector index"
    )
    other_tenant_doc = asyncio.run(
        PgSemanticKnowledgeBase(lambda: psycopg.connect(seeded), _fake_embedder).get(
            _policy(_TENANT_B), _SHARED_DOC_ID
        )
    )
    assert other_tenant_doc is not None, (
        "tenant B's own document is unreachable, so the leak check above proves nothing "
        "about the predicate"
    )


def test_a_document_never_embedded_is_silently_excluded_not_errored(seeded: str) -> None:
    """The failure mode `semantic.py`'s docstring names, pinned so it stays deliberate.

    A row with `embedding IS NULL` is not an error and not a zero-score hit: it simply
    never appears, for as long as nothing re-embeds it.
    """
    with psycopg.connect(seeded) as conn:
        _insert_doc(
            conn,
            tenant_id=str(_TENANT_A),
            doc_id="doc-never-embedded",
            title="Warranty terms",
            body="Coverage extends for one year from the date of purchase.",
            embedding=None,
            effective_from=datetime.now(UTC) - timedelta(days=1),
        )

    semantic = PgSemanticKnowledgeBase(lambda: psycopg.connect(seeded), _fake_embedder)
    hits = asyncio.run(semantic.search(_policy(_TENANT_A), "warranty terms"))

    assert "Warranty terms" not in [hit.title for hit in hits], (
        "a document with no embedding was returned by a SEMANTIC search - it must be "
        "silently excluded (embedding IS NOT NULL), not scored"
    )
