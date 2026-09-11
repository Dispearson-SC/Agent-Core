"""Driven adapter: `ask_peer` - the deferred-tool mechanism for agent-to-agent asks.

Phase:   F9 (foundations) / D2 (full A2A wire protocol)
Tasks:   docs/TASKS.md#t-f9-04, docs/TASKS.md#t-f9-09
Status:  IMPLEMENTED - always defers, the answer-side wrapping composes mailbox.py
Tests:   Core/tests/unit/test_ask_peer_tool.py,
         Core/tests/unit/test_ports_agent_mailbox.py
Composes: ports/agent_mailbox.py, adapters/driven/peers/mailbox.py (t-f9-03, DONE)

WHY THIS IS NOT A VERTICAL'S TOOL

    `adapters/driven/tools/delivery/tools.py` and `.../fraud/tools.py` are "one tools
    package" - the part CLAUDE.md's contract says changes per vertical. This module is the
    opposite: a mechanism a vertical's package IMPORTS, never copies. `request_evidence`
    (t-f7-05, adapters/driven/tools/evidence.py) had the exact same defect once - filed
    inside a vertical's own package - and the fix was the same both times: pull it out to a
    shared module. Two anchors, one mistake (see the t-f7-05 note in docs/TASKS.md).

    `ask_peer` is the THIRD user of one suspension mechanism (`HumanGateway` and
    `request_evidence` are the first two): the turn suspends, something external answers,
    `DBOS.recv()` wakes it. No new suspension machinery gets invented here.

WHY THIS FUNCTION NEVER RETURNS A VALUE

    Pydantic AI's deferred tools cover two cases (docs/DECISIONS.md#d9): approval-required
    and externally-executed. `ask_peer` is always the second case - there is no synchronous
    answer to compute, ever, because the peer has to run its own turn and reply, possibly
    after suspending on a human itself. So the body has exactly one path, unconditionally
    `raise CallDeferred(...)`, never a branch that sometimes returns a placeholder "because
    this target answers instantly". A version that did that would type-check, pass a casual
    smoke test, and hand the model a fabricated answer nobody's peer ever gave.

WHAT TRAVELS IN THE METADATA, AND WHY NOTHING ELSE DOES

    `target` and `question` are exactly the model's own tool-call arguments, carried
    forward so whoever turns this deferred call into a real `AgentMailbox.ask()` call (a
    later wave's runner integration, t-f9-06, exactly mirroring how `request_evidence`'s
    deferred calls are turned into `HumanGateway.publish()` calls) does not have to guess
    them back out of the model's own message.

    `hop` and the correlation id are deliberately NOT minted here. Both need context this
    plain function does not have and must not fabricate:

      - `hop` is a property of the CURRENT TURN (0 for a turn a human started, or whatever
        `PeerAsk.hop` carried in if this turn is itself answering another agent's
        question) - turn-level state, not a model-supplied argument. A tool function that
        accepted `hop` from its own parameters would let the model set its own depth,
        which defeats `PeerPolicy.max_hops` outright.
      - The correlation id `AgentMailbox.ask()` returns is minted AND persisted together,
        atomically, by the same INSERT that makes it idempotent per (turn_id, target,
        question) - see mailbox.py's EXACTLY ONCE section. Minting a second one here, ahead
        of that insert, would be a second id nothing durable ever validates.

    This is also exactly why `hop_limit.authorise_hop` (t-f9-05) is not called from this
    module: it needs BOTH sides' `PeerPolicy` and the current hop depth in hand at once, and
    a plain tool function taking only the model's two string arguments has neither. The gate
    belongs at the same call site that eventually calls `AgentMailbox.ask()` - the runner,
    not here. Calling it here with only half its inputs would look like enforcement without
    being one, which is worse than not calling it at all. That runner has not landed yet
    (t-f9-06); if `hop_limit.py` is ever absent when this module is imported, this module
    still works, because it names the gate only in this comment and never imports it.

THE ANSWER IS UNTRUSTED CONTENT - CLAUDE.md non-negotiable 10

    `mailbox.py` exports `wrap_peer_answer` for exactly this module to use, rather than
    have this module re-derive the boundary - a delimiter that drifts between two modules
    is a boundary the model cannot see. `ask_peer_result` below is that composition: the
    value whoever resumes the turn (again, t-f9-06) should put in
    `DeferredToolResults.calls[tool_call_id]`, produced by calling `wrap_peer_answer`
    directly and never by rebuilding its delimiters.

TWO READERS NOW, AND ONLY ONE OF THEM RESUMES A TURN (t-f9-09)

    `mailbox.py` wraps on read rather than on write and said why: wrapping on write would
    "double-wrap the day a second reader is added". `AgentMailbox.read_answer` is that
    second reader - the port now declares the read-back that both adapters already had -
    and it returns the answer ALREADY wrapped.

    So there are two shapes in play and they must not be confused:

      `ask_peer_result(answer)`     takes the peer's RAW bytes - the value `answer()` was
                                    handed - and wraps them.
      `ask_peer_result_for(mailbox, correlation_id)` redeems the handle through the PORT
                                    and returns what the adapter already wrapped.

    A resumer holding a mailbox uses the second. Passing `read_answer`'s output into
    `ask_peer_result` nests one boundary inside another, and a model shown a nested
    `<untrusted-tool-output>` cannot tell which delimiter is the real one - a boundary
    whose meaning is ambiguous is not a boundary.

    There is deliberately no "is it already wrapped?" sniff anywhere here. A hostile peer
    controls its own bytes and can open with the opening delimiter and close with the
    closing one, so a sniff is a check the attacker passes on purpose - it would skip the
    stripping on exactly the answer that needed it. Which function to call is a fact about
    WHERE the text came from, and the type is what carries it.
"""

from __future__ import annotations

from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.ports.agent_mailbox import AgentMailbox


def ask_peer(target: str, question: str) -> str:
    """Ask another agent `target` a question. Always defers - see module docstring.

    The return type is `str` only so a type checker can tell what the AGENT eventually
    sees once the deferred call resolves (a tool result has to have some declared shape) -
    the peer's answer, wrapped by `ask_peer_result`. Every actual call raises before
    reaching that return.
    """
    raise CallDeferred(metadata={"target": target, "question": question})


def ask_peer_result(answer: str) -> str:
    """The value to place in `DeferredToolResults.calls[tool_call_id]` once a peer answers.

    A thin composition of `mailbox.wrap_peer_answer`, not a second implementation of it -
    see THE ANSWER IS UNTRUSTED CONTENT above. Exported so the runner that eventually
    resumes an `ask_peer` call (t-f9-06) has one place to call instead of importing
    `wrap_peer_answer` directly and forgetting that this is the tool that needs it.
    """
    return wrap_peer_answer(answer)


async def ask_peer_result_for(mailbox: AgentMailbox, correlation_id: str) -> str | None:
    """The resume value for an `ask_peer` call, redeemed through the PORT.

    Typed on `AgentMailbox`, not on a concrete adapter: that is the whole point of
    t-f9-09. Before the port declared `read_answer`, a resumer had to name
    `PgAgentMailbox` or `A2AAgentMailbox` to get an answer back, which put the choice of
    transport in the one module that is supposed to be transport-agnostic.

    Returns what the adapter already wrapped, verbatim - see TWO READERS NOW above for why
    it does not wrap again, and why it does not try to detect whether it should.
    None means the peer has not answered yet; the turn stays suspended, and a caller that
    turned that None into an empty tool result would be fabricating an answer nobody gave.
    """
    return await mailbox.read_answer(correlation_id)


def build_toolset() -> FunctionToolset[None]:
    """This mechanism's toolset - exactly `ask_peer`, nothing implied alongside it.

    A fresh `FunctionToolset` per call, same reasoning as every vertical's `build_toolset()`
    (see `delivery/tools.py`) and as `evidence.py`'s own: cheap to construct, and nothing
    here holds registration state a caller could accidentally share across profiles or
    turns. A profile that wants peers imports this one in rather than redeclaring the
    function (CLAUDE.md #8: no auto-discovery, an explicit list is the security property).
    """
    return FunctionToolset([ask_peer])
