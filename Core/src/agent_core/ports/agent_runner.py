"""Port: AgentRunner - how do I run a turn, and how do I resume it?

Phase:      F0 (fake) / F1 (real adapter)
Tasks:      docs/TASKS.md#t-f1-05
Adapter:    adapters/driven/agent_pydantic/
Per-vertical: NO

WHY THIS PORT EXISTS - READ BEFORE WIDENING IT
    So use cases and tests do not import Pydantic AI. That is ALL. It does NOT exist to
    let us swap frameworks later: that portability is not real, and chasing it produces a
    bloated abstraction that reimplements half a framework. docs/DECISIONS.md#d7.

    The port therefore MIRRORS Pydantic AI's own contract shape on purpose - run returns
    a result or deferred requests; resume takes decisions. Do not invent a parallel
    vocabulary.

    EARLY WARNING: if the adapter starts reimplementing message handling, retries, or
    streaming assembly, this port has grown too big. Trim it.

WHAT THE ADAPTER OWNS (and this port must never leak)
    - Building the Pydantic AI Agent from an AgentProfile.
    - Registering the seven lifecycle hooks. `before_tool_execute` is where ToolPolicy is
      consulted and AuditSink is written BEFORE any side effect; it blocks by raising
      SkipToolExecution(result).
    - Translating DeferredToolRequests into domain PendingRequest values.
    - Wrapping untrusted results (mcp_*, web, media) in delimiters.

WHY `run` AND `resume` ARE ASYNC (D13)
    They are the longest I/O in the system: a model call of roughly 5-40 seconds, during
    which the process must keep serving every other turn. D13 also records that Pydantic AI
    is async-native - `run()` is the coroutine and `run_sync()` is the wrapper around it -
    so a synchronous port here would force the adapter to bridge back the wrong way, which
    is precisely where the deadlocks D13 names live.

    This port mirrors Pydantic AI's contract shape on purpose (D7). Its shape is a
    coroutine; mirroring it means being one too.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
)


class ToolResolution(Protocol):
    """What a human decided, or supplied, for one suspended tool call.

    `approved=False` means the tool must NOT run; `payload` then carries the reason shown
    to the model. For an EVIDENCE request, `approved=True` and `payload` carries the
    MediaRef the model receives as the tool result."""

    tool_call_id: ToolCallId
    approved: bool
    payload: object | None


class AgentRunner(Protocol):
    """Runs one turn against a model, with tools already policy-filtered."""

    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:
        """PSEUDO-CODE - F1.

        ASYNC (D13): wraps the model call, the single longest await in a turn.

        1. Build (or fetch from cache) the runtime agent for `profile`.
        2. Attach hooks: policy check + audit in before_tool_execute; usage capture in
           after_model_request.
        3. Run with `history` prepended.
        4. If the run ended with deferred requests -> TurnOutcome(pending=(...)).
           Else -> TurnOutcome(result=TurnResult(...)).

        MUST NOT raise on a model-side refusal or a policy denial: both are ordinary
        outcomes carried in the result, not exceptions. Reserve exceptions for transport
        and programming errors.

        MUST NOT generate the TurnId - it arrives as the FIRST parameter, from the caller
        that generated it inside a DBOS step. CLAUDE.md non-negotiable #2.

        WHY `turn_id` LEADS, AND WHY IT IS NOT OPTIONAL
            `TurnOutcome` cannot be built without one and every audit row the adapter
            writes is filed under one. A `run` that could not be handed the identifier its
            own return type requires was a port cut, not an adapter inconvenience: the
            adapter closed the gap with a `for_turn` pre-binding and an "unbound" error
            type, i.e. a second way to call the port that the port never described.
            Widening it here deletes both. It leads for the same reason it leads on
            `resume`: the two members answer the same question about the same turn.
        """
        ...

    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
        *,
        caller: CallerIdentity,
        session: SessionRef,
    ) -> TurnOutcome:
        """PSEUDO-CODE - F3.

        ASYNC (D13): continues the same model call `run` started. Same reasoning, and the
        resumed run may issue several more model requests before it settles.

        1. Rebuild the deferred-results structure from `resolutions`.
        2. Continue the run from where it suspended, with the SAME enforcement `run`
           attaches - see the seat note below.
        3. Return the same two-shaped TurnOutcome - a resumed turn may suspend AGAIN
           (an approval that unlocks a tool whose result triggers an evidence request).
           The workflow loop must handle that; it is not an error.

        SILENT BUG TO GUARD: every resolution's `tool_call_id` must be the id Pydantic AI
        issued. A regenerated id is dropped without an exception, and the agent simply
        asks again forever. Assert the ids round-trip in an integration test.

        WHY `caller` AND `session` ARE HERE, AND WHY THEY ARE NOT OPTIONAL (t-f3-16)
            A resume is not one tool call. The call a human authorised is the FIRST of
            them, and the model may then ask for more on the same continuation. Enforcing
            anything about those later calls needs two things the port used to withhold:
            `ToolPolicy.load_rules(caller)` needs the caller, and compaction is attached
            per SESSION. Without them the adapter could attach no policy check, no audit
            write and no compaction - so every further tool call after a resume ran
            unwatched, and the tenant-narrowed knowledge tool disappeared for the same
            reason. Nothing failed; the turn simply stopped being governed.

            `run` receives both inside `TurnRequest`. `resume` has no request to carry
            them - a resume adds no user input - so they arrive as their own seats.

            This is the SECOND deliberate widening of this frozen port and it is the same
            class as the first (see docs/TASKS.md#t-f1-05): a port that cannot be handed
            what its own documented behaviour requires is itself the defect. Fix the port,
            not the adapter. The regression lock moves with it - the PRE-widening `resume`
            signature is what tests/unit/test_runner_resume_identity.py rejects, so the
            widening cannot be quietly undone.

            KEYWORD-ONLY on purpose. Added positionally after `turn_id`, an un-updated
            call site would bind `profile` to `caller` and fail far away from the mistake;
            keyword-only makes such a call refuse at the boundary instead.
        """
        ...
