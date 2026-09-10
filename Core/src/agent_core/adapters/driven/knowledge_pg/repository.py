"""Driven adapter: KnowledgeBase over Postgres. FULL_TEXT only.

Phase:   F8 - Knowledge: Skills and FULL_TEXT
Tasks:   docs/TASKS.md#t-f8-04
Status:  DONE - search, get and full_context; migration 0013 lives in
         adapters/driven/persistence_pg/knowledge_migration.py
Implements: ports/knowledge_base.py
Tests:   Core/tests/integration/test_knowledge_repository.py

TABLE (migration 0013, adapters/driven/persistence_pg/knowledge_migration.py)
    knowledge_docs (tenant_id, doc_id) pk, collection, title, body, version,
                   effective_from, superseded_by, updated_by, updated_at

THERE IS NO WRITE METHOD ON THIS CLASS AND THERE NEVER WILL BE (non-negotiable #8)
    `KnowledgeBase` is the object the agent holds. A prompt injection cannot call a method
    that does not exist on it, and that is the entire defence against corpus poisoning -
    structural, not conventional, so nobody can forget to check a flag. Writes belong to
    `ports/knowledge_admin.py`, which takes a `TenantAdminScope` and is never injected into
    anything an agent touches.

EVERY STATEMENT IN THIS MODULE CARRIES `tenant_id = %s`, AND NOTHING FILTERS AFTERWARDS
    This is the rule `ports/knowledge_base.py` states and the reason `TenantKnowledgePolicy`
    exists: for as long as the contract was `(policy, query, collections)` the adapter had
    no tenant to filter on, so the port's own instruction named a value it never handed
    over. It hands one over now, from exactly one source - `policy.tenant_id` - and there
    is no second source to disagree with it.

    A post-retrieval filter would pass every test on a good day. It returns the same rows;
    it only diverges the day someone edits it, adds an early return above it, or writes a
    second read path and forgets it. And when it diverges the failure is silent, because a
    missing predicate returns MORE rows, not fewer - it looks like a better search engine.
    `Core/tests/integration/test_knowledge_repository.py` records the statements this
    module actually hands to psycopg and asserts the predicate is in every one of them.

    Collection membership is checked in Python BEFORE the query and then bound INTO it as
    `collection = ANY(%s)`. That is not the same compromise: the Python step only narrows
    what is asked for, and the database is still told. A policy granting nothing never
    reaches Postgres at all, which is also the port's answer to "an agent asking for a
    collection it may not read gets an EMPTY result, not an error" - an error would confirm
    the collection exists, and that is itself a leak.

SCORES ARE NORMALISED PER QUERY, BECAUSE `min_score` IS A FRACTION AND `ts_rank` IS NOT
    `KnowledgePolicy.min_score` defaults to 0.6 and `KnowledgeHit` says a score "is
    comparable only within a single query". Raw `ts_rank` values sit around 0.05-0.1 even
    for an excellent match, so comparing one against 0.6 would drop EVERY hit and the
    knowledge base would look empty rather than broken. Each row is divided by the best
    rank in its own result set, so the best hit scores 1.0 and `min_score` reads as
    "how close to the best match must a result be" - which is the only question a
    cross-query-incomparable score can answer.

D13: the port is async, psycopg is not. Every query runs in `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    KnowledgeDoc,
    KnowledgeHit,
    TenantKnowledgePolicy,
)

# A source of connections that belong to THIS repository, the same seam
# `persistence_pg/policy_repository.py` uses. `psycopg_pool.ConnectionPool.connection`
# satisfies it directly; so does `lambda: psycopg.connect(conninfo)`.
ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# The regconfig is a literal here and a literal in the index expression in
# knowledge_migration.py, and the two strings must stay identical. An expression index is
# only used when the query's expression matches it exactly; a mismatch is not an error, it
# is a sequential scan nobody is told about.
_TS_VECTOR = "to_tsvector('english', title || ' ' || body)"
_TS_QUERY = "plainto_tsquery('english', %s)"

# `rank / MAX(rank) OVER ()` is the per-query normalisation the module docstring explains.
# NULLIF guards the degenerate all-zero set: `@@` can match with a rank of 0 for a
# stop-word-only query, and dividing by it would raise inside the database.
_SEARCH_SQL = f"""
    WITH matched AS (
        SELECT doc_id, collection, title, body, version,
               ts_rank({_TS_VECTOR}, {_TS_QUERY}) AS rank
        FROM knowledge_docs
        WHERE tenant_id = %s
          AND collection = ANY(%s)
          AND superseded_by IS NULL
          AND effective_from <= now()
          AND {_TS_VECTOR} @@ {_TS_QUERY}
    )
    SELECT doc_id, collection, title, body, version,
           COALESCE(rank / NULLIF(MAX(rank) OVER (), 0), 0.0) AS score
    FROM matched
    ORDER BY score DESC, doc_id ASC
    LIMIT %s
"""

_GET_SQL = """
    SELECT doc_id, collection, title, body, version, effective_from,
           superseded_by, updated_by, updated_at
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND doc_id = %s
      AND collection = ANY(%s)
      AND superseded_by IS NULL
      AND effective_from <= now()
"""

# Ordered by (collection, doc_id) rather than by relevance or recency: this text is
# injected into the system prompt, and D8 wants the prompt prefix to be BYTE-IDENTICAL
# between turns or the provider cache is lost and the whole prompt is re-billed. A stable
# sort is what makes the same corpus render the same string twice.
_FULL_CONTEXT_SQL = """
    SELECT title, body
    FROM knowledge_docs
    WHERE tenant_id = %s
      AND collection = ANY(%s)
      AND superseded_by IS NULL
      AND effective_from <= now()
    ORDER BY collection ASC, doc_id ASC
"""


class PgKnowledgeBase:
    """`KnowledgeBase` over Postgres, FULL_TEXT mode. READ ONLY.

    SEMANTIC and HYBRID are deliberately absent: `docs/TASKS.md` puts them behind pgvector
    in `knowledge_pg/semantic.py` (t-d2-05), and the port keeps them in the signature so
    that switching is a config line per collection rather than a redesign. This class
    serves whole documents and has zero retrieval error on a small corpus.
    """

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    async def search(
        self,
        policy: TenantKnowledgePolicy,
        query: str,
        *,
        collections: tuple[CollectionId, ...] = (),
    ) -> tuple[KnowledgeHit, ...]:
        """Backs the `knowledge_search` tool. Results are UNTRUSTED CONTENT - the corpus
        holds text a human wrote, and the caller wraps every excerpt in
        untrusted-content delimiters exactly as it does an `mcp_*` result.
        """
        readable = self._readable(policy, collections)
        if not readable:
            return ()
        rows = await asyncio.to_thread(self._search_sync, policy, query, readable)
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
        return self._within_budget(hits, policy.max_context_chars)

    async def get(self, policy: TenantKnowledgePolicy, doc_id: DocId) -> KnowledgeDoc | None:
        """One current document, or None. Missing and forbidden are indistinguishable, and
        a `DocId` belonging to another tenant is exactly the missing case - because
        `policy.tenant_id` is in the WHERE clause, not because a check was remembered
        afterwards. Document ids are guessable; the predicate is what makes guessing one
        useless."""
        readable = self._readable(policy, ())
        if not readable:
            return None
        row = await asyncio.to_thread(self._get_sync, policy, doc_id, readable)
        return None if row is None else _to_doc(row)

    async def full_context(self, policy: TenantKnowledgePolicy) -> str:
        """Every permitted document for `policy.tenant_id`, concatenated and capped.

        The most dangerous of the three if the tenant predicate is forgotten: it needs no
        query to leak, it hands the corpus to the model as a system prompt, and the agent
        then answers from it confidently for the rest of the conversation.
        """
        readable = self._readable(policy, ())
        if not readable:
            return ""
        rows = await asyncio.to_thread(self._full_context_sync, policy, readable)
        return _cap(_render(rows), policy.max_context_chars)

    def _readable(
        self, policy: TenantKnowledgePolicy, requested: tuple[CollectionId, ...]
    ) -> list[str]:
        """The requested collections intersected with what the policy grants.

        An empty grant list means NOTHING (`KnowledgePolicy.can_read`), so this returns an
        empty list and the caller returns an empty answer without touching the database.
        A list rather than a tuple because psycopg adapts a Python list to a Postgres
        array and a tuple to a composite - `collection = ANY(%s)` needs the array.
        """
        asked = requested if requested else policy.collections
        return [str(collection) for collection in asked if policy.can_read(collection)]

    def _search_sync(
        self, policy: TenantKnowledgePolicy, query: str, collections: list[str]
    ) -> Sequence[tuple[Any, ...]]:
        with self._connect() as connection:
            rows = connection.execute(
                _SEARCH_SQL,
                (query, str(policy.tenant_id), collections, query, policy.top_k),
            ).fetchall()
        return list(rows)

    def _get_sync(
        self, policy: TenantKnowledgePolicy, doc_id: DocId, collections: list[str]
    ) -> tuple[Any, ...] | None:
        with self._connect() as connection:
            row = connection.execute(
                _GET_SQL, (str(policy.tenant_id), str(doc_id), collections)
            ).fetchone()
        return None if row is None else tuple(row)

    def _full_context_sync(
        self, policy: TenantKnowledgePolicy, collections: list[str]
    ) -> Sequence[tuple[Any, ...]]:
        with self._connect() as connection:
            rows = connection.execute(
                _FULL_CONTEXT_SQL, (str(policy.tenant_id), collections)
            ).fetchall()
        return list(rows)

    def _within_budget(
        self, hits: tuple[KnowledgeHit, ...], max_context_chars: int
    ) -> tuple[KnowledgeHit, ...]:
        """Truncate the COMBINED excerpts to `max_context_chars`, best hit first.

        Capping each excerpt independently would let k results spend k times the budget,
        which is how a context window quietly fills with retrieved text and the model stops
        reading the conversation. A hit whose budget has run out is dropped rather than
        included empty: an empty excerpt is a citation with nothing behind it.
        """
        kept: list[KnowledgeHit] = []
        remaining = max_context_chars
        for hit in hits:
            if remaining <= 0:
                break
            excerpt = hit.excerpt[:remaining]
            remaining -= len(excerpt)
            kept.append(
                KnowledgeHit(
                    doc_id=hit.doc_id,
                    collection=hit.collection,
                    title=hit.title,
                    excerpt=excerpt,
                    score=hit.score,
                    version=hit.version,
                )
            )
        return tuple(kept)


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


def _render(rows: Sequence[tuple[Any, ...]]) -> str:
    return "\n\n".join(f"# {title}\n{body}" for title, body in rows)


def _cap(text: str, max_context_chars: int) -> str:
    return text[:max_context_chars]
