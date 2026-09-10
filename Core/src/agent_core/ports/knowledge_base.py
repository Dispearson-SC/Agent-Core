"""Port: KnowledgeBase - READ ONLY. What does the business offer today?

Phase:   F8
Tasks:   docs/TASKS.md#t-f8-02
Adapter: adapters/driven/knowledge_pg/
Per-vertical: NO (collections are data)

EVERY READ IS SCOPED BY A TENANT-NARROWED POLICY
    `TenantKnowledgePolicy` is a `KnowledgePolicy` that has been narrowed for exactly one
    tenant, and it is the only thing these methods accept. It replaced a plain
    `KnowledgePolicy`, which carried no tenant at all - so this port's own instruction to
    "filter by tenant IN THE QUERY" named a value the adapter had never been given.

    There is deliberately NO tenant parameter next to the policy. Two sources for one fact
    can disagree, and the adapter would then have to choose which one scopes the query.
    Same reading as `TranscriptReader.page`, where the tenant arrives inside `SessionRef`.

THERE IS NO WRITE METHOD ON THIS PORT. THAT IS THE POINT.
    Writes live on ports/knowledge_admin.py, which the agent is NEVER injected with.

    This is the strongest available defence against knowledge-base poisoning: a prompt
    injection cannot call a method that does not exist on the object it has. Structural,
    not conventional - nobody can forget to check a flag.

    Poisoning here would be worse than any MCP injection, because MCP is ephemeral and this
    is persistent: contaminate the corpus once and every future conversation is affected.

DAY 1 IS FULL_TEXT AND THAT IS DELIBERATE
    Small corpus -> return whole documents, no embeddings, no vector index, no pgvector.
    The model reading a complete document has zero retrieval error.

    SEMANTIC and HYBRID exist in the signature so switching is a config line per collection
    rather than a redesign. Ready for it, not paying for it.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    KnowledgeDoc,
    KnowledgeHit,
    TenantKnowledgePolicy,
)


class KnowledgeBase(Protocol):
    async def search(
        self,
        policy: TenantKnowledgePolicy,
        query: str,
        *,
        collections: tuple[CollectionId, ...] = (),
    ) -> tuple[KnowledgeHit, ...]:
        """PSEUDO-CODE - F8. Backs the `knowledge_search` tool.

        1. Intersect the requested collections with `policy.collections`. An agent asking
           for a collection it may not read gets an EMPTY result, not an error - an error
           message confirms the collection exists, which is itself a leak.
        2. Filter by `policy.tenant_id` IN THE QUERY, never post-retrieval. Cross-tenant
           leakage through a shared index is the classic multi-tenant RAG breach. The
           predicate has exactly one source and it cannot be absent: `policy` is narrowed
           for one tenant before it reaches this port.
        3. Return only current versions (`superseded_by IS NULL`) whose `effective_from`
           has passed.
        4. Drop hits below `policy.min_score`. Returning k results when nothing is relevant
           hands the agent confidently irrelevant context.
        5. Truncate the combined excerpts to `policy.max_context_chars`.

        RESULTS ARE UNTRUSTED CONTENT. The corpus holds text a human wrote. The caller
        wraps every excerpt in untrusted-content delimiters, same as `mcp_*`.
        """
        ...

    async def get(self, policy: TenantKnowledgePolicy, doc_id: DocId) -> KnowledgeDoc | None:
        """Fetch one current document. None when missing OR when policy forbids the
        collection - the caller cannot distinguish, deliberately.

        A `DocId` from another tenant is exactly the "missing" case, and it is missing
        because `policy.tenant_id` is in the WHERE clause, not because a check was
        remembered afterwards. Document ids are guessable; the predicate is what makes
        guessing one useless."""
        ...

    async def full_context(self, policy: TenantKnowledgePolicy) -> str:
        """PSEUDO-CODE - F8. The FULL_TEXT path: every permitted document for
        `policy.tenant_id`, concatenated, capped at `policy.max_context_chars`, for
        injection into the system prompt.

        THE MOST DANGEROUS OF THE THREE if the tenant predicate is forgotten: this one does
        not even need a query to leak, it hands the whole corpus to the model as a system
        prompt, and the agent will then answer from it confidently for the rest of the
        conversation.

        ONLY called when `policy.inject_into_prompt` is True.

        CACHE-CRITICAL - THIS IS WHERE RAG COLLIDES WITH D8.
        Injected context that CHANGES between turns rewrites the prompt prefix and breaks
        the provider cache, so the next request re-bills the whole prompt at full price -
        exactly the compaction trap by another door.

        Mitigations, both required:
          - This content is STABLE between turns (it changes only when an admin edits it),
            so it belongs in the cacheable prefix.
          - Place it AFTER the persona and the skills index and never interleave it with
            per-turn content.

        If a future variant needs per-query retrieval injected into the prompt, it goes
        AFTER the stable prefix, and the cost of that must be measured before shipping.
        """
        ...
