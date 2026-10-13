"""The eval scenarios (C32, D67).

Each scenario drives a multi-agent flow through the PRODUCTION runner (`PydanticAgentRunner`, the
real `ask_peer` tool, the real untrusted-content wrapping) with in-memory ports, and returns the
checks the outcome must pass. The durable workflow (DBOS, Postgres) is not part of an eval: it is
covered by the integration suite, and an eval is about what the MODEL does with the tools and the
answers it is given. The hand-off the workflow does between turns (run the asked agent, resume the
asker with its answer as a `<peer>` envelope) is done here in a few lines, in-process.

Against a real model the checks must hold for most repetitions, not necessarily every one; that is
what the pass rate in the report is for. Against the scripted model every repetition is identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.fakes import ports as fakes
from tests.fakes.scripted_model import ScriptedModel, text, tool_call

from agent_core.adapters.driven.agent_pydantic.runner import PydanticAgentRunner
from agent_core.adapters.driven.tools.peers import build_toolset
from agent_core.domain.peer_result import PeerResult, PeerStatus
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnRequest,
    UserInput,
)
from evals.harness import CheckResult, ModelSource, Outcome, Scenario

__all__ = ["SCENARIOS"]

TENANT = TenantId("eval")
CALLER = CallerIdentity(subject_id="eval-user", channel="eval", tenant_id=TENANT)
FRONT_DESK, INVENTORY = "front_desk", "inventory"
FACT = "17"


@dataclass
class _Resolution:
    """What the workflow hands `runner.resume` for a finished delegation (not frozen: the port's
    `ToolResolution` protocol declares writable attributes)."""

    tool_call_id: ToolCallId
    approved: bool
    payload: object | None


def _profile(source: ModelSource, agent: str, persona: str, asks: tuple[str, ...]) -> AgentProfile:
    return AgentProfile(
        id=agent,
        persona=persona,
        model=source.profile_model(agent),
        description=f"The {agent.replace('_', ' ')}.",
        peers=PeerPolicy(
            enabled=True,
            peers=tuple(AgentRef(agent_id=AgentId(a), display_name=a) for a in asks),
        ),
    )


def _runner(source: ModelSource, profiles: dict[str, AgentProfile]) -> PydanticAgentRunner:
    return PydanticAgentRunner(
        model=fakes.FakeModelGateway(),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=source.build,
        peer_profiles=profiles,
    )


def _request(agent: str, message: str, session: str) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TENANT),
        caller=CALLER,
        profile_id=agent,
        input=UserInput(text=message),
    )


async def _front_desk_asks_inventory(source: ModelSource) -> Outcome:
    """A customer asks the front desk about stock. The front desk must ask the inventory agent
    (it knows no stock levels itself), and its answer to the customer must carry the number the
    inventory agent gave."""
    profiles = {
        FRONT_DESK: _profile(
            source,
            FRONT_DESK,
            "You are the front desk of a shop. You do not know stock levels. For any question "
            "about how many units are in stock you MUST call the ask_peer tool to ask the "
            "inventory agent, then tell the customer what it answered, in one short sentence.",
            asks=(INVENTORY,),
        ),
        INVENTORY: _profile(
            source,
            INVENTORY,
            "You are the inventory desk. Stock levels right now: SKU-42 has 17 units, SKU-7 has "
            "0 units. Answer the question in one short sentence that states the number.",
            asks=(FRONT_DESK,),
        ),
    }
    runner = _runner(source, profiles)
    asked = await runner.run(
        TurnId("eval-turn-1"),
        _request(FRONT_DESK, "How many units of SKU-42 do we have in stock?", "eval-case"),
        profiles[FRONT_DESK],
        None,
    )
    delegations = [p for p in asked.pending if p.kind is PendingKind.DELEGATION]
    checks = [
        CheckResult(
            "front desk delegates to inventory",
            len(delegations) == 1
            and delegations[0].tool_name == "ask_peer"
            and delegations[0].arguments.get("target") == INVENTORY,
            f"pending: {[(p.tool_name, p.arguments.get('target')) for p in asked.pending]}",
        )
    ]
    if len(delegations) != 1:
        return Outcome(tuple(checks))
    pending = delegations[0]

    question = str(pending.arguments.get("question", ""))
    answered = await runner.run(
        TurnId("eval-turn-2"), _request(INVENTORY, question, "eval-peer"), profiles[INVENTORY], None
    )
    specialist = answered.result.text if answered.result is not None else ""
    checks.append(CheckResult("inventory states the stock level", FACT in specialist, specialist))

    envelope = PeerResult(peer=INVENTORY, status=PeerStatus.OK, text=specialist).render()
    resumed = await runner.resume(
        TurnId("eval-turn-1"),
        profiles[FRONT_DESK],
        list(asked.messages),
        (_Resolution(pending.tool_call_id, True, envelope),),
        caller=CALLER,
        session=_request(FRONT_DESK, "", "eval-case").session,
    )
    final = resumed.result.text if resumed.result is not None else ""
    checks.append(CheckResult("front desk relays the stock level", FACT in final, final))
    return Outcome(tuple(checks))


def _front_desk_scripts() -> dict[str, Any]:
    return {
        FRONT_DESK: ScriptedModel().push(
            tool_call(
                "ask_peer",
                {"target": INVENTORY, "question": "How many units of SKU-42 are in stock?"},
                id="call-1",
            ),
            text("We have 17 units of SKU-42 in stock."),
        ),
        INVENTORY: ScriptedModel().push(text("SKU-42 has 17 units in stock.")),
    }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="front_desk_asks_inventory",
        description=(
            "A customer asks the front desk how many units of SKU-42 are in stock. The front "
            "desk must delegate to the inventory agent with ask_peer and relay its number."
        ),
        run=_front_desk_asks_inventory,
        scripts=_front_desk_scripts,
    ),
)
