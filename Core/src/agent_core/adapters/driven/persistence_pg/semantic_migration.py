"""Migration 0021 - the OPT-IN pgvector schema for `SEMANTIC` retrieval.

Phase:   D2 - Multi-tenancy / SEMANTIC retrieval
Tasks:   docs/TASKS.md#t-d2-05
Status:  DONE - opt-in embedding column + HNSW index, applied by whoever enables SEMANTIC
Tests:   Core/tests/integration/test_semantic_retrieval.py

THIS MODULE DEFINES NO MODULE-LEVEL `Migration`, AND THAT IS THE WHOLE DESIGN
    `migrations.discover_app_migrations()` pkgutil-imports every module of this package and
    collects every `Migration` it finds at a module's top level, bare or inside a sequence.
    Production then applies that entire set, in id order, against whatever database it was
    pointed at. Discovery is forget-proof precisely because it has no opt-out - which makes
    the set it returns a promise: every migration in it can succeed on every deployment
    this system supports.

    `CREATE EXTENSION vector` cannot keep that promise. pgvector is a compiled C extension:
    the server either ships `vector.control` or it does not, and the local PostgreSQL 17
    cluster this suite runs against does not (docs/FIELD-NOTES.md, "The local cluster has no
    pgvector. The remote one does."). A previous version of this file defined its migration
    at module scope, so discovery picked it up, and one impossible statement stopped the
    whole startup path - not SEMANTIC, STARTUP, for every other anchor's tests too. A
    capability belongs to a NAMED database's server; a migration set that assumes one is a
    migration set that cannot be deployed.

    So the `Migration` is built by `semantic_migration()`, a factory, and exists only once
    somebody calls it. Discovery walks module attributes and finds nothing, which is the
    accurate answer: this schema is not part of what every deployment gets.

    F8 says SEMANTIC is "one config line per collection plus pgvector in the existing
    instance". Opt-in per collection means the SCHEMA is opt-in too. `apply_semantic_migration`
    is that opt-in, called by whoever turns SEMANTIC on for a collection, and it is the
    single place the pgvector requirement is paid for.

    (The factory also keeps this module honest with
    `test_startup_migrations.py::test_every_migration_defined_in_the_tree_is_discovered`,
    which scans source TEXT for a migration constructed with a literal id and demands
    discovery reach every one it finds - hence `MIGRATION_ID` below, a named constant. That
    guard is right for every schema that ships by default; this one does not ship by
    default, and `test_semantic_retrieval.py` asserts the opposite property - that discovery
    must NOT return it - so the invariant is pinned from both sides rather than dodged on
    one.)

FAILING AT THE POINT OF ENABLING, LOUDLY, IS THE POINT
    The alternative to a discovered migration is not "skip it quietly". A SEMANTIC
    collection whose column was never created returns nothing, forever, and an empty
    retrieval is a VALID answer everywhere else in this system - `KnowledgePolicy.min_score`
    exists to produce one. Silent degradation here is indistinguishable from an empty
    corpus and nothing reports it.

    `apply_semantic_migration` therefore checks `pg_available_extensions` FIRST and raises
    `PgVectorUnavailableError` naming the server, rather than letting the operator read
    "could not open extension control file" and conclude the schema is buggy. And it records
    its id only AFTER the SQL succeeded, so a failed enable can be retried once pgvector is
    installed - an id written for a migration that did not run is a column that never
    appears again.

WHY THE COLUMN IS NULLABLE, AND WHAT THAT MEANS FOR A ROW WRITTEN TODAY
    `ALTER TABLE ... ADD COLUMN` cannot backfill a value that does not exist - there is no
    embedding to compute here, and migrations do not call models. Every row `knowledge_docs`
    already holds gets `embedding = NULL`, and so does every row a write path inserts
    without computing one.

    `semantic.py`'s query filters `embedding IS NOT NULL`, so a NULL row is not an error and
    not a zero-score hit: it is invisible to every SEMANTIC search until something embeds
    it. That is the tenant-predicate danger shape mirrored - there a missing filter returns
    MORE rows than it should, here a missing value returns FEWER - and it is reported in
    `semantic.py`'s module docstring rather than papered over with a NOT NULL default that
    would make every insert fail until an embedding pipeline exists. Which model produces
    that embedding has no answer yet: no port exposes one. See `semantic.py`.

WHY HNSW OVER IVFFLAT
    IVFFlat's lists are k-means centroids computed at `CREATE INDEX` time, so it must be
    trained on existing data and degrades until rebuilt as the corpus grows. The column
    starts EMPTY on every deployment the day this runs, so there is nothing to train on.
    HNSW builds incrementally and needs no training step. `vector_cosine_ops` matches the
    `<=>` operator `semantic.py` queries with; an index built with a different operator
    class is silently never used, the same class of bug the full-text index warns about.

EMBEDDING_DIM IS A CONFIGURATION CHOICE, NOT A FACT PROVEN AGAINST A MODEL
    1536 is the OpenAI `text-embedding-3-small` family's size, the most common for anything
    reached through an OpenAI-compatible endpoint - which is how every provider in
    docs/FIELD-NOTES.md is reached. No embedding model has been verified against this
    schema, so it is a starting default. pgvector fixes the dimension per column: changing
    it later is a migration that rewrites the column, not a config edit, which is why
    `semantic_migration()` takes it as an argument rather than burying it in a string.

FORWARD-ONLY AND NON-DESTRUCTIVE
    Adds an extension, a nullable column and an index. Drops nothing, rewrites no row.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

EMBEDDING_DIM = 1536
"""pgvector fixes this per column. See the module docstring for why 1536 and why it is a
starting default rather than a fact proven against a verified embedding model."""

MIGRATION_ID = "0021_knowledge_docs_embedding"
"""Pre-allocated in docs/TASKS.md and spent the moment it was written there.

Migrations are append-only: `schema_migrations` records this id on every database that ran
it, and the applier returns early on that row, so editing this migration in place after it
has run anywhere does nothing at all. A change to the shape below is a NEW id.
"""

_AVAILABLE_SQL = "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"


class PgVectorUnavailableError(RuntimeError):
    """SEMANTIC was enabled against a server that cannot provide pgvector.

    Raised at the point of ENABLING rather than discovered later as a retrieval that
    returns nothing. pgvector is a compiled C extension - it cannot be installed from
    inside a session, so this is an operations fact about the server, not a repairable
    error, and the message says which one so nobody debugs the schema instead.
    """


def semantic_migration(*, dimensions: int = EMBEDDING_DIM) -> Migration:
    """Build the opt-in SEMANTIC migration. NOT a module-level object, deliberately.

    Discovery collects module-level `Migration` objects and production applies all of them;
    this one requires pgvector and therefore must never be in that set. See the module
    docstring - a previous version of this file learned it the expensive way.
    """
    return Migration(
        id=MIGRATION_ID,
        sql=f"""
        CREATE EXTENSION IF NOT EXISTS vector;
        ALTER TABLE knowledge_docs ADD COLUMN IF NOT EXISTS embedding vector({dimensions});
        CREATE INDEX IF NOT EXISTS ix_knowledge_docs_embedding_hnsw
            ON knowledge_docs USING hnsw (embedding vector_cosine_ops);
        """,
    )


def _pgvector_available_sync(conninfo: str) -> bool:
    with psycopg.connect(conninfo) as conn:
        return conn.execute(_AVAILABLE_SQL).fetchone() is not None


async def pgvector_available(conninfo: str) -> bool:
    """Whether this database's SERVER could install pgvector at all.

    `pg_available_extensions`, not `pg_extension`: the question is what the server ships,
    not what this database already created. Offered as a public read so a composition root
    can decide whether SEMANTIC is offerable BEFORE promising it to a collection.
    """
    return await asyncio.to_thread(_pgvector_available_sync, conninfo)


def _apply_semantic_migration_sync(app_conninfo: str, dimensions: int) -> None:
    migration = semantic_migration(dimensions=dimensions)
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (migration.id,)
        ).fetchone()
        if applied is not None:
            return
        if conn.execute(_AVAILABLE_SQL).fetchone() is None:
            raise PgVectorUnavailableError(
                "SEMANTIC retrieval was enabled, but the 'vector' extension is not "
                f"available on the server behind this database ({conn.info.host}:"
                f"{conn.info.port}/{conn.info.dbname}). pgvector is a compiled C extension "
                "and cannot be installed from a session: install it on that server, or "
                "leave the collection on FULL_TEXT. Nothing was applied and migration "
                f"{migration.id} was not recorded, so this can be retried once it is there."
            )
        conn.execute(migration.sql)
        # Recorded only after the SQL succeeded. An id written for a migration that did not
        # run makes the applier skip it forever, and the column would never appear.
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (migration.id,))


async def apply_semantic_migration(
    app_conninfo: str, *, dimensions: int = EMBEDDING_DIM
) -> None:
    """Enable the SEMANTIC schema on one database. The opt-in, called by whoever enables it.

    NOT called by `migrations.apply_all_migrations` and not reachable from discovery: a
    deployment that never turns SEMANTIC on must start cleanly on a server with no pgvector,
    which is the majority case and was the regression that broke the build.

    Runs after `knowledge_migration.apply_knowledge_migration` - it ALTERs the table `0013`
    creates and reads the same `schema_migrations` tracking table, both of which
    `apply_all_migrations` has already put in place on any started system.

    Idempotent by that tracking and by `IF NOT EXISTS` on the extension, column and index
    alike. Raises `PgVectorUnavailableError`, having changed nothing, when the server cannot
    provide pgvector.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_semantic_migration_sync, app_conninfo, dimensions)
