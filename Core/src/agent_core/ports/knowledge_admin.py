"""Port: KnowledgeAdmin - WRITE. Administrators only, never an agent.

Phase:   F8
Tasks:   docs/TASKS.md#t-f8-03
Status:  t-f8-03 FROZEN - AdminSubjectId makes the identity wall a type-checked one;
         TenantAdminScope makes every write name the business it is editing
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

    THAT LAST SENTENCE WAS NOT TRUE UNTIL `AdminSubjectId` EXISTED. Two frozen dataclasses
    are only unrelated while their FIELDS are, and both identities carried a plain `str`
    subject id. So

        AdminIdentity(subject_id=caller.subject_id)

    type-checked cleanly under `mypy --strict`: no cast, no `Any`, no ignore comment. The
    separation was a naming convention, and the only field the two types share is the one
    a widening would reach for. `AdminSubjectId` closes it the way the codebase already
    closes `DocId` and `CollectionId` - a `str` no longer fits the seat, so minting an
    administrator becomes an explicit, greppable act performed by the admin route.
    `Core/tests/unit/test_ports_knowledge_admin.py` holds the line under mypy and by
    sweeping the package for any callable that takes a caller and returns an admin.

AND THE TENANT IS A DIFFERENT QUESTION AGAIN
    `AdminIdentity` answers "who is this and which corpora may they edit". It does NOT
    answer "whose corpora". Collection ids are not tenant-unique - that is settled by
    `ports/knowledge_base.py`, which needs a collection intersection AND a tenant predicate
    to be safe - so an administrator granted `pricing` was, until `TenantAdminScope`
    existed, granted every tenant's `pricing`. Every method here takes the scope, so a
    write path that never decided which business it was editing does not compile.

UPDATES ARE VERSIONS, NEVER OVERWRITES
    A price change creates version n+1 and marks n superseded. That history is what answers
    "why did the agent quote the old price on Tuesday?" - and for pricing, that is the
    difference between a bug report and a dispute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NewType, Protocol

from agent_core.domain.knowledge import CollectionId, DocId, KnowledgeDoc
from agent_core.domain.turn import TenantId

AdminSubjectId = NewType("AdminSubjectId", str)
"""Who an administrator is, in a type a `CallerIdentity.subject_id` cannot satisfy.

This is the whole mechanism behind non-negotiable #9. `NewType` costs nothing at runtime
and is erased to `str`, so this buys exactly one thing - `mypy` refuses the assignment -
and that one thing is the entire defence. Nobody has to remember a rule.

Deliberately NOT reused for `updated_by` on `KnowledgeDoc`: that field is a recorded name
in an audit trail, and an audit record must be able to hold an identifier that no longer
authorises anything.
"""


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    """Deliberately NOT CallerIdentity, and deliberately not convertible from one.

    `collections` scopes which corpora this administrator may edit - a franchise manager
    edits their own branch's hours, not head office's price list.

    DO NOT ADD `channel`, `tenant_id` OR `roles` HERE FOR CONVENIENCE. The two identities
    currently share exactly one field NAME, which is what makes
    `AdminIdentity(**asdict(caller))` fail at runtime - the one widening spelling mypy is
    structurally blind to, because unpacking a `dict[str, Any]` is always legal."""

    subject_id: AdminSubjectId
    collections: tuple[CollectionId, ...] = ()
    is_superuser: bool = False


@dataclass(frozen=True, slots=True)
class TenantAdminScope:
    """One administrator, narrowed to exactly ONE tenant. What every write method takes.

    WHY THE TENANT IS NOT A FIELD ON AdminIdentity
        Read the `AdminIdentity` docstring above: `channel`, `tenant_id` and `roles` are the
        three field names it refuses, because each one is a landing site for
        `AdminIdentity(**asdict(caller))` - the one widening spelling mypy is structurally
        blind to. Closing the tenant hole by opening that one would be a poor trade.

    WHY IT IS NOT A SUBCLASS EITHER
        A subclass of `AdminIdentity` IS an `AdminIdentity`, so
        `def scope_for(caller) -> TenantAdminScope` would be the forbidden widening while
        the package sweep in `tests/unit/test_ports_knowledge_admin.py`, which matches the
        return type by NAME, went on reporting the tree clean. The scan now knows both
        names, and this type stays outside the hierarchy so that neither defence depends on
        the other.

    THE PAIRING HAPPENS ONCE, WHERE THE CREDENTIAL IS VERIFIED
        The admin route (`t-f8-06`) authenticates an administrator and knows the tenant that
        credential belongs to; it builds the scope there and everything downstream carries
        the pair. `TenantKnowledgePolicy.for_caller` does the same job on the read side,
        where a `CallerIdentity` is available to derive from. There is no such derivation
        here on purpose: nothing an agent holds may mint one of these.

    `is_superuser` means every collection IN THIS TENANT. It has never meant every tenant,
    and it must not start: `tenant_id` is not defaulted and not optional, so a superuser is
    scoped exactly like anybody else."""

    admin: AdminIdentity
    tenant_id: TenantId = field(kw_only=True)


class KnowledgeAdmin(Protocol):
    async def upsert(
        self,
        scope: TenantAdminScope,
        doc: KnowledgeDoc,
        *,
        effective_from: datetime | None = None,
    ) -> KnowledgeDoc:
        """PSEUDO-CODE - F8.

        0. Every statement below is scoped by `scope.tenant_id` IN THE SQL, never checked
           afterwards - the same rule `ports/knowledge_base.py` states for retrieval, and
           for a stronger reason: a misdirected read shows one business another's prices,
           a misdirected write CHANGES them.
        1. Authorise: `doc.collection` must be in `scope.admin.collections` (or superuser).
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

    async def delete(self, scope: TenantAdminScope, doc_id: DocId) -> None:
        """Soft delete: mark superseded, keep the row. A hard delete destroys the record of
        what the agent used to tell customers, which is the one thing an audit needs.

        A `DocId` from another tenant matches nothing, because `scope.tenant_id` is in the
        WHERE clause. Document ids are guessable; the predicate is what makes guessing one
        useless."""
        ...

    async def list_docs(
        self,
        scope: TenantAdminScope,
        collection: CollectionId,
        *,
        include_superseded: bool = False,
    ) -> tuple[KnowledgeDoc, ...]:
        """For the admin UI. `include_superseded=True` renders the change history.

        The collection name alone does not identify a corpus - `pricing` exists in every
        tenant - so this listing is `(scope.tenant_id, collection)` or it is a directory of
        other people's documents."""
        ...
