"""Use case: ResumeTurn - continue a suspended turn with human answers.

Phase:   F3 (approvals) / F7 (evidence, same code path)
Tasks:   docs/TASKS.md#t-f3-02
Status:  IMPLEMENTED (t-f3-02) - STEP 1 VALIDATION STILL PENDING, SEE THE READ-BACK NOTE

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

LAYER RULE
    Imports domain/ and ports/ ONLY - not even a sibling use case; `test_contract.py`
    enforces that by walking the AST, which is why `UnknownProfileError` is declared below
    instead of imported from `start_turn.py`. No Pydantic AI, no DBOS, no FastAPI, no
    driver.

WHY THE HUMAN DECISION IS NOT AUDITED HERE
    The stub's pseudo-code filed a `record_human_decision` row before resuming. It cannot,
    and it must not. It cannot because `record_human_decision` takes the `subject_id` of
    the person who decided, and nothing on this call carries one - `execute` is reached
    from the workflow waking out of `DBOS.recv()`, not from the human's request.

    It must not because `DecideApproval` (docs/TASKS.md#t-f3-03) already writes that row,
    on the human's own request, BEFORE it signals the workflow. So the property the stub
    wanted - "if resuming crashes, the record that a human approved something already
    exists" - is guaranteed by the time control reaches this method. Writing a second row
    would double-book an append-only sink (CLAUDE.md non-negotiable #6: never update), and
    an audit trail that records one decision twice cannot be reconciled afterwards.

    `AuditSink` stays on the constructor because that is the port this use case will file
    under once t-f3-05 settles who may decide; the seat is deliberate, not a leftover.

THE READ-BACK NOTE - WHAT STEP 1 CANNOT DO YET
    The stub's STEP 1 validates every incoming `tool_call_id` against the pending requests
    actually stored for `turn_id`. `ConversationStore` exposes `load_history`,
    `append_request` and `append_outcome`; it has no read-back of a stored `TurnOutcome`
    by turn id, and `HumanGateway` publishes and correlates but never reports whether a
    request is already answered. So there is no port through which the suspended outcome
    can be loaded, and inventing one belongs to a port anchor, not to this one.

    What is enforced instead is the half that needs no read-back: ids are unique within a
    batch, and an id this use case has already resolved never reaches the runner twice.

IDEMPOTENCY, AND EXACTLY HOW FAR IT REACHES
    Keyed on `(turn_id, tool_call_id)`: an already-resolved pair is a no-op returning the
    stored outcome. The ledger is in-process, which covers the case that actually happens
    - a retry, a double-clicking human, a redelivered signal - within one running process.

    Across a process death, the durable guarantee is DBOS step memoisation: a step that
    completed is not re-executed on replay, and this use case is awaited inside one. It is
    NOT a durable ledger of its own, and it deliberately does not pretend to be: the
    honest place for that is the `human_requests.answered_at` column migration `0010`
    already carries, reachable only once a port exposes it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.domain.media import MediaDelivery, MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import SessionRef, ToolCallId, TurnId, TurnOutcome
from agent_core.ports.agent_runner import AgentRunner, ToolResolution
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.media_store import MediaStore


class UnknownProfileError(KeyError):
    """`profile_id` names no loaded profile. Same rule and same reasoning as `StartTurn`'s
    error of the same name: falling back to a default profile turns a typo into a turn
    resumed with someone else's permissions.

    DECLARED HERE RATHER THAN IMPORTED. `test_contract.py` walks the AST of `application/`
    and allows the standard library, `agent_core.domain.*` and `agent_core.ports.*` only -
    a sibling use case is not on that list, and that guard is right: one use case importing
    another is how an application layer grows a private dependency graph nobody drew.

    The duplication is therefore deliberate and visible, not an oversight. A shared
    `application/errors.py` is the real home for it, and creating one is a task of its own.
    Subclassing `KeyError` means the obvious `except KeyError` at a call site still catches
    either type, so no caller has to know there are two.
    """


@dataclass(slots=True)
class ResolvedTool:
    """One `ToolResolution` ready to hand back to the runner.

    A concrete carrier exists because step 3 REPLACES the payload of an evidence
    resolution - a `MediaRef` becomes bytes or a signed URL - and the incoming value may
    be any structural `ToolResolution` the driving adapter chose to build. The replacement
    is a NEW value rather than a mutation of the caller's object: a use case that edits its
    argument in place behaves differently on the second call.

    NOT FROZEN, AGAINST THIS CODEBASE'S HABIT, AND NOT BY PREFERENCE. `ToolResolution` in
    `ports/agent_runner.py` declares plain attributes, which a Protocol reads as SETTABLE
    variables; mypy rejects a frozen dataclass against it with "expected settable variable,
    got read-only attribute". Freezing this class would make the very type this use case
    hands to `AgentRunner.resume` fail to satisfy the port it is handed to. The immutability
    is enforced by discipline instead: nothing here mutates an instance after construction.

    `tool_call_id` is copied VERBATIM and never regenerated. CLAUDE.md non-negotiable #5.
    """

    tool_call_id: ToolCallId
    approved: bool
    payload: object | None = None


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
        # The idempotency ledger. One entry per RESOLVED (turn_id, tool_call_id), holding
        # the outcome that resolving it produced. Read the module docstring for how far
        # this reaches and what carries the guarantee past a process death.
        self._resolved: dict[tuple[TurnId, ToolCallId], TurnOutcome] = {}

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        """Feed the human's answers back into a suspended turn.

        WHY IT IS ASYNC (D13): it awaits the store, possibly the media store, and then the
        model through `AgentRunner.resume` - the longest await in the system.

        WHAT THIS METHOD MUST NEVER DO
            - Loop. A resumed turn may suspend AGAIN; the outcome goes back untouched and
              the workflow decides what to do with it.
            - Regenerate a `tool_call_id`.
            - Run the same resolved tool call twice.
        """
        if not resolutions:
            raise ValueError(
                "ResumeTurn was called with no resolutions. A resume that answers nothing "
                "would re-enter the model with the turn still suspended."
            )
        self._reject_duplicate_ids(resolutions)

        # Before any write, exactly as in StartTurn: an unknown id must leave no trace.
        profile = self._resolve_profile(profile_id)

        # STEP 2 - DROP EVERY PAIR THIS USE CASE HAS ALREADY RESOLVED. Before the media
        # store is touched and long before the runner is: re-resolving an answered call
        # runs its tool a second time on one human approval.
        fresh = tuple(
            resolution
            for resolution in resolutions
            if (turn_id, resolution.tool_call_id) not in self._resolved
        )
        if not fresh:
            # Every id in this batch is answered, so this is a replay. Hand back what the
            # call that answered them produced. The batch is answered as a unit, so any of
            # its keys names the same outcome; the first is taken for determinism.
            return self._resolved[(turn_id, resolutions[0].tool_call_id)]

        # STEP 3 - BUILD THE TOOL RESULTS. An approval passes through; an evidence payload
        # is resolved through MediaStore per the profile's delivery mode.
        prepared = tuple([await self._prepare(resolution, profile) for resolution in fresh])

        # STEP 4 - RESUME. STEP 5 - PERSIST. A suspended outcome is persisted exactly like
        # a finished one; the process may die while the next human takes three days.
        history = await self._store.load_history(session)
        outcome = await self._runner.resume(turn_id, profile, history, prepared)
        await self._store.append_outcome(turn_id, outcome)

        # Recorded AFTER the outcome is persisted, never before. A pair marked resolved
        # against an outcome that was never stored would hand the next caller a result the
        # conversation has no record of.
        for resolution in fresh:
            self._resolved[(turn_id, resolution.tool_call_id)] = outcome
        return outcome

    def _reject_duplicate_ids(self, resolutions: tuple[ToolResolution, ...]) -> None:
        """Two answers for one pending call in a single batch is a caller bug, and a
        silent one: the runner would take whichever arrived last and the other human's
        answer would vanish without an exception."""
        seen: set[ToolCallId] = set()
        for resolution in resolutions:
            if resolution.tool_call_id in seen:
                raise ValueError(
                    f"Two resolutions carry tool_call_id {resolution.tool_call_id!r}. One "
                    "pending call has exactly one answer."
                )
            seen.add(resolution.tool_call_id)

    def _resolve_profile(self, profile_id: str) -> AgentProfile:
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise UnknownProfileError(
                f"No profile is loaded under id {profile_id!r}. Refusing to fall back to a "
                "default: a typo must not resume the turn with another profile's permissions."
            ) from None

    async def _prepare(
        self, resolution: ToolResolution, profile: AgentProfile
    ) -> ToolResolution:
        """Turn one human answer into the value the model receives as the tool result.

        Only an EVIDENCE payload needs work, and it is recognised by its TYPE rather than
        by `PendingKind`: a `MediaRef` is a pointer, and handing a pointer to the model is
        the bug this resolves. Everything else - an approval, or a refusal carrying the
        human's reason - passes through so the model can adapt instead of retrying.
        """
        payload = resolution.payload
        if not isinstance(payload, MediaRef):
            return resolution

        if profile.media.delivery is MediaDelivery.SIGNED_URL:
            resolved: object = await self._media.signed_url(payload.media_id)
        else:
            resolved = await self._media.get(payload.media_id)

        return ResolvedTool(
            tool_call_id=resolution.tool_call_id,
            approved=resolution.approved,
            payload=resolved,
        )
