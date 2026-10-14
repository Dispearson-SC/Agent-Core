"""Port: IntentRunner - run one piece of an intent plan on the agent runtime (D97).

Phase:   Phase 5 (fast communication), P3
Adapter: adapters/driven/agent_pydantic/runner.py (`PydanticAgentRunner`, the same runner a model
         turn uses, so a planned call goes through the very hooks a model's call does)
Per-vertical: NO - a vertical declares plans as data (an addon's `provides.intents`).

One question: carry out this part of a plan as the owning profile would. `RunIntent`
(application/run_intent.py) owns the order, the timeouts, the fallbacks, the persistence and the
audit; the workflow makes each call a recorded step.

    call_tool       ONE tool call of `profile`, with the arguments the plan resolved, through the
                    profile's tool grant, the caller's policy (a denial is a result, never a
                    raise), the wrappers, the audit row and its `tool_called` event, and the
                    profile's budget. Answers how it ended (`PlanToolResult.failure` when it did
                    not end well). It never asks a person and never runs a tool that is not
                    replay-safe: either is `tool_not_read_only` / `needs_approval`.
    compose_reply   ONE model call of `profile`, offered NO tools, that writes the reply to the
                    person's question from the results and from nothing else. A finished outcome
                    carrying the reply message, its usage and its streamed text.
    template_reply  a finished outcome carrying `text` as the agent's reply, no model call.

An adapter may raise; `RunIntent` turns a raise into a fallback to the normal path.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.intents import PlannedCall, PlanToolResult
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest

__all__ = ["IntentRunner"]


class IntentRunner(Protocol):
    async def call_tool(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, call: PlannedCall
    ) -> PlanToolResult: ...

    async def compose_reply(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        results: tuple[PlanToolResult, ...],
    ) -> TurnOutcome: ...

    def template_reply(self, turn_id: TurnId, text: str) -> TurnOutcome: ...
