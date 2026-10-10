"""Driven adapter: `ask_peer` - the deferred-tool mechanism for agent-to-agent asks.

Phase:   F9 (foundations) / D2 (full A2A wire protocol)
Tasks:   docs/TASKS.md#t-f9-04, docs/TASKS.md#t-f9-09
Status:  IMPLEMENTED - always defers; the answer side belongs to mailbox.py and the
         workflow, and this module composes neither (see WHY THERE ARE NO RESUME HELPERS)
Tests:   Core/tests/unit/test_ask_peer_tool.py
Composes: nothing. It names no port and imports no adapter - the whole module is one
         deferring function and its toolset.

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

THE ANSWER IS UNTRUSTED CONTENT - CLAUDE.md non-negotiable 10, AND THIS MODULE HAS NO
PART IN APPLYING IT

    `mailbox.py` owns that boundary. It wraps ON READ rather than on write, and said why:
    wrapping on write would "double-wrap the day a second reader is added". So
    `AgentMailbox.read_answer` returns the peer's answer ALREADY inside the delimiters,
    and `workflow/turn_workflow.py::_step_answer_peer` puts exactly those bytes into
    `DeferredToolResults.calls[tool_call_id]` - unchanged, unexamined, unwrapped again.

    ONE BOUNDARY, APPLIED AT ONE PLACE, IS THE WHOLE PROPERTY. Non-negotiable 10 is not
    "wrap a lot"; it is that a model can point at one `<untrusted-tool-output>` and know
    what it delimits. Double-wrapping is not twice as safe - a nested boundary is a fence
    the model has to parse, and a delimiter whose meaning is ambiguous is not a delimiter.

WHY THERE ARE NO RESUME HELPERS HERE ANY MORE (t-f9-09's pair, retired as t-f11-50
collateral)

    This module used to export two: `ask_peer_result(answer)`, which wrapped a peer's RAW
    bytes, and `ask_peer_result_for(mailbox, correlation_id)`, which redeemed the handle
    through the port and returned what the adapter had already wrapped. Since t-f11-48
    neither was reachable from production - `_step_answer_peer` does the read itself - and
    both are now deleted rather than kept as a documented surface.

    KEEPING THEM AND "MAKING THEM IMPOSSIBLE TO MISUSE" WAS NOT ON THE TABLE. The only
    way to make one function safe against being handed the other's input is a check of the
    form "is this text already wrapped?", and no such check can exist here: a hostile peer
    controls its own bytes, so it can open with the opening delimiter and close with the
    closing one, and the sniff is then a test the attacker passes ON PURPOSE - skipping
    the stripping on exactly the answer that needed it. Which shape a string is, is a fact
    about WHERE it came from, and nothing recoverable from the string.

    So the pair's safety rested entirely on a caller reading a docstring and picking the
    right one, and its only remaining callers were tests. Two functions kept for that is
    two ways to nest a boundary, sitting in the one module a future vertical imports when
    it wants peers. A vertical that needs raw bytes wrapped calls
    `mailbox.wrap_peer_answer`; a resumer holding a mailbox calls
    `AgentMailbox.read_answer` and passes what comes back through UNCHANGED.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred, ModelRetry
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


def ask_peer(target: str, question: str) -> str:
    """Ask another agent `target` a question. Always defers - see module docstring.

    The return type is `str` only so a type checker can tell what the AGENT eventually
    sees once the deferred call resolves (a tool result has to have some declared shape) -
    the peer's answer, as `AgentMailbox.read_answer` hands it back: already inside the
    untrusted-content delimiters. Every actual call raises before reaching that return.
    """
    raise CallDeferred(metadata={"target": target, "question": question})


def _guarded_ask_peer(ctx: RunContext[Any], target: str, question: str) -> str:
    """Ask another agent `target` a question. One ask per model step.

    The workflow resumes a turn with one peer answer at a time, while Pydantic AI needs the
    results of every deferred call at once, so a second ask_peer in the same model response
    would hang the turn. Only the first ask_peer of a response defers; the others get a
    retry prompt telling the model to ask one agent per step. This is a guard in the tool,
    not the full fix (collecting every answer before resuming), which belongs to the
    workflow.
    """
    last = ctx.messages[-1] if ctx.messages else None
    if isinstance(last, ModelResponse):
        asks = [p for p in last.parts if isinstance(p, ToolCallPart) and p.tool_name == "ask_peer"]
        if len(asks) > 1 and ctx.tool_call_id != asks[0].tool_call_id:
            raise ModelRetry(
                "Ask exactly one specialist per step: wait for the answer to your first "
                "ask_peer before asking another."
            )
    return ask_peer(target, question)


def build_toolset() -> FunctionToolset[None]:
    """This mechanism's toolset - exactly `ask_peer`, nothing implied alongside it.

    A fresh `FunctionToolset` per call, same reasoning as every vertical's `build_toolset()`
    (see `delivery/tools.py`) and as `evidence.py`'s own: cheap to construct, and nothing
    here holds registration state a caller could accidentally share across profiles or
    turns. A profile that wants peers imports this one in rather than redeclaring the
    function (CLAUDE.md #8: no auto-discovery, an explicit list is the security property).
    """
    return FunctionToolset([Tool(_guarded_ask_peer, name="ask_peer")])
