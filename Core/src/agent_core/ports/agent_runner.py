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
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import ToolCallId, TurnId, TurnOutcome, TurnRequest


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

    def run(self, request: TurnRequest, profile: AgentProfile, history: object) -> TurnOutcome:
        """PSEUDO-CODE - F1.

        1. Build (or fetch from cache) the runtime agent for `profile`.
        2. Attach hooks: policy check + audit in before_tool_execute; usage capture in
           after_model_request.
        3. Run with `history` prepended.
        4. If the run ended with deferred requests -> TurnOutcome(pending=(...)).
           Else -> TurnOutcome(result=TurnResult(...)).

        MUST NOT raise on a model-side refusal or a policy denial: both are ordinary
        outcomes carried in the result, not exceptions. Reserve exceptions for transport
        and programming errors.

        MUST NOT generate the TurnId - it is passed in by the caller, which generated it
        inside a DBOS step. CLAUDE.md non-negotiable #2.
        """
        ...

    def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        """PSEUDO-CODE - F3.

        1. Rebuild the deferred-results structure from `resolutions`.
        2. Continue the run from where it suspended.
        3. Return the same two-shaped TurnOutcome - a resumed turn may suspend AGAIN
           (an approval that unlocks a tool whose result triggers an evidence request).
           The workflow loop must handle that; it is not an error.

        SILENT BUG TO GUARD: every resolution's `tool_call_id` must be the id Pydantic AI
        issued. A regenerated id is dropped without an exception, and the agent simply
        asks again forever. Assert the ids round-trip in an integration test.
        """
        ...
