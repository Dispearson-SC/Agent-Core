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
      turn-end audit. A request write that raises is not taken at its word (it may have failed
      after its commit): the store is asked whether the turn exists (`request_persisted`). Absent,
      the plan falls back (nothing was written); present, the reply follows it; unknowable (no
      check, or the check fails) the turn fails (`PlanRequestUncertainError`), because the normal
      path could write the request twice. A reply that cannot be written after it fails the turn
      (`PlanReplyNotPersistedError`). The turn-end row and the context update after it are
      independent, each logged when it fails, never fatal. A compose that fails leaves no trace in
      the conversation, so the normal path's `StartTurn` writes the request exactly once. The calls
      before it were reads; their audit rows stay, as a model's would.
    * A delegated plan (S8) audits both identities: the subject it ran as and the person.
    * The reply never invents data: `compose_reply` is told to use only the results (adapter), and
      a template only puts the results in.
    * An audit row that cannot be written is logged and never fails the turn: the plan already
      ran, and the tool calls each have their own row.

A COMPOSITE (S10, `domain/fast_path.py`)
    The fast path's composite answers SEVERAL intents in one turn: `run_composite` resolves every
    member's plan (constants and context values only: no router extracted arguments for it), then
    runs all the distinct calls concurrently, each as its owner (delegated as an ask_peer to the
    owner would run when the member carries a hop, S8) through `call` (timeout, failure codes).
    Identical calls of one owner run once and each intent reads the result. The calls start in a
    sorted order and their results are read in that order, so the step's result never depends on
    which call finished first (non-negotiable #7; the step itself is recorded). Budgets: each
    owner's `max_iterations`, and `max_composite_calls` distinct calls in all. `reply_composite`
    writes ONE reply on the composer profile (the turn's entry profile unless one is configured)
    from all the results grouped by intent, then persists the PERSON's turn exactly as `reply` does
    and audits every member (`reply_mode` `composite`). Any failure falls back: the turn runs the
    entry profile's normal path, from scratch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from agent_core.domain.fast_path import (
    MAX_COMPOSITE_CALLS,
    CompositeCall,
    CompositeMember,
    CompositeRun,
)
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
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest, delegated_identity
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.context_engine import ContextEngine
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.intent_runner import IntentRunner
from agent_core.ports.route_audit import RouteAudit

__all__ = [
    "DEFAULT_TOOL_TIMEOUT_MS",
    "PlanReplyNotPersistedError",
    "PlanRequestUncertainError",
    "PreparedPlan",
    "RequestPersisted",
    "RunIntent",
]

RequestPersisted = Callable[[TurnId], Awaitable[bool]]
"""Whether a turn's request is in the store. Composition binds it to the store's own table, like
`DecideApproval`'s requester seat: one read the composition root can answer, not a port member."""

_LOG = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_MS = 20_000
"""One planned call's bound. A read that takes longer is better answered by the normal path."""


def _today() -> date:
    return datetime.now(UTC).date()


class PlanReplyNotPersistedError(RuntimeError):
    """The planned reply could not be written after the turn's request was. The turn fails: the
    normal path cannot start it again without writing the request twice."""


class PlanRequestUncertainError(RuntimeError):
    """The request write raised and nothing can tell whether it committed. The turn fails: falling
    back could write the request twice, carrying on could answer a turn with no request."""


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
        request_persisted: RequestPersisted | None = None,
        composer: str | None = None,
        max_composite_calls: int = MAX_COMPOSITE_CALLS,
    ) -> None:
        if tool_timeout_ms < 1:
            raise ValueError(f"the plan tool timeout must be at least 1 ms, got {tool_timeout_ms}")
        if max_composite_calls < 1:
            raise ValueError("a composite must be allowed at least one call")
        self._runner = runner
        self._store = store
        self._audit = audit
        self._routes = routes
        self._context = context
        self._profiles = profiles
        self._plans = plans
        self._timeout = tool_timeout_ms / 1000
        self._today = today
        self._request_persisted = request_persisted
        # S10. The profile that writes a composite's reply (None: the turn's entry profile).
        self._composer = composer
        self._max_composite_calls = max_composite_calls

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
        if not await self._persist(turn_id, request, outcome):
            return None
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

    async def _persist(self, turn_id: TurnId, request: TurnRequest, outcome: TurnOutcome) -> bool:
        """Persist an answered turn: request, reply, then the bookkeeping. False when nothing was
        persisted (no result, or the request certainly not written): the caller falls back."""
        if outcome.result is None:
            return False
        # The request is one transaction: when it fails nothing of the turn exists and the normal
        # path starts it from scratch. Once it is written, the normal path would write it twice, so
        # a reply that cannot follow it fails the turn with a named error instead (slice 36).
        if not await self._write_request(turn_id, request):
            return False
        try:
            await self._store.append_outcome(turn_id, outcome)
        except Exception as error:
            raise PlanReplyNotPersistedError(
                f"the planned reply of turn {turn_id} could not be persisted after its request "
                f"was: {type(error).__name__}"
            ) from error
        # The turn answered; what follows is bookkeeping, each part independent, never fatal.
        usage = outcome.result.usage
        try:
            self._context.update_from_response(request.session, usage)
        except Exception:
            _LOG.warning("could not update the context of turn %s", turn_id, exc_info=True)
        try:
            await self._audit.record_turn_end(turn_id, usage, usage.cost_usd)
        except Exception:
            _LOG.warning("could not audit the end of turn %s", turn_id, exc_info=True)
        return True

    # --- S10: a composite ---------------------------------------------------------------------

    async def run_composite(
        self, turn_id: TurnId, request: TurnRequest, composite: CompositeCall
    ) -> CompositeRun:
        """Every member's calls, concurrently and deduplicated; the results grouped by intent, or
        the reason the composite cannot answer."""
        context = {
            "today": self._today().isoformat(),
            "channel": request.caller.channel,
            "tenant_id": str(request.caller.tenant_id),
        }
        planned: list[tuple[CompositeMember, tuple[PlannedCall, ...]]] = []
        for member in composite.members:
            try:
                plan = self._plans[member.intent]
                if plan.profile_id != member.profile_id:
                    raise _PlanMismatchError(member.intent)
                owner = self._profiles[member.profile_id]
            except (KeyError, _PlanMismatchError) as error:
                return CompositeRun(fallback=error_reason(error))
            try:
                calls = tuple(resolve_arguments(step, {}, context) for step in plan.steps)
            except PlanArgumentError:
                return CompositeRun(fallback=PLAN_ARGUMENT_INVALID)
            if len(calls) > owner.max_iterations:
                return CompositeRun(fallback=PLAN_OVER_BUDGET)
            planned.append((member, calls))
        unique: dict[tuple[str, str, str], tuple[CompositeMember, PlannedCall]] = {}
        for member, calls in planned:
            for call in calls:
                unique.setdefault(_call_key(member, call), (member, call))
        if len(unique) > self._max_composite_calls:
            return CompositeRun(fallback=PLAN_OVER_BUDGET)
        order = sorted(unique)  # never the order calls finish in (non-negotiable #7)
        results = await asyncio.gather(
            *(
                self.call(turn_id, _member_request(request, unique[key][0]), unique[key][1])
                for key in order
            )
        )
        by_key = dict(zip(order, results, strict=True))
        failure = next((r.failure for r in results if r.failure is not None), None)
        if failure is not None:
            return CompositeRun(fallback=failure, tool_count=len(order))
        groups = tuple(
            (
                member.intent,
                tuple(
                    replace(by_key[_call_key(member, call)], step_id=call.step_id)
                    for call in calls
                ),
            )
            for member, calls in planned
        )
        return CompositeRun(groups=groups, tool_count=len(order))

    async def reply_composite(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        composite: CompositeCall,
        run: CompositeRun,
    ) -> TurnOutcome | None:
        """ONE reply from every group, then the person's turn persisted; None when it could not be
        written (nothing persisted: the caller falls back)."""
        mode = "composite"
        try:
            composer = self._profiles[self._composer or request.profile_id]
            outcome = await self._runner.compose_composite(turn_id, request, composer, run.groups)
        except Exception:
            _LOG.warning(
                "the composite reply of turn %s failed; the turn falls back",
                turn_id,
                exc_info=True,
            )
            return None
        if not await self._persist(turn_id, request, outcome):
            return None
        counts = {intent: len(results) for intent, results in run.groups}
        for member in composite.members:
            await self._record(
                turn_id,
                request,
                IntentRecord(
                    intent=member.intent,
                    profile_id=member.profile_id,
                    plan_used=True,
                    tool_count=counts.get(member.intent, 0),
                    reply_mode=mode,
                    **_member_identities(request, member),
                ),
            )
        return outcome

    async def fall_back_composite(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        composite: CompositeCall,
        reason: str,
        tool_count: int,
    ) -> None:
        """Audit a composite that did not answer; the caller then runs the normal path."""
        for member in composite.members:
            await self._record(
                turn_id,
                request,
                IntentRecord(
                    intent=member.intent,
                    profile_id=member.profile_id,
                    plan_used=False,
                    tool_count=tool_count,
                    reply_mode="composite",
                    fallback_reason=reason,
                    **_member_identities(request, member),
                ),
            )

    async def _write_request(self, turn_id: TurnId, request: TurnRequest) -> bool:
        """Write the turn's request: True when it is in the store, False when it certainly is not
        (the plan falls back). Raises `PlanRequestUncertainError` when nothing can tell."""
        try:
            await self._store.append_request(turn_id, request)
            return True
        except Exception as error:
            failure = error
        _LOG.warning(
            "the request write of turn %s raised; asking the store whether it committed",
            turn_id,
            exc_info=True,
        )
        if self._request_persisted is not None:
            try:
                return await self._request_persisted(turn_id)
            except Exception:
                _LOG.warning("could not read back the request of turn %s", turn_id, exc_info=True)
        raise PlanRequestUncertainError(
            f"the request of turn {turn_id} may or may not be persisted after "
            f"{type(failure).__name__}; the turn cannot fall back safely"
        ) from failure

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


def _call_key(member: CompositeMember, call: PlannedCall) -> tuple[str, str, str]:
    """What makes two planned calls the same call: the owner, the tool and the arguments."""
    arguments = json.dumps(dict(call.arguments), sort_keys=True, default=str, ensure_ascii=False)
    return member.profile_id, call.tool, arguments


def _member_request(request: TurnRequest, member: CompositeMember) -> TurnRequest:
    """The request a member's calls run with: its owner's, delegated (S8) when it carries a hop."""
    if member.hop is None:
        return replace(request, profile_id=member.profile_id)
    return replace(
        request,
        profile_id=member.profile_id,
        caller=delegated_identity(request.caller),
        hop=member.hop,
    )


def _member_identities(request: TurnRequest, member: CompositeMember) -> dict[str, str | None]:
    """Both identities of a member that ran delegated; none when it ran as the person."""
    if member.hop is None:
        return {"actor": None, "on_behalf_of": None}
    return _identities(_member_request(request, member))
