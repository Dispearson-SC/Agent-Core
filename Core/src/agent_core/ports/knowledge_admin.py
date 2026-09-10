"""Port: KnowledgeAdmin - WRITE. Administrators only, never an agent.

Phase:   F8
Tasks:   docs/TASKS.md#t-f8-03
Adapter: adapters/driven/knowledge_pg/
Per-vertical: NO

THE SECURITY PROPERTY THIS PORT PROVIDES BY EXISTING SEPARATELY
    `StartTurn` is injected with `KnowledgeBase`. It is NEVER injected with this. So no
    tool, no hook, and no prompt injection can reach a write path - the method is not on
    any object the agent holds.

    Compare with the alternative, a single port with read and write methods plus an
    `if is_admin` check. That check is one refactor away from being wrong, and when it is
    wrong nothing fails.

    It also satisfies the port rule in CLAUDE.md: one port, one question. "What does the
    business offer?" and "change what the business offers" are two questions.

IDENTITY IS A DIFFERENT TYPE, NOT A FLAG
    Every method takes `AdminIdentity`, which is NOT `CallerIdentity` and cannot be
    produced from one. A chat client's identity cannot be widened into an administrator's
    by any code path, and the type checker enforces it before a test would.

UPDATES ARE VERSIONS, NEVER OVERWRITES
    A price change creates version n+1 and marks n superseded. That history is what answers
    "why did the agent quote the old price on Tuesday?" - and for pricing, that is the
    difference between a bug report and a dispute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agent_core.domain.knowledge import CollectionId, DocId, KnowledgeDoc


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    """Deliberately NOT CallerIdentity, and deliberately not convertible from one.

    `collections` scopes which corpora this administrator may edit - a franchise manager
    edits their own branch's hours, not head office's price list."""

    subject_id: str
    collections: tuple[CollectionId, ...] = ()
    is_superuser: bool = False


class KnowledgeAdmin(Protocol):
    async def upsert(
        self, admin: AdminIdentity, doc: KnowledgeDoc, *, effective_from: datetime | None = None
    ) -> KnowledgeDoc:
        """PSEUDO-CODE - F8.

        1. Authorise: `doc.collection` must be in `admin.collections` (or superuser).
        2. Load the current version; bump to n+1; mark n superseded.
        3. Persist, stamping `updated_by` and `updated_at`.
        4. AUDIT: who, when, which document, and the BEFORE and AFTER values. A price change
           with no before-value is not an audit record, it is a note.
        5. If the collection is SEMANTIC, re-embed ONLY the changed chunks. Re-embedding a
           whole corpus for one price edit is how an update path becomes too slow to use,
           and an update path too slow to use stops being used.

        `effective_from` in the future is a SCHEDULED change - new hours starting Monday.
        Retrieval already filters on it, so this needs no separate scheduler.
        """
        ...

    async def delete(self, admin: AdminIdentity, doc_id: DocId) -> None:
        """Soft delete: mark superseded, keep the row. A hard delete destroys the record of
        what the agent used to tell customers, which is the one thing an audit needs."""
        ...

    async def list_docs(
        self, admin: AdminIdentity, collection: CollectionId, *, include_superseded: bool = False
    ) -> tuple[KnowledgeDoc, ...]:
        """For the admin UI. `include_superseded=True` renders the change history."""
        ...
