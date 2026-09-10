"""Knowledge vocabulary - what the business offers TODAY.

Phase:   F8 - Knowledge retrieval
Tasks:   docs/TASKS.md#t-f8-01
Status:  TYPES DEFINED / BEHAVIOUR PENDING

WHAT THIS IS NOT
    This is NOT conversation memory. Compaction (F5) owns that. Two different subsystems
    with different lifecycles, and blurring them is how both end up wrong:

        conversation memory   per session, ages, compressed, written every turn
        knowledge base        shared, versioned, permanent, written by an admin

THE LINE BETWEEN SKILLS AND KNOWLEDGE BASE
    Skills          = HOW to do things. Static, in git, written by engineers.
                      Procedures, escalation rules, policies.
    KnowledgeBase   = WHAT the business offers today. Dynamic, in the database,
                      edited by administrators. Prices, hours, catalogue, availability.

    The deciding question is NOT retrieval quality - it is WHO EDITS IT. A price lives here
    even when it is two lines long, because a SKILL.md is a file in git and an administrator
    is not going to open a pull request to change a price.

RETRIEVAL MODE IS CONFIGURATION, NOT ARCHITECTURE
    FULL_TEXT for a small corpus: inject or read it whole, no embeddings, no vector index.
    SEMANTIC when it outgrows that. Same port, same admin write path, one config line.

    Rule of thumb: under ~20-30K characters total, FULL_TEXT wins outright. The model
    reading the complete document has ZERO retrieval error; adding embeddings buys nothing
    and adds a chance of fetching the wrong chunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

DocId = NewType("DocId", str)
CollectionId = NewType("CollectionId", str)


class RetrievalMode(StrEnum):
    """FULL_TEXT  - whole documents; no embeddings. Day 1 for every small corpus.
    SEMANTIC   - chunked and embedded. Turn on per collection when it outgrows FULL_TEXT.
    HYBRID     - semantic + keyword, fused. Postgres gives both in ONE query (pgvector +
                 tsvector), which is the main reason pgvector is the day-1 choice over a
                 dedicated vector database.
    """

    FULL_TEXT = "full_text"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class KnowledgeDoc:
    """One versioned document. Updates create a NEW version; nothing is overwritten.

    `effective_from` plus `superseded_by` answer the question an operator actually asks:
    "why did the agent quote the old price on Tuesday?" Overwriting destroys that answer,
    and for prices that answer is the difference between a bug and an argument.

    `collection` is a PERMISSION BOUNDARY, not an organising convenience. A fraud agent
    must not be able to retrieve from the delivery corpus."""

    doc_id: DocId
    collection: CollectionId
    title: str
    body: str
    version: int = 1
    effective_from: datetime | None = None
    superseded_by: DocId | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """One retrieval result. `score` is comparable only within a single query."""

    doc_id: DocId
    collection: CollectionId
    title: str
    excerpt: str
    score: float
    version: int


@dataclass(frozen=True, slots=True)
class KnowledgePolicy:
    """Per-profile knowledge access. Lives on AgentProfile. DISABLED BY DEFAULT.

    `collections` is the whole answer to "only certain agents": an empty tuple means this
    agent retrieves nothing at all.

    `min_score` is not optional. Without a relevance floor, retrieval always returns k
    results even when nothing is relevant, and the agent gets confidently irrelevant
    context. AN EMPTY RESULT IS A VALID ANSWER.

    `max_context_chars` mirrors Hermes' MEMORY_CONTEXT_MAX_CHARS = 6,000. Borrowed rather
    than invented, because it was tuned against real conversations."""

    enabled: bool = False
    collections: tuple[CollectionId, ...] = ()
    mode: RetrievalMode = RetrievalMode.FULL_TEXT
    top_k: int = 5
    min_score: float = 0.6
    max_context_chars: int = 6_000
    inject_into_prompt: bool = False

    def can_read(self, collection: CollectionId) -> bool:
        """PSEUDO-CODE - F8.

        Empty `collections` means NOTHING, not everything - same reading as
        MediaPolicy.accepts, opposite of PolicyRule.subject_roles. The asymmetry is
        deliberate and it is tested: permissive defaults are acceptable for convenience
        features and unacceptable for data access.
        """
        raise NotImplementedError("F8 - docs/TASKS.md#t-f8-01")
