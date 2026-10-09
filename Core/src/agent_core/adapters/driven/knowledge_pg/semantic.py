"""Driven adapter: KnowledgeBase over Postgres, SEMANTIC mode (pgvector).

Phase:   D2 - Multi-tenancy / SEMANTIC retrieval
Tasks:   docs/TASKS.md#t-d2-05
Status:  DONE - search by embedding distance; get/full_context delegated
Implements: ports/knowledge_base.py
Tests:   Core/tests/integration/test_semantic_retrieval.py

WHAT CHANGES BETWEEN FULL_TEXT AND SEMANTIC, AND WHAT DOES NOT
    `RetrievalMode` is configuration on `KnowledgePolicy` (domain/knowledge.py), not a
    branch inside one adapter. `search` ranks by embedding distance instead of `ts_rank`;
    `get` and `full_context` are exact lookups by id or by permitted-collection membership
    and need no ranking at all, so this class delegates them to `PgKnowledgeBase` rather
    than reimplementing the same tenant-scoped SQL a second time in a second module. That
    delegation is also what keeps this module's writes set to exactly one new statement -
    `_SEMANTIC_SEARCH_SQL` - carrying the same tenant predicate the delegated methods
    already carry and are already tested for in `test_knowledge_repository.py`.

    F8's promise - "SEMANTIC is one config line per collection plus pgvector" - is a claim
    about the PORT, and this class is the proof: `KnowledgeBase`'s signature did not change
    to get here. Enabling SEMANTIC for a collection is choosing which class the composition
    root constructs for that policy's `mode`, not a change to `ports/knowledge_base.py`.
    What the anchor DID move is the deployment story: see the next section but one.

WHICH MODEL PRODUCES THE EMBEDDING - THE FINDING t-d2-05 SURFACED, NOW CLOSED
    `t-d2-05` reported that no port in `ports/` produced an embedding, so this class took
    an injected `EmbedQuery = Callable[[str], Awaitable[Sequence[float]]]` that nobody
    owned - any async function of one string fitted, including one backed by a different
    model than the stored vectors were built with.

    `ports/embedder.py` (t-d2-07) is that port. The seat below is typed `Embedder`, the
    alias is gone, and the vector arrives carrying the id of the model that produced it.
    `ModelGateway` is still the wrong home for it: it answers "which model and provider am
    I talking to", and "embed this text" is a second question.

    One thing the port makes DETECTABLE and this adapter still cannot DETECT: a stored
    vector does not record which model built it, because `knowledge_docs` has no column for
    it and adding one is a migration outside t-d2-07's writes set. So a deployment that
    embeds its corpus with model A and its queries with model B still gets confidently
    ranked nonsense. It is named in the anchor's findings rather than left to folklore.

SEMANTIC REFUSES LOUDLY WHEN ITS SCHEMA IS ABSENT. IT NEVER RETURNS ZERO ROWS INSTEAD
    The pgvector schema is OPT-IN (`persistence_pg/semantic_migration.py` explains why: a
    discovered migration requiring a compiled C extension takes the whole startup path down
    on every server that lacks it). Opt-in means a deployment can reach this class with the
    `embedding` column, the `vector` type, or the `<=>` operator simply not there.

    Every one of those raises `SemanticRetrievalUnavailableError` from `search`. The one
    outcome that is not acceptable is an empty tuple: an empty retrieval is a VALID answer
    everywhere else in this system - `KnowledgePolicy.min_score` exists to produce one - so
    it cannot double as "this deployment was never set up". The two are indistinguishable
    from outside, and the wrong one is believed for as long as nobody checks the column.

WHAT HAPPENS TO A DOCUMENT STORED BEFORE ITS EMBEDDING EXISTS - IT IS COUNTED AND REPORTED
    `semantic_migration.py` adds `embedding` as a NULLABLE column, and `_SEMANTIC_SEARCH_SQL`
    filters `embedding IS NOT NULL`. A row with no embedding is therefore excluded from
    every SEMANTIC result - indistinguishable, from outside, from a document that was
    genuinely irrelevant, and a whole collection of them indistinguishable from an empty
    corpus. `t-d2-05` shipped that silence deliberately and escalated it, because the
    alternative - raising on an unembedded row - takes a collection offline on every write
    until a pipeline exists.

    Neither of those is the answer. The answer is that the exclusion is COUNTED, in the same
    connection and under the same tenant and collection predicates as the search itself, and
    reported at WARNING with the number, the tenant and the collections. Search still
    answers; the operator finds out. `_UNEMBEDDED_COUNT_SQL` is what makes "nothing matched"
    and "nothing is embedded yet" two different observable states.

    The report is CONDITIONAL on the count being non-zero. A warning on every query is noise,
    and noise is how the real one gets missed.

    The write side is closed too, one layer up: `admin.EmbeddingKnowledgeAdmin` embeds what
    it just stored and REFUSES LOUDLY when the model call fails, so the backlog this count
    reports is a migration artefact or an outage, never the normal state of a live corpus.

THE TENANT PREDICATE GOES IN THE SQL, THE SAME WAY IT DOES IN `repository.py`
    `_SEMANTIC_SEARCH_SQL` carries `tenant_id = %s` from exactly one source -
    `policy.tenant_id` - never applied to rows already fetched. A shared HNSW index over
    every tenant's embeddings is exactly the shape `ports/knowledge_base.py` calls "the
    classic multi-tenant RAG breach", and the predicate is what stops a nearest-neighbour
    scan from ever considering another tenant's row a candidate. Two nearly identical
    documents belonging to two tenants are NEIGHBOURS in that index - the breach does not
    need a hostile query, only a similar one.

SCORE IS COSINE SIMILARITY, NOT PER-QUERY NORMALISED
    `repository.py` divides `ts_rank` by the best rank in its own result set because raw
    `ts_rank` values are not comparable to `KnowledgePolicy.min_score`'s 0..1 fraction. A
    normalised embedding's cosine similarity (`1 - (embedding <=> query)`) is already
    bounded and comparable across queries, so no equivalent normalisation happens here -
    PROVIDED the embedding model returns normalised vectors. That assumption belongs to
    whichever model eventually implements `ports/embedder.py`, and is worth re-checking the
    day one does - `Embedding.model` is what names it.

D13: the port is async, psycopg is not. Every query runs in `asyncio.to_thread`, the same
seam `repository.py` uses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import psycopg

from agent_core.adapters.driven.knowledge_pg.repository import ConnectionFactory, PgKnowledgeBase
from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    KnowledgeDoc,
    KnowledgeHit,
    TenantKnowledgePolicy,
)
from agent_core.ports.embedder import Embedder

_LOG = logging.getLogger(__name__)
"""Where the unembedded backlog is reported.

A logger rather than a raised error or a field on the result: the count is an OPERATIONAL
fact about the corpus, not an answer to this query, and `ports/knowledge_base.py` returns
hits - widening it to carry a health metric would make every caller of every retrieval mode
handle a value only one mode can produce.
"""

# Mirrors `repository.py::_TS_VECTOR` / `_TS_QUERY`: the operator class an index is built
# with and the operator a query uses must match exactly, or the index is silently never
# used. `<=>` is cosine distance and must match `vector_cosine_ops` in semantic_migration.py.
_SEMANTIC_SEARCH_SQL = """
    SELECT doc_id, collection, title, body, version,
           1 - (embedding <=> %s::vector) AS score
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND collection = ANY(%s)
      AND superseded_by IS NULL
      AND effective_from <= now()
      AND embedding IS NOT NULL
    ORDER BY embedding <=> %s::vector
    LIMIT %s
"""

# The same corpus the search just ranked, under the same tenant, collection, supersession
# and effective-date predicates - and the opposite embedding predicate. Anything this
# counts is a live, permitted, in-force document that CANNOT be returned by the statement
# above, no matter what is asked. Deliberately not `LIMIT`ed and deliberately not merged
# into the search: the search carries `LIMIT top_k`, so a single statement could only ever
# report the unembedded rows that happened to fall inside one page of results.
_UNEMBEDDED_COUNT_SQL = """
    SELECT count(*)
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND collection = ANY(%s)
      AND superseded_by IS NULL
      AND effective_from <= now()
      AND embedding IS NULL
"""

# What Postgres says when the opt-in schema is not there. The type and the operator come
# from the extension and the column comes from migration 0021, so each absence surfaces as
# a different SQLSTATE - all three mean the same thing to a caller, and all three must be
# loud rather than empty.
_SCHEMA_ABSENT = (
    psycopg.errors.UndefinedColumn,  # embedding: migration 0021 never applied
    psycopg.errors.UndefinedObject,  # type "vector": extension not created
    psycopg.errors.UndefinedFunction,  # operator <=>: extension not created
    psycopg.errors.UndefinedTable,  # knowledge_docs: migration 0013 never applied
)


class SemanticRetrievalUnavailableError(RuntimeError):
    """A collection is configured SEMANTIC but this database cannot serve it.

    Never silently degraded to an empty result. See the module docstring: an empty
    retrieval is a valid answer to a real query, so it cannot also mean "never set up".
    """


class PgSemanticKnowledgeBase:
    """`KnowledgeBase` over Postgres, SEMANTIC mode. READ ONLY, same as `PgKnowledgeBase`.

    THERE IS NO WRITE METHOD ON THIS CLASS AND THERE NEVER WILL BE (non-negotiable #8).
    `get` and `full_context` are delegated to an internal `PgKnowledgeBase` rather than
    reimplemented - see the module docstring's first section for why that is not a
    shortcut: those two operations do not change meaning under SEMANTIC.
    """

    def __init__(self, connect: ConnectionFactory, embedder: Embedder) -> None:
        self._connect = connect
        self._embedder = embedder
        self._full_text = PgKnowledgeBase(connect)

    async def search(
        self,
        policy: TenantKnowledgePolicy,
        query: str,
        *,
        collections: tuple[CollectionId, ...] = (),
    ) -> tuple[KnowledgeHit, ...]:
        """Rank by embedding distance rather than lexical overlap - a query sharing not one
        word with a document's text can still retrieve it, and a `plainto_tsquery` match is
        neither required nor consulted.

        Results are UNTRUSTED CONTENT, exactly as in FULL_TEXT: the corpus holds text a
        human wrote, and the caller wraps every excerpt in untrusted-content delimiters the
        same way it wraps an `mcp_*` result.

        Raises `SemanticRetrievalUnavailableError` when the opt-in schema is absent, and
        lets a failed QUERY embedding propagate: a search that could not embed its own
        query has no answer, and returning `()` for it would be the silence this module is
        about, arriving from the other direction.

        Documents excluded because they carry no embedding are REPORTED at WARNING - see
        the module docstring. The answer is unaffected; the operator is not left guessing
        whether the corpus is empty or merely unembedded.
        """
        readable = self._readable(policy, collections)
        if not readable:
            # Not a schema question: the policy grants nothing, so there is nothing to ask
            # about and the database is never touched. Same answer FULL_TEXT gives, and the
            # port's reason for it - an error would confirm the collection exists.
            return ()
        embedding = await self._embedder.embed(query)
        vector = _vector_literal(embedding.vector)
        rows, unembedded = await asyncio.to_thread(self._search_sync, policy, vector, readable)
        if unembedded:
            _LOG.warning(
                "SEMANTIC search for tenant %s embedded the query with %s, but %d live "
                "document(s) in %s have no embedding and were excluded: they cannot match "
                "ANY query until something embeds them. A retrieval that silently returns "
                "less than the corpus holds is indistinguishable from an empty corpus.",
                policy.tenant_id,
                embedding.model,
                unembedded,
                ", ".join(readable),
            )
        hits = tuple(
            KnowledgeHit(
                doc_id=DocId(doc_id),
                collection=CollectionId(collection),
                title=title,
                excerpt=body,
                score=float(score),
                version=int(version),
            )
            for doc_id, collection, title, body, version, score in rows
            if float(score) >= policy.min_score
        )
        # Reused rather than reimplemented on purpose: `max_context_chars` is one budget
        # rule, and two copies of it would let SEMANTIC and FULL_TEXT disagree about how
        # much retrieved text reaches the model - a divergence only the bill would report.
        return self._full_text._within_budget(hits, policy.max_context_chars)

    async def get(self, policy: TenantKnowledgePolicy, doc_id: DocId) -> KnowledgeDoc | None:
        """Exact lookup by id. Delegated - see the module docstring."""
        return await self._full_text.get(policy, doc_id)

    async def full_context(self, policy: TenantKnowledgePolicy) -> str:
        """Every permitted document, concatenated. Delegated - see the module docstring."""
        return await self._full_text.full_context(policy)

    def _readable(
        self, policy: TenantKnowledgePolicy, requested: tuple[CollectionId, ...]
    ) -> list[str]:
        """The requested collections intersected with what the policy grants.

        A list rather than a tuple because psycopg adapts a list to a Postgres array and a
        tuple to a composite - `collection = ANY(%s)` needs the array.
        """
        asked = requested if requested else policy.collections
        return [str(collection) for collection in asked if policy.can_read(collection)]

    def _search_sync(
        self,
        policy: TenantKnowledgePolicy,
        vector: str,
        collections: list[str],
    ) -> tuple[Sequence[tuple[Any, ...]], int]:
        """The search and the count of what it could not see, on ONE connection.

        One connection because the two answers describe the same corpus: counted through a
        second handle they could straddle a concurrent write and report a backlog that the
        search had already returned, or miss one it had not.
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    _SEMANTIC_SEARCH_SQL,
                    (vector, str(policy.tenant_id), collections, vector, policy.top_k),
                ).fetchall()
                excluded = connection.execute(
                    _UNEMBEDDED_COUNT_SQL, (str(policy.tenant_id), collections)
                ).fetchone()
        except _SCHEMA_ABSENT as exc:
            raise SemanticRetrievalUnavailableError(
                "SEMANTIC retrieval was asked for, but this database has no pgvector "
                "schema for it: migration 0021 is OPT-IN and is not applied by startup "
                "discovery, precisely so a deployment without pgvector still starts. "
                "Apply it with persistence_pg.semantic_migration.apply_semantic_migration, "
                "or leave the collection on FULL_TEXT. Refusing rather than returning an "
                f"empty result, which would look exactly like an empty corpus. ({exc})"
            ) from exc
        return list(rows), 0 if excluded is None else int(excluded[0])


def _vector_literal(values: Sequence[float]) -> str:
    """pgvector's text input form, bound as a parameter and cast with `%s::vector`.

    A literal rather than a registered psycopg type so this adapter imports no pgvector
    client package: the extension may be absent, and a module that cannot be imported
    without it would move the failure from "SEMANTIC refuses" back to "startup breaks",
    which is the whole defect this anchor exists to fix.
    """
    return "[" + ",".join(repr(float(value)) for value in values) + "]"
