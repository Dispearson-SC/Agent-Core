"""`AgentMailbox` sends with a hop count and never waits for the reply.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-02

WHY THIS TEST EXISTS
    Two properties of this port fail in production rather than in CI, and neither is
    visible to any other test in the suite.

    1. EVERY SEND CARRIES A HOP COUNT. `PeerPolicy.max_hops` is the only thing standing
       between this system and A asking B asking A forever, and unlike an ordinary
       infinite loop every turn of it is a full turn with model calls on both sides. The
       limit can only be enforced by a caller that was handed the current depth, so a
       send able to omit the depth is a send that will omit it - and the omission looks
       exactly like a first hop, which is the one value that always passes the check.
       The defence is therefore not "hop exists" but "hop is REQUIRED": no default, and
       keyword-only so it can never be filled positionally by accident.

       The guard is written over every sending member rather than over `ask` by name.
       A later `broadcast` or `forward` would be a second send, and a second send that
       forgot the depth would reopen the hole while `ask` stayed correct.

    2. THERE IS NO BLOCKING RECEIVE. The durable wait is `DBOS.recv()` in the workflow -
       that is what survives a restart, a redeploy, and a peer that suspends its own turn
       on `HumanGateway` to ask a person. A `receive()` or `wait_for_answer()` here would
       type-check, pass a unit test against an in-process fake, and then lose the turn on
       the next deploy, exactly as it would on `HumanGateway`. The absence of the member
       is the contract, so the absence is what this module asserts.

       A blocking receive also hides in a RETURN TYPE. If `ask` handed back the peer's
       answer instead of a correlation id, no implementation could satisfy it without
       waiting - the signature alone would have re-created the poll loop. So the return
       type is pinned too.

       And a sync `def` is the same defect wearing a different hat: it blocks the event
       loop for as long as the peer takes, and the peer may be waiting on a human for
       three days (D13).

    Nothing below drives behaviour. It is a lock on the contract that `t-f9-03`'s durable
    queue, `t-f9-04`'s `ask_peer` tool and `t-f9-05`'s hop limit are all built against.

    This module says nothing about believing what comes back. A peer's answer is
    untrusted content (CLAUDE.md, non-negotiable 10) and the wrapping is the runner's job.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.turn import SessionRef, TurnId
from agent_core.ports.agent_mailbox import AgentMailbox

# Every way a "just wait here for the reply" member has ever been spelled. Deliberately
# generous: the point is to make a reviewer justify a new member, not to match one name.
BLOCKING_FRAGMENTS = (
    "wait",
    "await",
    "block",
    "poll",
    "sleep",
    "join",
    "recv",
    "receive",
    "listen",
    "subscribe",
    "consume",
    "next_",
    "get_result",
    "resolve_when",
)


def _protocol_members() -> frozenset[str]:
    """The names `AgentMailbox` itself declares, without object/Protocol noise."""
    declared = getattr(AgentMailbox, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(name for name in vars(AgentMailbox) if not name.startswith("_"))


def _hints(name: str) -> dict[str, object]:
    return dict(get_type_hints(getattr(AgentMailbox, name)))


def _signature(name: str) -> inspect.Signature:
    return inspect.signature(getattr(AgentMailbox, name))


def _parameters(name: str) -> list[str]:
    return [p for p in _signature(name).parameters if p != "self"]


def _sending_members() -> list[str]:
    """Members that put a message on the wire toward a named peer.

    Identified structurally - by the presence of an `AgentId` target - rather than by a
    hard-coded name, so a member added later is caught by the hop guard the day it lands
    instead of the day it loops.
    """
    sending = []
    for name in _protocol_members():
        hints = _hints(name)
        if any(hint is AgentId for key, hint in hints.items() if key != "return"):
            sending.append(name)
    return sorted(sending)


class _SilentMailbox:
    """A mailbox with no peers configured, so it can reach nobody.

    It is also the type-level conformance check: mypy proves this satisfies the port at
    the annotated assignment in the tests below, so the port cannot drift away from the
    shape asserted here without the project-wide mypy run failing too.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[AgentId, str, int]] = []

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        return policy.peers

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
    ) -> str:
        self.sent.append((target, question, hop))
        return "corr-1"

    async def answer(self, correlation_id: str, answer: str) -> None:
        return None


@pytest.mark.phase("F9")
def test_every_send_carries_a_required_hop_count() -> None:
    """A send that can omit the depth is a send that will omit it.

    Required, `int`, and keyword-only. Optional would default the depth to a first hop,
    which is the one value `max_hops` never rejects; positional would let an argument
    shuffle put the question's own length where the depth belongs.
    """
    sending = _sending_members()

    assert sending == ["ask"], (
        "A member naming an AgentId target is a send, and every send must be under the "
        f"hop guard below. Found: {sending}. Adding one is allowed; adding one that "
        "skips the hop count is how A -> B -> A becomes unbounded."
    )

    for name in sending:
        signature = _signature(name)
        assert "hop" in signature.parameters, (
            f"AgentMailbox.{name} sends to a peer without a hop count. PeerPolicy."
            "max_hops cannot be enforced by a caller that was never told the depth, and "
            "every hop is a full turn with model calls on both sides."
        )

        hop = signature.parameters["hop"]
        assert hop.default is inspect.Parameter.empty, (
            f"AgentMailbox.{name}'s hop count has a default of {hop.default!r}. An "
            "omitted depth reads as a first hop, which always passes max_hops - the "
            "limit would exist and never fire."
        )
        assert hop.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"AgentMailbox.{name}'s hop count must be keyword-only. Positionally it sits "
            "among strings and ids, where a shuffled argument silently becomes the depth."
        )
        assert _hints(name)["hop"] is int, (
            f"AgentMailbox.{name}'s hop count is a depth, so it is an int. Comparing "
            "anything else against max_hops is a comparison that may never be true."
        )

    assert _parameters("ask") == [
        "policy",
        "target",
        "question",
        "from_session",
        "turn_id",
        "hop",
    ], (
        "ask is frozen at the policy that authorises it, the peer, the question, the "
        "originating session and turn, and the depth. The policy travels WITH the call "
        "because the check is two-sided; a mailbox that looks the policy up itself is a "
        "mailbox whose asker-side check can be skipped."
    )

    hints = _hints("ask")
    assert hints["policy"] is PeerPolicy
    assert hints["target"] is AgentId
    assert hints["question"] is str
    assert hints["from_session"] is SessionRef
    assert hints["turn_id"] is TurnId


@pytest.mark.phase("F9")
def test_protocol_exposes_no_blocking_receive() -> None:
    """The port sends and correlates. Anything that waits belongs in the workflow.

    All three halves of "blocking" are checked: no member NAMED for waiting, no member
    that is a sync `def`, and no member whose RETURN TYPE could only be produced by
    waiting.
    """
    members = sorted(_protocol_members())

    assert members == ["answer", "ask", "discover"], (
        "AgentMailbox answers one question - how does one agent ask another - through "
        f"exactly discover + ask + answer. Found: {members}. The durable wait is "
        "DBOS.recv() in adapters/driving/workflow/, not a member here."
    )

    waiting = sorted(
        name for name in members if any(fragment in name for fragment in BLOCKING_FRAGMENTS)
    )
    assert waiting == [], (
        f"AgentMailbox grew a waiting member: {waiting}. A sleep, a poll loop or a "
        "thread join here silently loses the turn on the next deploy - and the peer may "
        "itself be suspended on a human for three days."
    )

    for name in members:
        assert inspect.iscoroutinefunction(getattr(AgentMailbox, name)), (
            f"AgentMailbox.{name} is sync; ports/ is async end to end (D13). A blocking "
            "peer round-trip here stalls every other turn in the process."
        )

    assert _hints("ask")["return"] is str, (
        "ask returns a CORRELATION ID, not the peer's answer. A signature promising the "
        "answer is a blocking receive in disguise: no implementation could satisfy it "
        "without waiting, and the wait would not be durable."
    )

    assert _parameters("answer") == ["correlation_id", "answer"]
    answer_hints = _hints("answer")
    assert answer_hints["correlation_id"] is str
    assert answer_hints["answer"] is str
    assert answer_hints["return"] is type(None), (
        "answer delivers and hands nothing back. A return value here is where a caller "
        "starts waiting on it."
    )

    assert _parameters("discover") == ["policy"]
    assert _hints("discover")["policy"] is PeerPolicy
    assert _hints("discover")["return"] == tuple[AgentRef, ...], (
        "discover reports advertised capabilities as HINTS for routing. It hands back "
        "refs, never authority: a peer claiming `calendar.read` gains nothing by saying "
        "so, because its own policy decides that on its own side."
    )

    mailbox: AgentMailbox = _SilentMailbox()
    assert mailbox is not None
