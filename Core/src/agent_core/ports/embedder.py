"""Port: Embedder - turn this text into a vector. One question, and only this one.

Phase:   D2 - Multi-tenancy / SEMANTIC retrieval
Tasks:   docs/TASKS.md#t-d2-07
Adapter: none yet - see "WHICH MODEL" below; the seat is typed, the model is not chosen
Per-vertical: NO

WHY THIS PORT EXISTS AT ALL - IT WAS FOUND, NOT DESIGNED
    `t-d2-05` built SEMANTIC retrieval and discovered that nothing in `ports/` produces an
    embedding. `ModelGateway` answers "which model and provider am I talking to", not
    "embed this text" - it has no `embed` method and adding one would make it answer two
    questions. `KnowledgeAdmin.upsert`'s own pseudo-code says step 5 is "if the collection
    is SEMANTIC, re-embed ONLY the changed chunks", and there was nothing to call to do it.

    So the adapter took `EmbedQuery = Callable[[str], Awaitable[Sequence[float]]]`, declared
    inside itself. That type-checks perfectly and names nothing: any async function of one
    string satisfies it, including one backed by a different model than the stored vectors
    were built with. A callable nobody owns is not a seam, it is a gap with a signature.

ONE PORT, ONE QUESTION - AND DELIBERATELY NOT A KNOWLEDGE-STORE METHOD
    "Embed this text" is one question. It is NOT "store this document", and this protocol
    must never grow a method that touches the corpus. Non-negotiable #8 is structural: the
    corpus has exactly one write surface, `ports/knowledge_admin.py`, and an agent is never
    injected with it. An embedder that could also write would be a second write surface
    reachable from anything that retrieves - which is every turn.

    What this port returns is numbers. That is the whole of it.

THE VECTOR CARRIES THE MODEL THAT PRODUCED IT, AND THAT IS NOT DECORATION
    Embeddings from two models are not comparable. A query embedded by model B searched
    against a corpus embedded by model A returns a confidently ranked list of the wrong
    documents, with no exception, no failed test and no log line - `CLAUDE.md`'s silent-bug
    table, in a new place. `semantic.py`'s own docstring predicted it: "a deployment that
    wires an embedder whose vectors come from a DIFFERENT model than the stored ones were
    built with gets nonsense rankings with nothing raising - the model identity belongs in
    that port when somebody writes it."

    It is here. `Embedding.model` is the id the vector came from, so the mismatch is
    DETECTABLE. It is not yet DETECTED at query time: `knowledge_docs` has no column to
    record the model a stored vector came from, and adding one is a migration, which is not
    in this anchor's writes set. Recorded in the anchor's findings rather than hidden -
    until that column exists, the check has to be a deployment discipline.

WHICH MODEL, AND WHAT ONE CALL COSTS
    No adapter implements this port yet, and this file does not pretend to choose. What is
    known and verified is in `docs/FIELD-NOTES.md`: the two live providers here are MiniMax
    (`minimax/MiniMax-M3`) and Gemini (`gemini/gemini-3-flash-preview`), both CHAT models,
    reached through LiteLLM. **No embedding model has been probed against either provider
    in this repository**, so naming one here would be exactly the kind of unverified fact
    `docs/FIELD-NOTES.md` exists to stop. Whoever writes the adapter probes it first and
    records the model id, the dimension and the cost per call there.

    Two consequences the composition root has to hold at once, because both are real money:

    - EVERY upsert into a SEMANTIC collection now costs a model call, and every SEMANTIC
      query costs another. That is why the method takes ONE text: batching is a widening
      this port can take later, once a chunker exists, and premature batching would have
      fixed the call count for a chunking strategy nobody has written.
    - The dimension is fixed by the COLUMN (`semantic_migration.EMBEDDING_DIM`), not by
      this port. A model whose vectors are a different width cannot be swapped in without a
      migration, and pgvector will refuse the insert rather than truncate - which is the
      one good outcome available.

WHAT HAPPENS WHEN IT FAILS
    It is I/O against a paid provider, so it fails: timeouts, rate limits, a dead
    credential. This port does not classify those - `ModelGateway.classify_error` already
    owns "which of the five recoveries this failure earns", and duplicating that here would
    give the system two classifiers to disagree with each other.

    What it does promise is that failure is an EXCEPTION and never an empty vector. A zero
    vector stored in the column is worse than no vector at all: `embedding IS NOT NULL`
    accepts it, so the document is retrievable, equidistant from every query, and quietly
    wrong. The unembedded count in `semantic.py` would report nothing, because there is
    nothing left to count.

D13: async, like every port above the domain. A model call is I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["Embedder", "Embedding"]


@dataclass(frozen=True, slots=True)
class Embedding:
    """One vector and the identity of the model that produced it.

    `model` is the id as the caller asked for it (`provider/model`), the same spelling
    `ModelAttempt.model` uses, so a log line from either side of the system names the route
    the same way. It is NOT optional and there is no default: an embedding whose provenance
    is unknown cannot be compared to anything, and a defaulted field is how it would become
    unknown without anybody choosing that.

    `vector` is a tuple rather than a list because this is a value: two callers must not be
    able to hold the same embedding and have one of them mutate it. Frozen for the same
    reason `ModelAttempt` is - a consumer must not be able to edit its own evidence.
    """

    model: str
    vector: tuple[float, ...]


class Embedder(Protocol):
    async def embed(self, text: str) -> Embedding:
        """Produce the embedding for exactly this text.

        PSEUDO-CODE:
        1. Call the embedding model for `text`. One call, one text - see the module
           docstring on why this is not a batch method yet.
        2. Return the vector WITH the model id that produced it. Never a bare sequence:
           a vector with no provenance cannot be told apart from one built by a model that
           was swapped out last week.
        3. RAISE on failure. Never return an empty or zero vector to keep a caller moving -
           a zero vector passes `embedding IS NOT NULL`, sits at a constant distance from
           every query, and is therefore retrievable and always wrong.

        The caller decides what a failure means: `semantic.py` lets a failed QUERY
        embedding propagate, because a search that cannot embed its query has no answer to
        give, and the admin decorator turns a failed DOCUMENT embedding into a loud refusal
        naming the document - it is already stored, and an administrator told "saved" about
        a row that can never be retrieved is the same silence one layer down.
        """
        ...
