"""Knowledge vocabulary - what the business offers TODAY.

Phase:   F8 - Knowledge retrieval
Tasks:   docs/TASKS.md#t-f8-01
Status:  TYPES DEFINED / KnowledgePolicy.can_read DONE (t-f8-01) /
         TENANT NARROWING ADDED - see the t-f8-01 correction note in docs/TASKS.md

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

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import NewType

from agent_core.domain.turn import CallerIdentity, TenantId

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
        """Whether this agent may retrieve from `collection`.

        Empty `collections` means NOTHING, not everything - same reading as
        MediaPolicy.accepts, opposite of PolicyRule.subject_roles. The asymmetry is
        deliberate and it is tested: permissive defaults are acceptable for convenience
        features and unacceptable for data access.

        Membership is the whole rule, and it is written as membership rather than as a
        special case for the empty tuple: a grant list nobody was added to denies
        everything by construction, so there is no empty branch that a later edit could
        invert into a wildcard.
        """
        return collection in self.collections


@dataclass(frozen=True, slots=True)
class TenantKnowledgePolicy(KnowledgePolicy):
    """A KnowledgePolicy narrowed for exactly ONE tenant. The only thing `KnowledgeBase`
    accepts.

    WHY THIS TYPE EXISTS
        `ports/knowledge_base.py` instructs its adapter to "filter by tenant IN THE QUERY,
        never post-retrieval", and for as long as the whole contract was
        `(policy, query, collections)` no adapter could obey it: nothing in the contract
        carried a tenant. An instruction a port gives that its own signature cannot supply
        is not a rule, it is a hope - and the failure it was warning about, a shared index
        answering tenant A with tenant B's documents, leaves no exception and no failing
        test behind.

    WHY THE TENANT IS CARRIED HERE RATHER THAN PASSED ALONGSIDE
        This is `RuleSet`'s answer, one boundary over, and it is the same answer for the
        same reason. `RuleSet` stores the roles and channel it was loaded for, so a
        snapshot IS the answer for one caller's narrowing and cannot be pointed at another
        one. A separate `tenant` parameter next to the policy type-checks perfectly while
        naming a tenant the policy was never loaded for, and in retrieval that means one
        business reading another's prices. One value, one tenant, nothing to mismatch.

    WHY IT IS A SUBCLASS AND NOT A FIELD ON KnowledgePolicy
        `KnowledgePolicy` is profile CONFIGURATION: `AgentProfile.knowledge` holds one, and
        `profile._build_knowledge_policy` derives the accepted YAML keys from
        `fields(KnowledgePolicy)`. A `tenant_id` field there would immediately become an
        authorable key, and a profile file naming its own tenant is the breach with a
        config file's manners. The tenant belongs to the CALLER, so it joins the policy at
        the moment a caller is known and not before.

        Subclassing keeps `collections`, `min_score` and `max_context_chars` where the
        retrieval path already reads them, and it makes the assignment one-way: a narrowed
        policy goes anywhere a `KnowledgePolicy` is wanted, and an unnarrowed one goes
        nowhere near the port.

    `tenant_id` is keyword-only and has NO DEFAULT, so the narrowing cannot be performed
    absent-mindedly. Every other field here defaults; this one is the whole point."""

    tenant_id: TenantId = field(kw_only=True)

    @classmethod
    def for_caller(cls, caller: CallerIdentity, policy: KnowledgePolicy) -> TenantKnowledgePolicy:
        """Narrow `policy` for `caller`'s tenant. The one intended construction path.

        Shaped exactly like `RuleSet.for_caller` so the two security boundaries read the
        same way: the narrowing comes from the identity the turn is being served for,
        rather than being copied by hand at each call site - which is where it would
        eventually be copied wrong.

        The settings are carried across one by one rather than splatted, so adding a field
        to `KnowledgePolicy` is a visible edit here. `tests/unit/test_knowledge.py` fails if
        one is ever forgotten: a silently dropped `min_score` would put every tenant back on
        the default relevance floor without anything raising."""
        return cls(
            enabled=policy.enabled,
            collections=policy.collections,
            mode=policy.mode,
            top_k=policy.top_k,
            min_score=policy.min_score,
            max_context_chars=policy.max_context_chars,
            inject_into_prompt=policy.inject_into_prompt,
            tenant_id=caller.tenant_id,
        )
