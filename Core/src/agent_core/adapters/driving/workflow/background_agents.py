"""Driving adapter: the background-agent tools' seat over the mailbox and the workflow engine.

Phase:   Agent-comms v2, C20 (D50)
Tests:   Core/tests/unit/test_background_agents.py
Seat:    adapters/driven/tools/background.py (`BackgroundAgents`)

It lives in the workflow package because stopping a child cancels its workflow, and `dbos` may be
imported nowhere else (CLAUDE.md non-negotiable #1). The stop itself is
`turn_workflow.stop_background_ask`, the C4 cascade.

SCOPE IS THE FIRST THING EVERY VERB DOES. The mailbox returns the background asks of the OWNER
conversation (tenant and session id, in SQL), and a handle is looked up in that list and nowhere
else: a handle from another conversation is indistinguishable from one that never existed, and no
verb below acts on a row it did not find there.

`message` IS A NEW BACKGROUND ASK THAT CONTINUES THE CHILD'S SESSION. A running turn cannot be
interrupted (it is one recorded model step at a time), so a message is delivered the way every ask
is: as a turn of the child, in the child's own peer session, which the queue partition runs after
the turn already in flight. That is the "queued for the child's next step" the design allowed, and
the `steer` / `follow_up` difference is the framing the child reads. It goes through the same gate
as any ask: both profiles' `enabled` switches and allowlists and the hop limit (`authorise_hop`),
the asker's `allow_background` and `max_background_asks`, and the mailbox's ownership check of the
session (same tenant, case, asking agent and target). Its answer comes back as one more follow-up
turn of the conversation, tagged with the new handle. Idempotent per tool call (`call_id`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from agent_core.adapters.driven.peers.hop_limit import authorise_hop
from agent_core.adapters.driven.tools.background import (
    BackgroundOwner,
    MessageMode,
    MessageResult,
    StopResult,
)
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import (
    BACKGROUND_LIST_LIMIT,
    AgentId,
    BackgroundAsk,
    BackgroundKeys,
    BackgroundState,
    PeerContinuationRefusedError,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId
from agent_core.ports.agent_mailbox import AgentMailbox

__all__ = ["WorkflowBackgroundAgents"]

_STOP_REASON: Final[str] = "stopped by the conversation that started it (peer_stop)"

# What the child reads, per mode. OUR voice: the asking agent wrote the text, and it arrives as an
# ordinary ask (the question of a peer turn).
_FRAMING: Final[dict[MessageMode, str]] = {
    MessageMode.STEER: (
        "Steering note from the agent that asked you. It changes how to do the work you were "
        "given:\n{text}"
    ),
    MessageMode.FOLLOW_UP: (
        "Follow-up from the agent that asked you. It adds to what you were given:\n{text}"
    ),
}

_NOT_FOUND: Final[str] = "no background agent with that handle exists in this conversation"
_STOPPED: Final[str] = "that background agent was stopped, so it cannot be messaged"
_NOT_ALLOWED: Final[str] = "this agent is not allowed to run asks in the background"
_CAP: Final[str] = (
    "this conversation already has {limit} background asks running, which is the most it may have: "
    "wait for one to finish or stop one"
)
_GATE: Final[str] = "the message was refused by the peer gate: {reason}"
_SESSION: Final[str] = "the child's session cannot be continued any more"
_IN_FOLLOW_UP: Final[str] = (
    "this turn is itself the follow-up that carried a background answer back, and a follow-up "
    "may not start another background ask (that would chain them without end)"
)
_NO_ASKER_PROFILE: Final[str] = (
    "the asking agent's profile {profile!r} is not loaded, so the peer gate cannot run"
)
_UNKNOWN_PEER: Final[str] = "no profile named {target!r} is loaded, so the peer gate cannot run"


class WorkflowBackgroundAgents:
    """`BackgroundAgents` over an `AgentMailbox` and the profiles the gate needs."""

    __slots__ = ("_mailbox", "_profiles")

    def __init__(self, mailbox: AgentMailbox, profiles: Mapping[str, AgentProfile]) -> None:
        self._mailbox = mailbox
        self._profiles = profiles

    async def list(
        self, owner: BackgroundOwner, *, limit: int = BACKGROUND_LIST_LIMIT
    ) -> tuple[BackgroundAsk, ...]:
        return await self._mailbox.background_asks(owner.session, limit=limit)

    async def count(self, owner: BackgroundOwner) -> int:
        return await self._mailbox.count_background(owner.session)

    async def status(self, owner: BackgroundOwner, handle: str) -> BackgroundAsk | None:
        # By handle in the store, scoped to the conversation: not a search of a listing window.
        return await self._mailbox.background_ask(owner.session, handle)

    async def stop(self, owner: BackgroundOwner, handle: str) -> StopResult:
        ask = await self.status(owner, handle)
        if ask is None:
            return StopResult.UNKNOWN
        if not ask.is_open:
            return StopResult.NOT_RUNNING
        if await turn_workflow.stop_background_ask(handle, _STOP_REASON):
            return StopResult.STOPPED
        return StopResult.NOT_RUNNING  # it answered between the listing and the cancel

    async def message(
        self,
        owner: BackgroundOwner,
        handle: str,
        text: str,
        mode: MessageMode,
        *,
        call_id: str,
    ) -> MessageResult:
        child = await self.status(owner, handle)
        if child is None or child.session is None:
            return MessageResult(refusal=_NOT_FOUND)
        if child.state is BackgroundState.CANCELLED:
            return MessageResult(refusal=_STOPPED)
        if await self._mailbox.is_background_followup(owner.session, owner.turn_id):
            return MessageResult(refusal=_IN_FOLLOW_UP)
        asker = self._profiles.get(owner.profile_id)
        policy = None if asker is None else asker.peers
        callee = self._profiles.get(child.target)
        if policy is None:
            return MessageResult(refusal=_NO_ASKER_PROFILE.format(profile=owner.profile_id))
        if callee is None:
            return MessageResult(refusal=_UNKNOWN_PEER.format(target=child.target))
        if not policy.allow_background:
            return MessageResult(refusal=_NOT_ALLOWED)
        decision = authorise_hop(
            caller=AgentId(owner.profile_id),
            caller_policy=policy,
            callee=AgentId(child.target),
            callee_policy=callee.peers,
            hop=max(child.hop - 1, 0),  # the hops made BEFORE the child's own ask
        )
        if not decision.allowed or decision.next_hop is None:
            reason = decision.reason.value if decision.reason else "refused"
            return MessageResult(refusal=_GATE.format(reason=reason))
        # Counted in the store, leaving out this very message if an earlier attempt of the call
        # already sent it: the tool is idempotent per call, so the replay must not meet its own row.
        running = await self._mailbox.count_background(
            owner.session,
            open_only=True,
            excluding=BackgroundKeys(owner.turn_id, (f"msg.{call_id}",)),
        )
        if running >= policy.max_background_asks:
            return MessageResult(refusal=_CAP.format(limit=policy.max_background_asks))
        try:
            sent = await self._mailbox.ask(
                policy,
                AgentId(child.target),
                _FRAMING[mode].format(text=text),
                from_session=owner.session,
                turn_id=TurnId(owner.turn_id),
                hop=decision.next_hop,
                asker=AgentId(owner.profile_id),
                request_key=f"msg.{call_id}",
                continue_session=child.session,
                root_turn_id=child.root_turn_id if child.root_turn_id else owner.turn_id,
                background_caller=owner.caller,
            )
        except PeerContinuationRefusedError:
            return MessageResult(refusal=_SESSION)
        return MessageResult(handle=sent)
