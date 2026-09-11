"""The peer gate: hop limit plus the two-sided allowlist.

Phase:   F9 (foundations) / D2 (full A2A)
Tasks:   docs/TASKS.md#t-f9-05
Status:  DONE - both allowlists, both feature switches, the stricter hop limit
Tests:   Core/tests/unit/test_hop_limit.py
Callers: adapters/driven/tools/peers.py (t-f9-04) - the tool that turns a question into a hop

WHY THIS IS NOT IN THE MAILBOX ADAPTER
    `mailbox.py` enqueues, claims and correlates; it says so itself under SCOPE - THIS
    ADAPTER IS NOT THE GATE, and it persists `hop` with the row precisely so this module
    has evidence to decide on. Deciding whether a question may become a hop is a different
    question from delivering it durably, and D2 swaps the delivery (an A2A HTTP task,
    docs/TASKS.md#t-d2-04) without changing the rule. A gate living inside the transport is
    a gate that has to be rewritten when the transport is.

    It is not in `domain/peers.py` either. `PeerPolicy.may_ask` is one half of the answer
    and stays exactly as it is - this module COMPOSES it, twice, and adds the two rules
    that need both profiles in hand at once.

THE COUNT HAS TO TRAVEL
    A -> B -> A cannot be caught by counting anything either side holds locally: to B, an
    ask arriving from A is the first ask B has seen. Only a number carried on the message
    distinguishes "A started a conversation" from "A is being asked back by the agent it
    just asked", which is why `hop` is a persisted column on the queue row rather than
    something recomputed here. So `hop` is an argument, and an allowed decision hands back
    the `next_hop` the ask must travel with. If this module derived the count itself, the
    cycle would reset at every hop and run until somebody read the bill - each hop being a
    full turn with model calls on both sides, which is what makes this loop different from
    an ordinary one.

    Nothing here validates that the incoming `hop` was not lowered in transit. Between our
    own agents the queue row is the source, and against a foreign A2A peer the count comes
    over the wire; whoever terminates that wire owns authenticating it, and pretending
    otherwise here would put a security claim in a module that cannot back it.

BOTH SIDES, ALWAYS - AND THAT INCLUDES THE LIMIT
    `may_ask` is checked on the caller's policy AND on the callee's, in both directions.
    One side is never enough: consulting only the callee lets any agent that decided to
    trust B conscript B, and consulting only the caller lets whoever writes A's profile
    page anybody, including a person's personal-assistant agent.

    The same reasoning applies to `max_hops`, so the stricter of the two wins. A limit read
    off the caller alone is a limit the caller can raise, which is not a limit.

    `enabled` is read here for the reason domain/peers.py gives when it declines to read
    it: it is the feature switch rather than an identity rule, and the caller that turns a
    question into a hop is the place both it and the limit are enforced. Both defaults
    already fail closed - disabled, and nobody allowlisted.

WHAT THIS SAYS NOTHING ABOUT
    Whether the answer may be believed. A peer's reply is untrusted content whatever this
    gate decided (CLAUDE.md, non-negotiable 10); `mailbox.wrap_peer_answer` owns that.
    Sync and pure: no I/O, so nothing to await (D13).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_core.domain.peers import AgentId, PeerPolicy


class HopRefusal(StrEnum):
    """Why a question did not become a hop.

    Distinct per side rather than one `NOT_ALLOWED`: "the gate said no" is unactionable in
    an audit row, and the two allowlist refusals are fixed by editing two different
    profiles owned by two different people.
    """

    CALLER_PEERS_DISABLED = "caller_peers_disabled"
    CALLEE_PEERS_DISABLED = "callee_peers_disabled"
    HOP_LIMIT_REACHED = "hop_limit_reached"
    CALLER_DOES_NOT_ALLOW_CALLEE = "caller_does_not_allow_callee"
    CALLEE_DOES_NOT_ALLOW_CALLER = "callee_does_not_allow_caller"


@dataclass(frozen=True, slots=True)
class HopDecision:
    """The verdict, plus the count the ask travels with when there is one.

    `next_hop` is populated only on an allowed decision, so a caller cannot enqueue a hop
    it was refused by reading a field that was filled in anyway. Refusals carry a `reason`
    and no count; allowances carry a count and no reason.
    """

    allowed: bool
    reason: HopRefusal | None = None
    next_hop: int | None = None


def _refuse(reason: HopRefusal) -> HopDecision:
    return HopDecision(allowed=False, reason=reason)


def authorise_hop(
    *,
    caller: AgentId,
    caller_policy: PeerPolicy,
    callee: AgentId,
    callee_policy: PeerPolicy,
    hop: int,
) -> HopDecision:
    """May `caller` ask `callee`, given that `hop` hops have already been made?

    `hop` is the count carried by the ask being relayed - 0 for a question a human's turn
    started. The checks run in a fixed order so one cause is reported for one refusal and
    the audit row does not depend on evaluation luck: feature switches, then the limit,
    then the two allowlists. Every branch fails closed.

    Keyword-only on purpose. `(caller, caller_policy, callee, callee_policy)` is four
    arguments of two alternating types, and a positional call site that transposes a pair
    still type-checks - it would silently make the gate one-sided in the direction nobody
    tested.
    """
    if not caller_policy.enabled:
        return _refuse(HopRefusal.CALLER_PEERS_DISABLED)
    if not callee_policy.enabled:
        return _refuse(HopRefusal.CALLEE_PEERS_DISABLED)

    # The stricter side owns the limit; see BOTH SIDES, ALWAYS above.
    if hop >= min(caller_policy.max_hops, callee_policy.max_hops):
        return _refuse(HopRefusal.HOP_LIMIT_REACHED)

    if not caller_policy.may_ask(callee):
        return _refuse(HopRefusal.CALLER_DOES_NOT_ALLOW_CALLEE)
    if not callee_policy.may_ask(caller):
        return _refuse(HopRefusal.CALLEE_DOES_NOT_ALLOW_CALLER)

    return HopDecision(allowed=True, next_hop=hop + 1)
