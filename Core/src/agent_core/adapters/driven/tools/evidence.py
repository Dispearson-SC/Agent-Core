"""Driven adapter: `request_evidence` - the deferred-tool mechanism itself (D9).

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-05
Status:  IMPLEMENTED - always defers, correlation id reuses HumanGateway's generator
Tests:   Core/tests/unit/test_evidence_tool.py

WHY THIS IS NOT A VERTICAL'S TOOL

    `adapters/driven/tools/delivery/tools.py` and `.../fraud/tools.py` are what CLAUDE.md's
    contract calls "one tools package" - the part that changes per vertical. This module is
    the opposite: the mechanism a vertical's package IMPORTS, never copies. It was filed
    inside the delivery vertical once; that was the defect (see the note at
    docs/TASKS.md#t-f7-05). A second vertical that needed evidence would have copied this
    file, and the copy is the one that drifts. `ask_peer` (F9, D17) will be the third user
    of the same pattern and belongs here too, for the same reason.

WHY THIS FUNCTION NEVER RETURNS A VALUE

    Pydantic AI's deferred tools cover two cases (docs/DECISIONS.md#d9): a tool that needs
    approval before it runs, and a tool that is executed EXTERNALLY - the run ends and
    something outside the agent produces the result later. `request_evidence` is always the
    second case. There is no policy question here the way `pricing_apply`'s approval
    threshold is a policy question: asking a human to hand over a photo has no synchronous
    answer to compute, ever. So the body has exactly one path, and it raises
    `pydantic_ai.exceptions.CallDeferred` unconditionally - never a branch that sometimes
    returns a dict "because this one didn't really need a human". A version that did that
    would type-check, pass a casual smoke test, and then hand the model a fabricated result
    for a photo nobody supplied.

    `CallDeferred`, not `ApprovalRequired`: the latter routes the call into
    `DeferredToolRequests.approvals`, which is the approval half of D9 and belongs to
    `AgentProfile.requires_approval_for()` (t-f4-02), not to this tool.

THE CORRELATION ID - REUSED, NOT RE-MINTED

    `HumanGateway` (t-f3-04, adapters/driven/human/gateway.py) already owns one unguessable
    id scheme: `new_correlation_id()`, 256 bits from `secrets`, the same bearer token an
    approval hands to a human over a channel. Evidence requests are the second correlated
    ask over the same table (migration 0010's `human_requests`), so this module imports
    that exact function rather than writing `uuid.uuid4()` or `secrets.token_hex()` next to
    it. Two generators for one job is how the two drift - one gets tightened after an
    audit and the other is forgotten.

    The id is minted here, at deferral time, and travels in `CallDeferred.metadata` (which
    Pydantic AI surfaces on `DeferredToolRequests.metadata`, keyed by `tool_call_id`) so it
    exists before the runner branch (t-f7-07) or the upload route (t-f7-08) run at all -
    both are later waves and both need a stable handle to key on rather than inventing one
    of their own at a different layer.

NOT THIS MODULE'S JOB

    - Deciding whether a profile is allowed to ask for evidence at all
      (`MediaPolicy.allow_evidence_requests`, domain/media.py) - that gates whether this
      tool is even added to a toolset, which is composition's concern, not this function's.
    - Publishing the ask to a channel, or storing the correlation row - `HumanGateway
      .publish()` does both, called from the runner once it turns `DeferredToolRequests
      .calls` into `PendingRequest`s (t-f7-07).
    - The BYTES vs SIGNED_URL choice for how the answered evidence reaches the model
      (docs/TASKS.md#t-f7-06, t-f7-07).
"""

from __future__ import annotations

from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.human.gateway import new_correlation_id


def request_evidence(kind: str, reason: str) -> dict[str, str]:
    """Ask a human to supply evidence - a photo, a document, whatever `kind` names.

    The return type is `dict[str, str]` only so a type checker can tell what the AGENT
    eventually sees once the deferred call resolves (a tool result has to have some
    declared shape). Every actual call raises before reaching that return - see the module
    docstring. `kind` is a free string here on purpose: which kinds a given profile accepts
    is `MediaPolicy.accepted_kinds` (domain/media.py), not a closed set this shared
    mechanism should hard-code and force every vertical's policy to match.
    """
    raise CallDeferred(
        metadata={
            "correlation_id": new_correlation_id(),
            "kind": kind,
            "reason": reason,
        }
    )


def build_toolset() -> FunctionToolset[None]:
    """This mechanism's toolset - exactly `request_evidence`, nothing implied alongside it.

    A fresh `FunctionToolset` per call, same reasoning as every vertical's `build_toolset()`
    (see `delivery/tools.py`): cheap to construct, and nothing here holds registration state
    a caller could accidentally share across profiles or turns. A vertical's own
    `build_toolset()` composes this one in rather than redeclaring the function.
    """
    return FunctionToolset([request_evidence])
