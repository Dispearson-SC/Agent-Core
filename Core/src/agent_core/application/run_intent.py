"""Use case: RunIntent - answer a turn with its intent's declared plan (D97, S3/S6 layer B).

Phase:   Phase 5 (fast communication), P3
Tests:   Core/tests/unit/test_run_intent.py

LAYER RULE
    Imports domain/ and ports/ only (and the standard library). Testable with fakes.

WHO CALLS IT
    `adapters/driving/workflow/turn_workflow.py`, one recorded step per method call:
    `_step_intent_prepare`, one `_step_intent_tool` per planned call, `_step_intent_reply`, and
    `_step_intent_fallback` when the plan gives up. So a resumed turn never re-runs a call or a
    compose that already finished: their results are read back from the operation log.

WHAT IT GUARANTEES
    * The plan runs as its OWNING profile (the profile the route step already made the turn's),
      and every call goes through the runner's own hooks: the tool grant, the caller's policy,
      the wrappers, the audit row and its `tool_called` event, the profile's budget.
    * Arguments are resolved before the first call: a plan that cannot run (an argument the router
      did not extract, more calls than the profile's `max_iterations`) runs nothing.
    * Nothing here fails the turn. A call that raises or outlives `tool_timeout_ms` is a failed
      call (`error:<Class>`, `timeout`), and the workflow then runs the normal model path from
      scratch, with the reason audited (`fall_back`).
    * The turn is persisted ONLY when the reply exists: the request, the reply message, the
      turn-end audit. A request that cannot be written falls back (nothing was written); a reply
      that cannot be written after it fails the turn (`PlanReplyNotPersistedError`); the turn-end
      row and the context update after it are logged when they fail, never fatal. A compose that
      fails leaves no trace in the conversation, so the normal path's `StartTurn` writes the
      request exactly once. The calls before it were reads; their
      audit rows stay, as a model's would.
    * A delegated plan (S8) audits both identities: the subject it ran as and the person.
    * The reply never invents data: `compose_reply` is told to use only the results (adapter), and
      a template only puts the results in.
    * An audit row that cannot be written is logged and never fails the turn: the plan already
      ran, and the tool calls each have their own row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from agent_core.domain.intents import (
    PLAN_ARGUMENT_INVALID,
    PLAN_OVER_BUDGET,
    PLAN_TIMEOUT,
    IntentCall,
    IntentPlan,
    IntentRecord,
    PlanArgumentError,
    PlannedCall,
    PlanToolResult,
    ReplyMode,
    render_template,
    resolve_arguments,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import error_reason
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.context_engine import ContextEngine
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.intent_runner import IntentRunner
from agent_core.ports.route_audit import RouteAudit

__all__ = ["DEFAULT_TOOL_TIMEOUT_MS", "PlanReplyNotPersistedError", "PreparedPlan", "RunIntent"]

_LOG = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_MS = 20_000
"""One planned call's bound. A read that takes longer is better answered by the normal path."""


def _today() -> date:
    return datetime.now(UTC).date()


class PlanReplyNotPersistedError(RuntimeError):
    """The planned reply could not be written after the turn's request was. The turn fails: the
    normal path cannot start it again without writing the request twice."""


class _PlanMismatchError(LookupError):
    """The plan's owner is not the profile the turn runs under (a route step from another build)."""


@dataclass(frozen=True, slots=True)
class PreparedPlan:
    """The calls a plan will make, resolved, or why it will not run (`fallback`)."""

    calls: tuple[PlannedCall, ...] = ()
    reply: ReplyMode = ReplyMode.COMPOSE
    fallback: str | None = None


class RunIntent:
    def __init__(
        self,
        *,
        runner: IntentRunner,
        store: ConversationStore,
        audit: AuditSink,
        routes: RouteAudit,
        context: ContextEngine,
        profiles: Mapping[str, AgentProfile],
        plans: Mapping[str, IntentPlan],
        tool_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS,
        today: Callable[[], date] = _today,
    ) -> None:
        if tool_timeout_ms < 1:
            raise ValueError(f"the plan tool timeout must be at least 1 ms, got {tool_timeout_ms}")
        self._runner = runner
        self._store = store
        self._audit = audit
        self._routes = routes
        self._context = context
        self._profiles = profiles
        self._plans = plans
        self._timeout = tool_timeout_ms / 1000
        self._today = today

    async def prepare(
        self, turn_id: TurnId, request: TurnRequest, call: IntentCall
    ) -> PreparedPlan:
        """Every call of the plan with its arguments resolved, or the reason it cannot run."""
        try:
            plan, profile = self._plan_and_profile(request, call)
        except (KeyError, _PlanMismatchError) as error:
            return PreparedPlan(fallback=error_reason(error))
        context = {
            "today": self._today().isoformat(),
            "channel": request.caller.channel,
            "tenant_id": str(request.caller.tenant_id),
        }
        try:
            calls = tuple(resolve_arguments(s, call.arguments, context) for s in plan.steps)
        except PlanArgumentError:
            return PreparedPlan(reply=plan.reply, fallback=PLAN_ARGUMENT_INVALID)
        if len(calls) > profile.max_iterations:
            return PreparedPlan(reply=plan.reply, fallback=PLAN_OVER_BUDGET)
        return PreparedPlan(calls=calls, reply=plan.reply)

    async def call(
        self, turn_id: TurnId, request: TurnRequest, planned: PlannedCall
    ) -> PlanToolResult:
        """One planned call as the turn's profile. A raise or a timeout is a failed call."""
        profile = self._profiles[request.profile_id]
        try:
            return await asyncio.wait_for(
                self._runner.call_tool(turn_id, request, profile, planned), self._timeout
            )
        except TimeoutError:
            return PlanToolResult(planned.step_id, planned.tool, failure=PLAN_TIMEOUT)
        except Exception as error:
            _LOG.warning(
                "planned call %r of turn %s failed; the turn falls back",
                planned.tool,
                turn_id,
                exc_info=True,
            )
            return PlanToolResult(planned.step_id, planned.tool, failure=error_reason(error))

    async def reply(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        call: IntentCall,
        results: tuple[PlanToolResult, ...],
    ) -> TurnOutcome | None:
        """The reply, written then persisted, or None when it could not be written (nothing was
        persisted: the caller falls back to the normal path)."""
        try:
            plan, profile = self._plan_and_profile(request, call)
            if plan.reply is ReplyMode.TEMPLATE and plan.template is not None:
                text = render_template(plan.template, {r.step_id: r.content for r in results})
                outcome = self._runner.template_reply(turn_id, text)
            else:
                outcome = await self._runner.compose_reply(turn_id, request, profile, results)
        except Exception:
            _LOG.warning(
                "the reply of intent %r for turn %s failed; the turn falls back",
                call.intent,
                turn_id,
                exc_info=True,
            )
            return None
        if outcome.result is None:
            return None
        # The request is one transaction: when it fails nothing of the turn exists and the normal
        # path starts it from scratch. Once it is written, the normal path would write it twice, so
        # a reply that cannot follow it fails the turn with a named error instead (slice 36).
        try:
            await self._store.append_request(turn_id, request)
        except Exception:
            _LOG.warning(
                "could not persist the request of turn %s; the turn falls back",
                turn_id,
                exc_info=True,
            )
            return None
        try:
            await self._store.append_outcome(turn_id, outcome)
        except Exception as error:
            raise PlanReplyNotPersistedError(
                f"the planned reply of turn {turn_id} could not be persisted after its request "
                f"was: {type(error).__name__}"
            ) from error
        # The turn answered; what follows is bookkeeping and never fails it.
        try:
            self._context.update_from_response(request.session, outcome.result.usage)
            await self._audit.record_turn_end(
                turn_id, outcome.result.usage, outcome.result.usage.cost_usd
            )
        except Exception:
            _LOG.warning(
                "could not account the planned reply of turn %s", turn_id, exc_info=True
            )
        await self._record(
            turn_id,
            request,
            IntentRecord(
                intent=call.intent,
                profile_id=request.profile_id,
                plan_used=True,
                tool_count=len(results),
                reply_mode=plan.reply.value,
                **_identities(request),
            ),
        )
        return outcome

    async def fall_back(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        call: IntentCall,
        reason: str,
        tool_count: int,
    ) -> None:
        """Audit a plan that did not answer the turn; the caller then runs the normal path."""
        plan = self._plans.get(call.intent)
        await self._record(
            turn_id,
            request,
            IntentRecord(
                intent=call.intent,
                profile_id=request.profile_id,
                plan_used=False,
                tool_count=tool_count,
                reply_mode=None if plan is None else plan.reply.value,
                fallback_reason=reason,
                **_identities(request),
            ),
        )

    def _plan_and_profile(
        self, request: TurnRequest, call: IntentCall
    ) -> tuple[IntentPlan, AgentProfile]:
        plan = self._plans[call.intent]
        if plan.profile_id != request.profile_id:
            raise _PlanMismatchError(call.intent)
        return plan, self._profiles[plan.profile_id]

    async def _record(self, turn_id: TurnId, request: TurnRequest, record: IntentRecord) -> None:
        try:
            await self._routes.record_intent(turn_id, str(request.session.tenant_id), record)
        except Exception:
            _LOG.warning("could not audit the intent plan of turn %s", turn_id, exc_info=True)


def _identities(request: TurnRequest) -> dict[str, str | None]:
    """Both identities of a delegated turn for its audit row (S8); none for a person's own turn."""
    person = request.caller.on_behalf_of
    if person is None:
        return {"actor": None, "on_behalf_of": None}
    return {"actor": request.caller.subject_id, "on_behalf_of": person.subject_id}
