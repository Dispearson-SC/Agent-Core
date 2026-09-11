"""Port: AuditReader - what did this turn call, and what was it told?

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-08
Status:  t-f11-08 FROZEN - the admin seat and the recorded-verdict shape are pinned by
         Core/tests/integration/test_audit_reader.py
Adapter: adapters/driven/persistence_pg/audit_read_repository.py
Per-vertical: NO

WHY THIS EXISTS AND WHAT IT IS NOT
    `AuditSink` is WRITE-ONLY BY DESIGN and that design is right: non-negotiable #6 makes
    the trail append-only, and the absence of any read member is part of what keeps it a
    single-purpose evidence writer. A read method bolted onto it would also drag the
    audit pool's write path into every inspection tool that wanted one row.

    So the read was nobody's. `adapters/driving/cli/console.py` declared its own
    `ToolCallLog` protocol and `main.py` bound a driven adapter by hand, because
    `Container` had no seat for one - the only place outside `composition.py` that ever
    chose a driven adapter. A consumer forced to declare the protocol it consumes is the
    definition of a missing port, and the next consumer declares a second one.

HOW THIS DIFFERS FROM TranscriptReader, WHICH IS THE FIRST QUESTION TO ASK
    They are not the same port and must not be merged. Three differences, and any one of
    them alone would be enough:

    1. DIFFERENT SOURCE. `TranscriptReader` projects `transcript_entries`, a table
       built for reading. This reads `audit_tool_calls`, the append-only evidence
       `PgAuditSink` writes on its own pool. The two can disagree, and when they do the
       audit table is the one that is right - that is the point of writing it separately.

    2. DIFFERENT AUDIENCE MODEL. `TranscriptReader` takes an `Audience` because it serves
       BOTH: non-negotiable #11 is "store everything, filter on read", and the USER
       projection is a real, supported answer it must produce. This port has no audience
       parameter because it has no user-facing projection at all - see below.

    3. DIFFERENT QUESTION. "What happened in this conversation" is a merged timeline over
       a session. "What did this turn call, and what was it told" is one turn's tool calls
       as the trail recorded them. One port, one question (CLAUDE.md).

    If those three ever collapse into one - if the transcript projection starts reading
    `audit_tool_calls` directly and grows a per-turn tool-call endpoint - then this port
    should be retired into it rather than left beside it. Saying that here is how the
    NEXT reader avoids adding a third.

THIS IS AN ADMIN-AUDIENCE PORT, AND THE TYPE IS WHAT SAYS SO
    Tool names, tool arguments and policy verdicts are ADMIN-only in
    `ports/transcript_reader.py`'s own list, and non-negotiable #11 says a user must not
    learn WHICH tool is pending. A port that answered a `CallerIdentity` would be the way
    around that rule, so the seat takes `AdminIdentity` (non-negotiable #9): a different
    type, which no code path may produce from a caller's, enforced by mypy rather than by
    anyone remembering.

    `AdminIdentity` is reused from `ports/knowledge_admin.py` rather than a second admin
    identity being minted here. Two admin types would mean two separately-audited walls
    around the same property, and the newer one is always the one without the package
    sweep in `tests/unit/test_ports_knowledge_admin.py` behind it.

THERE IS NO TENANT SEAT, AND THAT IS A LIMITATION, NOT A DESIGN
    `audit_tool_calls` has no `tenant_id` column - `0005` never gave it one - so a tenant
    predicate cannot go in the query, and this port does not pretend otherwise by taking a
    `TenantAdminScope` it could only check after the fact.

    The obvious fix is NOT to join `turns`, and it is worth writing down why: the audit
    write is deliberately outside the domain transaction, so a turn whose domain writes
    rolled back leaves audit rows and no `turns` row. An inner join would silently hide
    exactly the rows non-negotiable #6 exists to preserve - the one turn you most need to
    explain. The real fix is a `tenant_id` column on the audit table, written by the sink
    from `caller.tenant_id`, and it has no anchor yet.

READ-ONLY BY CONSTRUCTION
    No member here writes, amends or redacts. The same argument
    `tests/unit/test_ports_audit_sink.py` makes about the sink's missing `update_*`
    applies in mirror image: an evidence reader that could also correct a row would let a
    reading tool become a writing one, and the absence IS the defence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from agent_core.domain.policy import Effect
from agent_core.domain.turn import TurnId
from agent_core.ports.knowledge_admin import AdminIdentity


@dataclass(frozen=True, slots=True)
class AuditedToolCall:
    """One tool call, as the append-only trail recorded it. Not a re-decided one.

    THE RECORDED VERDICT, NOT TODAY'S. `effect`, `rule_id` and `reason` are what the
    policy engine answered AT THE TIME the call was attempted. Asking the engine again now
    would report today's rules against yesterday's call, and the two differ on exactly the
    day somebody edits a rule to explain an incident.

    `reason` IS THE SENTENCE, AND IT IS THE FIELD THIS PORT WAS BLOCKED ON. Until
    migration 0022 (t-f11-07) the table had no column for it, so the trail could say WHICH
    rule fired and never WHAT it said - and the sentence is the whole content of a
    refusal: on DENY the model was handed it as the tool result, on NEEDS_APPROVAL a human
    was asked it. `None` means a row written before that column existed; it does not mean
    the rule was silent, and a reader must not render it as one.

    `arguments` are whatever the sink stored, i.e. ALREADY redacted through
    `PgAuditSink`'s per-tool allowlist. Nothing downstream re-redacts: two redaction paths
    drift, and the drifting one is always the one nobody reads until an incident.

    `caller_subject_id` is a recorded name, a plain `str` and deliberately not an identity
    type. An audit row must be able to hold an identifier that no longer authorises
    anything - the same reasoning `AdminSubjectId` gives for staying off
    `KnowledgeDoc.updated_by`.
    """

    tool_name: str
    caller_subject_id: str
    effect: Effect
    at: datetime
    reason: str | None = None
    rule_id: str | None = None
    arguments: dict[str, object] = field(default_factory=dict)

    @property
    def executed(self) -> bool:
        """Whether the tool body actually ran.

        `PolicyEnforcement.before_tool_execute` raises `SkipToolExecution` whenever the
        decision is not ALLOW, so this mirrors `PolicyDecision.blocks_execution` rather
        than restating a list of blocking effects - an effect added to the enum later must
        read as "did not run" here, not fall through to "ran" unnoticed.
        """
        return self.effect is Effect.ALLOW


@runtime_checkable
class AuditReader(Protocol):
    """Read the tool-call trail. One question, admin audience, no write path.

    `runtime_checkable` for the reason `ports/tool_provider.py` and
    `ports/skill_registry.py` give: whoever wires this seat must be able to check that
    what they were handed answers the question, without importing the adapter that does.
    """

    async def tool_calls_for_turn(
        self, admin: AdminIdentity, turn_id: TurnId
    ) -> tuple[AuditedToolCall, ...]:
        """Every tool call filed under `turn_id`, oldest first. Empty is a normal answer.

        ASYNC (D13): Postgres access is synchronous, so the adapter wraps the blocking
        read in `asyncio.to_thread`, exactly as every other driven adapter does.

        OLDEST FIRST, BY APPEND ORDER. An operator reads an exchange forwards, and the
        order the rows were appended in is the order the turn attempted them. Sorting by
        the timestamp alone would tie two calls appended inside the same millisecond and
        let them come back in either order - and a denial that reads as if it preceded the
        call it refused is a trail that tells the wrong story.

        HOLDING AN `AdminIdentity` IS THE AUTHORISATION. There is nothing narrower to
        check here: `audit_tool_calls` carries no tenant, so the adapter cannot scope this
        further than the turn it was asked about. See the module docstring - that is a
        limitation of the table, recorded rather than papered over with a seat that would
        check nothing.
        """
        ...
