"""Unit tests for the port that produces an embedding, and for the silence it removes.

Phase:   D2 - Multi-tenancy / SEMANTIC retrieval
Tasks:   docs/TASKS.md#t-d2-07
Covers:  ports/embedder.py
         adapters/driven/knowledge_pg/semantic.py
         adapters/driven/knowledge_pg/admin.py

WHAT IS BEING DEFENDED, IN TWO HALVES

    HALF ONE - A DOCUMENT WITH NO EMBEDDING IS REPORTED, NOT QUIETLY DROPPED.
        `_SEMANTIC_SEARCH_SQL` filters `embedding IS NOT NULL`, so a document written
        before anything embedded it is excluded from EVERY semantic query and nothing says
        so. A retrieval that silently returns nothing is indistinguishable from an empty
        corpus - the exact failure `t-d2-05` refused to ship and escalated here instead.

        The assertion is therefore not "search raises": erroring on an unembedded row would
        break retrieval for a whole collection on every write. It is that the exclusion is
        COUNTED and REPORTED, and that the report is absent when there is nothing to
        report - an unconditional warning is noise, and noise is how a real one is missed.

    HALF TWO - THE THING THAT PRODUCES AN EMBEDDING IS A PORT.
        Before this anchor it was `EmbedQuery = Callable[[str], Awaitable[Sequence[float]]]`
        declared inside an adapter: no `ports/` protocol owned the question, nothing named
        the model, and two deployments could hand in vectors from two different models with
        nothing raising. `KnowledgeAdmin.upsert`'s own pseudo-code says step 5 is "re-embed
        only the changed chunks" - with nothing to call.

        So the tests below assert the SHAPE, not only the behaviour: `Embedder` is a
        Protocol in `ports/`, it asks exactly one question, the semantic adapter is annotated
        with it rather than with a bare callable, and the vector it returns carries the id of
        the model that produced it.

NON-NEGOTIABLE #8 IS NOT RELAXED BY ANY OF THIS
    An embedder is not a write path to the corpus. `Embedder` has exactly one method and it
    returns numbers; the only thing that writes an embedding is a `KnowledgeAdmin`
    decorator, which is never injected into anything an agent holds.

No database, no network, no model: the adapter's whole database seam is a
`ConnectionFactory`, so a fake connection that records the statements it is handed is
enough to prove what the SQL asked for (D13 - the sync work runs in `asyncio.to_thread`).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Sequence
from datetime import datetime
from types import ModuleType, TracebackType
from typing import Any

import pytest

from agent_core.adapters.driven.knowledge_pg import admin as admin_module
from agent_core.adapters.driven.knowledge_pg import semantic as semantic_module
from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    KnowledgeDoc,
    RetrievalMode,
    TenantKnowledgePolicy,
)
from agent_core.domain.turn import TenantId
from agent_core.ports.knowledge_admin import (
    AdminIdentity,
    AdminSubjectId,
    KnowledgeAdmin,
    TenantAdminScope,
)

_SEMANTIC_LOGGER = "agent_core.adapters.driven.knowledge_pg.semantic"


# ---------------------------------------------------------------------------------------
# Loading the pieces this anchor adds
#
# Imported through helpers rather than at module scope on purpose. A missing module at
# import time is a COLLECTION ERROR, and a collection error is not a red test: it proves
# the file is absent, not that the behaviour is wrong. Each helper turns the absence into
# the assertion the anchor is actually about.
# ---------------------------------------------------------------------------------------


def _port() -> ModuleType:
    try:
        return importlib.import_module("agent_core.ports.embedder")
    except ModuleNotFoundError as exc:  # pragma: no cover - the red state of this anchor
        raise AssertionError(
            "nothing in ports/ produces an embedding: agent_core.ports.embedder does not "
            "exist, so SEMANTIC retrieval runs on a callable injected into an adapter that "
            "no port owns, and no model id travels with the vector"
        ) from exc


def _fake_embedder_class() -> Any:
    fakes = importlib.import_module("tests.fakes.ports")
    fake = getattr(fakes, "FakeEmbedder", None)
    assert fake is not None, (
        "tests/fakes/ports.py has no FakeEmbedder. docs/WAVES.md rule 4: a task that adds "
        "a port owns the shared fake for it in the same change, or the next wave finds a "
        "port with no way to test against it"
    )
    return fake


def _embedding_admin_class() -> Any:
    decorator = getattr(admin_module, "EmbeddingKnowledgeAdmin", None)
    assert decorator is not None, (
        "no KnowledgeAdmin embeds what it just stored, so KnowledgeAdmin.upsert's step 5 - "
        "'re-embed only the changed chunks' - still has nothing to call and every document "
        "written to a SEMANTIC collection stays invisible to it"
    )
    return decorator


# ---------------------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------------------


class _EitherShapeEmbedder:
    """Embeds under the PORT shape (`.embed`) and under the pre-anchor CALLABLE shape.

    Deliberately both, and this is the only reason it exists. The adapter under test used
    to take `EmbedQuery`, a plain async callable; if this double offered only `.embed`, the
    unembedded-report test below would fail with `TypeError: object is not callable` before
    reaching its assertion - an error, not a red test. Carrying both shapes makes the
    adapter run either way, so the failure that is observed is the one being asserted: that
    excluded documents are not reported.
    """

    def __init__(self, dimensions: int = 4) -> None:
        self.dimensions = dimensions

    def _vector(self) -> list[float]:
        values = [0.0] * self.dimensions
        values[0] = 1.0
        return values

    async def embed(self, text: str) -> Any:
        return _port().Embedding(model="fake/embedding-1", vector=tuple(self._vector()))

    async def __call__(self, text: str) -> Sequence[float]:
        return self._vector()


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """A psycopg-shaped connection that answers from a script and records every statement.

    Routing on `count(` rather than on call order because the anchor's whole point is that
    the count statement does not exist yet: ordering would make the pre-implementation run
    answer the search with the count row and fail for the wrong reason.
    """

    def __init__(
        self,
        hits: list[tuple[Any, ...]],
        unembedded: int,
        log: list[tuple[str, Any]],
    ) -> None:
        self._hits = hits
        self._unembedded = unembedded
        self._log = log

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self._log.append((sql, params))
        if "count(" in sql.lower():
            return _FakeCursor([(self._unembedded,)])
        return _FakeCursor(self._hits)


class _RecordingAdmin:
    """A `KnowledgeAdmin` that stores nothing and remembers everything it was asked to do."""

    def __init__(self) -> None:
        self.upserted: list[KnowledgeDoc] = []
        self.deleted: list[DocId] = []

    async def upsert(
        self,
        scope: TenantAdminScope,
        doc: KnowledgeDoc,
        *,
        effective_from: datetime | None = None,
    ) -> KnowledgeDoc:
        stored = KnowledgeDoc(
            doc_id=doc.doc_id,
            collection=doc.collection,
            title=doc.title,
            body=doc.body,
            version=doc.version + 1,
        )
        self.upserted.append(stored)
        return stored

    async def delete(self, scope: TenantAdminScope, doc_id: DocId) -> None:
        self.deleted.append(doc_id)

    async def list_docs(
        self,
        scope: TenantAdminScope,
        collection: CollectionId,
        *,
        include_superseded: bool = False,
    ) -> tuple[KnowledgeDoc, ...]:
        return tuple(self.upserted)


class _BrokenEmbedder:
    """The model call fails. Every embedder eventually is this one."""

    async def embed(self, text: str) -> Any:
        raise RuntimeError("upstream returned 503")


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------

_PRICING = CollectionId("pricing")


def _policy() -> TenantKnowledgePolicy:
    return TenantKnowledgePolicy(
        enabled=True,
        collections=(_PRICING,),
        mode=RetrievalMode.SEMANTIC,
        top_k=5,
        min_score=0.0,
        tenant_id=TenantId("t-1"),
    )


def _scope() -> TenantAdminScope:
    return TenantAdminScope(
        admin=AdminIdentity(subject_id=AdminSubjectId("admin-1"), collections=(_PRICING,)),
        tenant_id=TenantId("t-1"),
    )


def _hit_row() -> tuple[Any, ...]:
    return ("d-embedded", str(_PRICING), "Refunds", "Refunds take five days.", 1, 0.91)


def _search(
    unembedded: int,
    embedder: Any | None = None,
) -> tuple[tuple[Any, ...], list[tuple[str, Any]]]:
    log: list[tuple[str, Any]] = []
    base = semantic_module.PgSemanticKnowledgeBase(
        lambda: _FakeConnection([_hit_row()], unembedded, log),
        embedder if embedder is not None else _EitherShapeEmbedder(),
    )
    hits = asyncio.run(base.search(_policy(), "how long do refunds take"))
    return hits, log


# ---------------------------------------------------------------------------------------
# HALF ONE - the silence
# ---------------------------------------------------------------------------------------


def test_a_document_stored_without_an_embedding_is_reported_not_silently_excluded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """THE ASSERTION THIS ANCHOR EXISTS FOR.

    Two documents are live in the collection; one of them has no embedding, so the SQL's
    `embedding IS NOT NULL` drops it. The search still answers - that part is deliberate,
    erroring would take a whole collection offline on every write - but the exclusion must
    reach a human. An excluded document that nothing counts is indistinguishable from a
    document that was genuinely irrelevant, and from a corpus that is empty.
    """
    with caplog.at_level(logging.WARNING, logger=_SEMANTIC_LOGGER):
        hits, log = _search(unembedded=2)

    assert [hit.doc_id for hit in hits] == [DocId("d-embedded")], (
        "the embedded document must still be retrieved: reporting the unembedded ones is "
        "not allowed to cost the answer"
    )
    assert any("count(" in sql.lower() for sql, _ in log), (
        "nothing ever counted the documents that `embedding IS NOT NULL` excluded, so the "
        "adapter cannot know they exist - a document written before anything embedded it "
        "never matches a SEMANTIC query and NOTHING REPORTS IT"
    )

    reports = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert reports, (
        "two live documents in this collection have no embedding and were excluded from "
        "this search, and not one warning was emitted. A retrieval that silently returns "
        "less than the corpus holds is indistinguishable from an empty corpus"
    )
    message = " ".join(record.getMessage() for record in reports)
    assert "2" in message and str(_PRICING) in message, (
        f"the report must name how many documents were excluded and from which collection, "
        f"or an operator cannot act on it; got {message!r}"
    )


def test_nothing_is_reported_when_every_live_document_is_embedded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The negative half, and it is not a formality.

    A warning emitted on every search is noise, and noise is precisely how the real one
    gets missed. This is what keeps the test above from being satisfiable by an
    unconditional `log.warning` next to the query.
    """
    with caplog.at_level(logging.WARNING, logger=_SEMANTIC_LOGGER):
        hits, _ = _search(unembedded=0)

    assert [hit.doc_id for hit in hits] == [DocId("d-embedded")]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING], (
        "a fully embedded collection produced a warning, so the report carries no "
        "information and an operator will learn to ignore it"
    )


def test_the_report_names_the_tenant_it_was_counted_for() -> None:
    """The count is scoped in the SQL, exactly like the search it accompanies.

    A count that forgot the tenant predicate would report another business's unembedded
    documents to this one - a smaller leak than a retrieval breach and the same class of
    mistake, on the same shared table.
    """
    _, log = _search(unembedded=1)
    counts = [(sql, params) for sql, params in log if "count(" in sql.lower()]
    assert counts, "no count statement was issued at all"
    sql, params = counts[0]
    assert "tenant_id = %s" in sql, (
        f"the unembedded count is not scoped by tenant in the SQL; got {sql!r}"
    )
    assert "t-1" in tuple(params), (
        f"the tenant predicate is not bound to policy.tenant_id; got {params!r}"
    )
    assert "superseded_by IS NULL" in sql, (
        "archived versions carry no embedding by design, so counting them would report a "
        "backlog that does not exist and train an operator to ignore the number"
    )


# ---------------------------------------------------------------------------------------
# HALF TWO - the port
# ---------------------------------------------------------------------------------------


def test_the_thing_that_produces_an_embedding_is_a_port_asking_one_question() -> None:
    """`Embedder` is a Protocol in `ports/`, and it asks exactly one question.

    CLAUDE.md: a port answering two questions is cut wrong. And non-negotiable #8: an
    embedder must not become a knowledge-store method - the corpus has exactly one write
    surface and it is `KnowledgeAdmin`.
    """
    embedder = getattr(_port(), "Embedder", None)
    assert embedder is not None, "ports/embedder.py declares no Embedder"
    assert getattr(embedder, "_is_protocol", False), (
        "Embedder is not a typing.Protocol, so nothing structurally binds an adapter to it "
        "and it is an injected callable with a nicer name"
    )

    methods = sorted(
        name
        for name, _ in inspect.getmembers(embedder, inspect.isfunction)
        if not name.startswith("_")
    )
    assert methods == ["embed"], (
        f"one port, one question: 'embed this text'. Found {methods!r} - anything that "
        f"reads or writes a document belongs on KnowledgeBase or KnowledgeAdmin"
    )


def test_the_vector_carries_the_model_that_produced_it() -> None:
    """Embedding costs a model call, and WHICH model is not a detail.

    Vectors from two different models are not comparable: a query embedded by model B
    against a corpus embedded by model A ranks nonsense, and nothing raises. The model id
    travels with the vector so that mismatch is detectable rather than folkloric.
    """
    embedding = getattr(_port(), "Embedding", None)
    assert embedding is not None, (
        "ports/embedder.py returns a bare sequence of floats, so a vector cannot say which "
        "model produced it and a model swap silently degrades every ranking"
    )
    fields = {field.name for field in embedding.__dataclass_fields__.values()}
    assert {"model", "vector"} <= fields, (
        f"an Embedding must name its model and carry its vector; got {sorted(fields)!r}"
    )

    produced = asyncio.run(_fake_embedder_class()().embed("refunds"))
    assert produced.model, "the fake embedder returned a vector with no model id"
    assert len(produced.vector) > 0


def test_the_semantic_adapter_depends_on_the_port_and_not_on_a_bare_callable() -> None:
    """The adapter's seat is typed by the port.

    `EmbedQuery = Callable[[str], Awaitable[Sequence[float]]]` type-checked perfectly while
    naming nothing: any async function of one string fits, including one from a different
    model. The annotation is the whole defence and it is asserted rather than assumed.
    """
    port = _port()
    hints = inspect.get_annotations(
        semantic_module.PgSemanticKnowledgeBase.__init__, eval_str=True
    )
    seats = [name for name, kind in hints.items() if kind is port.Embedder]
    assert seats, (
        f"PgSemanticKnowledgeBase takes no Embedder; its constructor is annotated "
        f"{ {name: str(kind) for name, kind in hints.items()} !r}"
    )
    assert not hasattr(semantic_module, "EmbedQuery"), (
        "the adapter still exports the injected-callable alias this anchor replaces, so "
        "both shapes exist and a caller can keep using the one no port owns"
    )


def test_the_shared_fake_satisfies_the_port_and_drives_the_adapter() -> None:
    """docs/WAVES.md rule 4: the port, its implementations and the shared fake are one
    change. Three waves in a row ended red because a port moved and its fake did not."""
    fake = _fake_embedder_class()()
    hits, log = _search(unembedded=0, embedder=fake)
    assert [hit.doc_id for hit in hits] == [DocId("d-embedded")]
    assert fake.embedded, "the adapter never asked the injected Embedder for a vector"
    vector_params = [params for sql, params in log if "<=>" in sql]
    assert vector_params, "no statement ranked by embedding distance"


# ---------------------------------------------------------------------------------------
# The write side - a half-embedded document is the same bug one layer down
# ---------------------------------------------------------------------------------------


def test_an_embedding_that_fails_mid_upsert_is_loud_and_names_the_document() -> None:
    """`KnowledgeAdmin.upsert` step 5 now has something to call, and its failure is not
    swallowed.

    The document is already stored when the model call fails - that is what "mid-upsert"
    means, and it cannot be undone by pretending otherwise. What must not happen is the
    administrator being told the write succeeded while the row can never match a SEMANTIC
    query. That is the read-side silence this anchor removes, reappearing on the write
    side.
    """
    inner = _RecordingAdmin()
    decorator = _embedding_admin_class()(
        inner, _BrokenEmbedder(), lambda: _FakeConnection([], 0, [])
    )
    doc = KnowledgeDoc(doc_id=DocId("d-1"), collection=_PRICING, title="Refunds", body="Five days.")

    with pytest.raises(Exception) as failure:  # noqa: B017 - the type is the anchor's to name
        asyncio.run(decorator.upsert(_scope(), doc))

    assert inner.upserted, "the inner admin never ran, so this is not the mid-upsert case"
    message = str(failure.value)
    assert "d-1" in message, f"the failure does not name the document; got {message!r}"
    assert "embed" in message.lower(), (
        f"the failure does not say that the document is stored but unembedded, which is "
        f"the one thing an administrator has to know; got {message!r}"
    )


def test_the_embedding_admin_is_still_a_knowledge_admin_and_writes_the_vector() -> None:
    """It decorates the port rather than widening it: no new method reaches the corpus, and
    nothing an agent holds can reach this object at all (non-negotiable #8)."""
    inner = _RecordingAdmin()
    log: list[tuple[str, Any]] = []
    decorator = _embedding_admin_class()(
        inner, _fake_embedder_class()(), lambda: _FakeConnection([], 0, log)
    )
    port: KnowledgeAdmin = decorator
    doc = KnowledgeDoc(doc_id=DocId("d-1"), collection=_PRICING, title="Refunds", body="Five days.")

    stored = asyncio.run(port.upsert(_scope(), doc))

    assert stored.doc_id == DocId("d-1")
    writes = [(sql, params) for sql, params in log if "embedding" in sql.lower()]
    assert writes, "the document was stored and never embedded, which is the silent bug"
    sql, params = writes[0]
    assert "tenant_id = %s" in sql and "t-1" in tuple(params), (
        f"the embedding write is not scoped to the tenant that owns the document; got {sql!r}"
    )

    methods = sorted(
        name
        for name in dir(decorator)
        if not name.startswith("_") and callable(getattr(decorator, name))
    )
    assert methods == ["delete", "list_docs", "upsert"], (
        f"the decorator grew a surface KnowledgeAdmin does not have; got {methods!r}"
    )
