"""Driven adapter: KnowledgeAdmin over Postgres. The WRITE half of the corpus.

Phase:   F8 - Knowledge: Skills and FULL_TEXT
Tasks:   docs/TASKS.md#t-f8-06
Status:  DONE - upsert, delete and list_docs against migration 0013's table
Implements: ports/knowledge_admin.py
Tests:   Core/tests/integration/test_admin_routes.py

THIS FILE HAD NO ANCHOR, AND docs/STATE.md SAYS SO
    `docs/ARCHITECTURE.md` states that `knowledge_pg/` implements both knowledge ports, and
    `t-f8-04` wrote only the read half. `docs/STATE.md` records the gap under "open, unowned"
    rather than letting it be discovered by whoever wired the admin routes and found the
    port had no implementation. It is written here because `t-f8-06` is the anchor that
    needs it, and this module is in that anchor's writes set for that reason.

THE OBJECT AN AGENT HOLDS IS `PgKnowledgeBase`, AND IT IS NEVER THIS ONE
    Non-negotiable #8: there is no knowledge-write tool, ever. The defence is structural -
    a prompt injection cannot call a method that does not exist on the object the agent
    holds - so this class must never be injected into a runner, a toolset, a context
    engine, or anything else on the turn path. Its one caller is
    `adapters/driving/http/admin_routes.py`, which authenticates an administrator before
    it exists at all.

EVERY STATEMENT CARRIES THE TENANT, AND IT COMES FROM `scope.tenant_id`
    Same rule as `repository.py`, and here it is stronger: a misdirected read shows one
    business another's prices, a misdirected write CHANGES them. The tenant has exactly one
    source - the scope, built where the admin credential was verified - and there is no
    parameter, body field, or column that could supply a second one to disagree with it.

    Document ids are guessable and they are NOT unique across tenants: this module's own
    test seeds the same `doc_id` in two tenants deliberately, so that a missing predicate
    cannot hide behind an id that happened not to collide.

UPDATES ARE VERSIONS, NEVER OVERWRITES - AND HOW THAT FITS A (tenant_id, doc_id) KEY
    Migration 0013 keys the table on `(tenant_id, doc_id)`, so two versions cannot both sit
    under the stable id. The stable id therefore always names the CURRENT version, and the
    outgoing one is copied aside under `doc_id || '#v' || version` with `superseded_by` set
    to the stable id it was replaced by.

    That keeps three properties at once: `KnowledgeBase.get` on the stable id keeps
    working, retrieval ignores the archived row without a second predicate because
    `superseded_by IS NOT NULL`, and the before-value of every edit survives - which is
    what answers "why did the agent quote the old price on Tuesday?". A price change with
    no before-value is not an audit record, it is a note.

    A DELETE is the same mechanism with no successor: the live row is marked superseded by
    ITSELF, which reads as withdrawn. The row is kept. A hard delete destroys the record of
    what the agent used to tell customers, which is the one thing an audit needs.

D13: the port is async, psycopg is not. Every statement runs in `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from agent_core.adapters.driven.knowledge_pg.repository import ConnectionFactory
from agent_core.domain.knowledge import CollectionId, DocId, KnowledgeDoc
from agent_core.ports.embedder import Embedder
from agent_core.ports.knowledge_admin import KnowledgeAdmin, TenantAdminScope

__all__ = ["DocumentNotEmbeddedError", "EmbeddingKnowledgeAdmin", "PgKnowledgeAdmin"]

_DOC_COLUMNS = (
    "doc_id, collection, title, body, version, effective_from, "
    "superseded_by, updated_by, updated_at"
)

# `FOR UPDATE` because the read and the write below are one edit: two administrators
# saving the same document at the same moment must not both read version 3 and both write
# version 4, which would leave one edit silently discarded and two rows claiming the same
# version number.
_CURRENT_SQL = f"""
    SELECT {_DOC_COLUMNS}
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND doc_id = %s
      AND superseded_by IS NULL
    FOR UPDATE
"""

# The archive copy is built by SELECT rather than by re-sending the values the caller just
# read, so the row that is preserved is the row the database actually held. Re-sending them
# would archive what this process believed, which is the same thing right up until it is
# not. `ON CONFLICT DO NOTHING` makes a retried write idempotent instead of a crash.
_ARCHIVE_SQL = """
    INSERT INTO knowledge_docs
        (tenant_id, doc_id, collection, title, body, version,
         effective_from, superseded_by, updated_by, updated_at)
    SELECT tenant_id, doc_id || '#v' || version, collection, title, body, version,
           effective_from, doc_id, updated_by, updated_at
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND doc_id = %s
    ON CONFLICT (tenant_id, doc_id) DO NOTHING
"""

_INSERT_SQL = """
    INSERT INTO knowledge_docs
        (tenant_id, doc_id, collection, title, body, version,
         effective_from, superseded_by, updated_by, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, now())
"""

_UPDATE_SQL = """
    UPDATE knowledge_docs
    SET collection = %s,
        title = %s,
        body = %s,
        version = %s,
        effective_from = %s,
        superseded_by = NULL,
        updated_by = %s,
        updated_at = now()
    WHERE tenant_id = %s
      AND doc_id = %s
"""

# Superseded by ITSELF: withdrawn, with no successor to point at. The row stays.
_WITHDRAW_SQL = """
    UPDATE knowledge_docs
    SET superseded_by = doc_id,
        updated_by = %s,
        updated_at = now()
    WHERE tenant_id = %s
      AND doc_id = %s
      AND superseded_by IS NULL
"""

# `%s::boolean` rather than a bare `%s`: Postgres cannot infer the type of a parameter
# standing alone in a boolean expression, and the error it raises for it is about the
# operator, not about the parameter.
_LIST_SQL = f"""
    SELECT {_DOC_COLUMNS}
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND collection = %s
      AND (superseded_by IS NULL OR %s::boolean)
    ORDER BY doc_id ASC, version DESC
"""


# Only the CURRENT version. The archive copy keeps the vector of the body it was made
# from, which is the honest thing for it to hold; `_ARCHIVE_SQL` does not carry `embedding`
# forward, so an archived row is NULL there and is excluded from retrieval anyway by
# `superseded_by IS NOT NULL` - and from `semantic._UNEMBEDDED_COUNT_SQL` for the same
# reason, so the backlog number counts documents an agent could actually have retrieved.
_EMBED_SQL = """
    UPDATE knowledge_docs
    SET embedding = %s::vector
    WHERE tenant_id = %s
      AND doc_id = %s
      AND superseded_by IS NULL
"""


class DocumentNotEmbeddedError(RuntimeError):
    """The document was STORED and could not be EMBEDDED. Both halves are true at once.

    Never downgraded to a log line, and never swallowed so the write can report success.
    An administrator told "saved" about a row that no SEMANTIC query can ever return has
    been told something false in the only way that matters - and the row looks identical,
    from the admin UI, to one that works.
    """


class PgKnowledgeAdmin:
    """`KnowledgeAdmin` over Postgres. Never injected into anything an agent touches.

    Takes the same `ConnectionFactory` seam as `PgKnowledgeBase`, so the admin surface and
    the retrieval surface talk to one database through one kind of handle - and a test can
    record either one's statements without either class knowing.
    """

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    async def upsert(
        self,
        scope: TenantAdminScope,
        doc: KnowledgeDoc,
        *,
        effective_from: datetime | None = None,
    ) -> KnowledgeDoc:
        """Store `doc` as the current version for `scope.tenant_id`, archiving the one it
        replaces.

        `effective_from` in the future is a SCHEDULED change - new hours starting Monday.
        It needs no scheduler because retrieval filters on it, which is a promise
        `Core/tests/integration/test_knowledge_repository.py` already keeps from the read
        side and this anchor's suite keeps from the write side.
        """
        _authorise(scope, doc.collection)
        row = await asyncio.to_thread(
            self._upsert_sync, scope, doc, effective_from or doc.effective_from
        )
        return _to_doc(row)

    async def delete(self, scope: TenantAdminScope, doc_id: DocId) -> None:
        """Soft delete. A `DocId` from another tenant matches nothing, because
        `scope.tenant_id` is in the WHERE clause rather than in a check somebody
        remembered afterwards.

        Authorisation reads the row's collection from the database instead of taking it
        from the caller: a delete names only an id, and an id the caller supplies cannot
        also be allowed to declare which corpus it belongs to. A document this
        administrator may not touch is reported exactly like a document that does not
        exist - saying which would confirm it exists, and that is itself a leak.
        """
        await asyncio.to_thread(self._delete_sync, scope, doc_id)

    async def list_docs(
        self,
        scope: TenantAdminScope,
        collection: CollectionId,
        *,
        include_superseded: bool = False,
    ) -> tuple[KnowledgeDoc, ...]:
        """For the admin UI; `include_superseded=True` renders the change history.

        `(scope.tenant_id, collection)` identifies the corpus. The collection name alone
        does not - `pricing` exists in every tenant - so a listing narrowed by name only
        would be a directory of other people's documents.
        """
        _authorise(scope, collection)
        rows = await asyncio.to_thread(
            self._list_sync, scope, collection, include_superseded
        )
        return tuple(_to_doc(row) for row in rows)

    def _upsert_sync(
        self, scope: TenantAdminScope, doc: KnowledgeDoc, effective_from: datetime | None
    ) -> tuple[Any, ...]:
        tenant = str(scope.tenant_id)
        doc_id = str(doc.doc_id)
        updated_by = str(scope.admin.subject_id)
        with self._connect() as connection:
            current = connection.execute(_CURRENT_SQL, (tenant, doc_id)).fetchone()
            if current is None:
                connection.execute(
                    _INSERT_SQL,
                    (
                        tenant,
                        doc_id,
                        str(doc.collection),
                        doc.title,
                        doc.body,
                        1,
                        effective_from or _now(),
                        updated_by,
                    ),
                )
            else:
                connection.execute(_ARCHIVE_SQL, (tenant, doc_id))
                connection.execute(
                    _UPDATE_SQL,
                    (
                        str(doc.collection),
                        doc.title,
                        doc.body,
                        int(current[4]) + 1,
                        effective_from or current[5],
                        updated_by,
                        tenant,
                        doc_id,
                    ),
                )
            stored = connection.execute(_CURRENT_SQL, (tenant, doc_id)).fetchone()
        if stored is None:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError(f"the document {doc_id!r} vanished during its own upsert")
        return tuple(stored)

    def _delete_sync(self, scope: TenantAdminScope, doc_id: DocId) -> None:
        tenant = str(scope.tenant_id)
        with self._connect() as connection:
            current = connection.execute(_CURRENT_SQL, (tenant, str(doc_id))).fetchone()
            if current is None:
                return
            _authorise(scope, CollectionId(current[1]))
            connection.execute(
                _WITHDRAW_SQL, (str(scope.admin.subject_id), tenant, str(doc_id))
            )

    def _list_sync(
        self, scope: TenantAdminScope, collection: CollectionId, include_superseded: bool
    ) -> Sequence[tuple[Any, ...]]:
        with self._connect() as connection:
            rows = connection.execute(
                _LIST_SQL, (str(scope.tenant_id), str(collection), include_superseded)
            ).fetchall()
        return [tuple(row) for row in rows]


class EmbeddingKnowledgeAdmin:
    """`KnowledgeAdmin` that embeds what it just stored. For SEMANTIC collections only.

    WHAT IT CLOSES
        `ports/knowledge_admin.py` prescribes, as step 5 of `upsert`, "if the collection is
        SEMANTIC, re-embed ONLY the changed chunks" - and until `ports/embedder.py` existed
        there was nothing to call. So every document written to a SEMANTIC collection was
        stored with a NULL embedding, `semantic._SEMANTIC_SEARCH_SQL` filtered it out, and
        nothing anywhere said so.

    A DECORATOR, NOT A WIDENING, AND NOT A SECOND WRITE SURFACE
        It implements `KnowledgeAdmin` and delegates to one. No method is added, so the
        corpus still has exactly one write surface and non-negotiable #8 is untouched:
        this object is reachable only from the admin route, never from a runner, a toolset
        or a context engine. An `Embedder` returns numbers and cannot write anything; the
        write is this class's, under the tenant predicate, exactly like every other
        statement in this module.

        Composed per collection mode, the same way `PgSemanticKnowledgeBase` is chosen for
        the read side: a FULL_TEXT deployment wires the plain `PgKnowledgeAdmin` and pays
        for no model call at all.

    WHAT HAPPENS WHEN THE MODEL CALL FAILS MID-UPSERT
        The document is already stored by then. That cannot be undone here honestly - the
        inner adapter has committed a version bump and an archive row, and a compensating
        delete would destroy the audit history this module exists to keep.

        So the failure is RAISED, naming the document, and the row stays as it is: stored,
        unembedded, invisible to SEMANTIC retrieval, and COUNTED by
        `semantic._UNEMBEDDED_COUNT_SQL` on the next search. Two independent reports of the
        same fact, one at write time to the administrator and one at read time to the
        operator, because the write-time one can be dismissed and the read-time one cannot.

        `embedding` is also an OPT-IN column (`persistence_pg/semantic_migration.py`), so a
        deployment can reach this class with no column to write to. That surfaces the same
        way rather than as a raw psycopg error two layers up: the administrator's document
        is stored and unembedded either way, and the remedy differs only in which thing
        somebody has to fix.

    D13: the port is async, psycopg is not. The vector write runs in `asyncio.to_thread`.
    """

    def __init__(
        self, inner: KnowledgeAdmin, embedder: Embedder, connect: ConnectionFactory
    ) -> None:
        self._inner = inner
        self._embedder = embedder
        self._connect = connect

    async def upsert(
        self,
        scope: TenantAdminScope,
        doc: KnowledgeDoc,
        *,
        effective_from: datetime | None = None,
    ) -> KnowledgeDoc:
        """Store, then embed. In that order, and never the reverse.

        Embedding first would spend a model call on a write the inner adapter may refuse -
        `_authorise` rejects a collection this administrator does not hold - and paying a
        provider to reject a request is a way to turn a permission error into a bill.

        The body that is embedded is the STORED one, read back from `inner.upsert`'s
        return, not the one the caller passed: they can differ, and a vector built from
        text the database does not hold is a ranking nobody can reproduce.
        """
        stored = await self._inner.upsert(scope, doc, effective_from=effective_from)
        try:
            embedding = await self._embedder.embed(stored.body)
        except Exception as exc:
            # Broad on purpose, and it is a WRAP, never a swallow: `ModelGateway.
            # classify_error` owns which recovery a provider failure earns, and this layer
            # owns only the fact that the document is now stored and unretrievable. The
            # original is chained, so nothing about the provider failure is lost.
            raise DocumentNotEmbeddedError(
                f"the document {stored.doc_id!r} (version {stored.version}) in collection "
                f"{stored.collection!r} was STORED, and the embedding model call FAILED. "
                "It cannot match any SEMANTIC query until it is re-embedded, and nothing "
                "about it looks wrong in the admin UI. Re-run the upsert once the model is "
                f"reachable. ({exc})"
            ) from exc
        try:
            await asyncio.to_thread(self._embed_sync, scope, stored, embedding.vector)
        except Exception as exc:
            raise DocumentNotEmbeddedError(
                f"the document {stored.doc_id!r} (version {stored.version}) was STORED, its "
                f"embedding was produced by {embedding.model!r}, and WRITING that embedding "
                "failed. If this database has no `embedding` column, migration 0021 is "
                "opt-in and was never applied here - apply it, or leave the collection on "
                f"FULL_TEXT. Until then the document matches no SEMANTIC query. ({exc})"
            ) from exc
        return stored

    async def delete(self, scope: TenantAdminScope, doc_id: DocId) -> None:
        """Delegated. A withdrawn document keeps its vector and its row: retrieval already
        ignores it through `superseded_by`, and clearing the embedding would only make a
        restored version look unembedded."""
        await self._inner.delete(scope, doc_id)

    async def list_docs(
        self,
        scope: TenantAdminScope,
        collection: CollectionId,
        *,
        include_superseded: bool = False,
    ) -> tuple[KnowledgeDoc, ...]:
        """Delegated. Listing reads no vector and must not pay for a model call."""
        return await self._inner.list_docs(
            scope, collection, include_superseded=include_superseded
        )

    def _embed_sync(
        self, scope: TenantAdminScope, doc: KnowledgeDoc, vector: Sequence[float]
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                _EMBED_SQL,
                (_vector_literal(vector), str(scope.tenant_id), str(doc.doc_id)),
            )


def _vector_literal(values: Sequence[float]) -> str:
    """pgvector's text input form, bound as a parameter and cast with `%s::vector`.

    Duplicated from `semantic.py` rather than imported for the reason that module gives for
    not importing a pgvector client package at all: this one is the WRITE side, and the two
    must be able to move independently the day a dimension or a distance changes. It is
    four lines and it has one behaviour.
    """
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def _authorise(scope: TenantAdminScope, collection: CollectionId) -> None:
    """`collections` scopes which corpora this administrator may edit - a franchise manager
    edits their own branch's hours, not head office's price list.

    `is_superuser` means every collection IN THIS TENANT and has never meant every tenant:
    the scope's `tenant_id` is not defaulted and not optional, so it narrows a superuser
    exactly like anybody else, and this function never sees a request that did not name a
    tenant.
    """
    if scope.admin.is_superuser or collection in scope.admin.collections:
        return
    raise PermissionError(
        f"this administrator is not granted the {collection!r} collection"
    )


def _now() -> datetime:
    """A document with no `effective_from` is in force immediately.

    Taken from the database rather than from this process would be tidier, but the column
    is filled by an INSERT that also carries other values, and `now()` inside that INSERT
    would make the two-branch code below read differently for no gain. This is an adapter,
    not a DBOS workflow body - non-negotiable #2 is about step determinism on replay, and
    nothing here is replayed.
    """
    return datetime.now(tz=UTC)


def _to_doc(row: tuple[Any, ...]) -> KnowledgeDoc:
    doc_id, collection, title, body, version, effective_from, superseded_by, by, at = row
    return KnowledgeDoc(
        doc_id=DocId(doc_id),
        collection=CollectionId(collection),
        title=title,
        body=body,
        version=int(version),
        effective_from=_as_datetime(effective_from),
        superseded_by=None if superseded_by is None else DocId(superseded_by),
        updated_by=by,
        updated_at=_as_datetime(at),
    )


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None
