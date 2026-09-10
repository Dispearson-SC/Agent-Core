"""Use case: ResumeTurn - continue a suspended turn with human answers.

Phase:   F3 (approvals) / F7 (evidence, same code path)
Tasks:   docs/TASKS.md#t-f3-02
Status:  PSEUDO-CODE ONLY

THE INSIGHT THIS FILE ENCODES
    Approval and evidence are the SAME flow. Pydantic AI's deferred tools cover both
    approval-required and externally-executed tools; both end the run and return
    DeferredToolRequests.

    So this use case has no branch on `PendingKind` except when building the tool result:
    an approval yields allowed/refused, an evidence request yields a MediaRef. Everything
    around that is identical. If you find yourself adding a second code path for evidence,
    stop - you are undoing docs/DECISIONS.md#d9.

RESUMING CAN SUSPEND AGAIN
    An approval may unlock a tool whose result triggers an evidence request. The workflow
    loops; this use case just returns another two-shaped outcome. That is normal, not an
    error, and the loop needs a bound - see the workflow adapter.
"""

from __future__ import annotations

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import SessionRef, TurnId, TurnOutcome
from agent_core.ports.agent_runner import AgentRunner, ToolResolution
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.media_store import MediaStore


class ResumeTurn:
    def __init__(
        self,
        *,
        runner: AgentRunner,
        store: ConversationStore,
        audit: AuditSink,
        media: MediaStore,
        profiles: dict[str, AgentProfile],
    ) -> None:
        self._runner = runner
        self._store = store
        self._audit = audit
        self._media = media
        self._profiles = profiles

    def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        """PSEUDO-CODE - implement in F3.

        STEP 1 - VALIDATE THE RESOLUTIONS AGAINST WHAT IS ACTUALLY PENDING
            pending = load the suspended outcome for turn_id
            Every resolution's tool_call_id MUST appear in `pending`. Reject unknown ids.

            SILENT BUG: an id that does not match is dropped by Pydantic AI without an
            exception, and the agent asks the same question forever. Round-trip the ids
            VERBATIM - never regenerate, never normalise case, never re-encode.

        STEP 2 - AUDIT THE HUMAN DECISION BEFORE ACTING ON IT
            for r in resolutions:
                self._audit.record_human_decision(turn_id, r.tool_call_id, subject, ...)
            Before, not after. If resuming crashes, the record that a human approved
            something must already exist.

        STEP 3 - BUILD THE TOOL RESULTS
            APPROVAL approved   -> let the tool run
            APPROVAL refused    -> a refusal result carrying the human's reason, so the
                                   model can adapt instead of retrying
            EVIDENCE supplied   -> resolve the MediaRef through MediaStore per the
                                   profile's delivery mode (BYTES by default; a signed
                                   URL only when the policy says so)

        STEP 4 - RESUME
            outcome = self._runner.resume(turn_id, profile, history, resolutions)

        STEP 5 - PERSIST AND RETURN
            self._store.append_outcome(turn_id, outcome)
            Return it. It may be suspended AGAIN. Do not loop here.

        IDEMPOTENCY - THIS IS A DBOS STEP AND IT WILL BE REPLAYED
            Resuming the same turn with the same resolutions twice must not run the tool
            twice. Key on (turn_id, tool_call_id) and treat an already-resolved id as a
            no-op that returns the stored outcome.

            Without that, a crash between the tool running and the outcome being persisted
            means the account gets frozen twice on recovery. No test will catch it; only a
            deliberate crash-injection test will.
        """
        raise NotImplementedError("F3 - docs/TASKS.md#t-f3-02")
