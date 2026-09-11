"""`ask_peer` - the deferred-tool mechanism for agent-to-agent asks (D9's third user).

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-04
Covers:  adapters/driven/tools/peers.py

WHAT IS BEING DEFENDED

    1. IT NEVER ANSWERS. `ask_peer` has no synchronous result to give - the target agent
       has to run its own turn and reply. Exactly like `request_evidence`
       (test_evidence_tool.py, t-f7-05) and exactly like an approval, so every call must
       raise `pydantic_ai.exceptions.CallDeferred` and never fall through to a `return`. A
       version that blocked on `AgentMailbox.ask()` and then polled for the answer would
       hold the event loop (and the DBOS step) open for however long the peer takes - which
       may be three days, because the peer can itself suspend on a human.

    2. THE MODULE HAS NO ANSWER SIDE AT ALL, AND THAT ABSENCE IS THE DEFENCE (CLAUDE.md
       non-negotiable #10). It used to export `ask_peer_result` (wrap a peer's RAW bytes)
       and `ask_peer_result_for` (redeem through the port, already wrapped), and this file
       used to prove the first one composed `mailbox.wrap_peer_answer` rather than a
       drifting copy of it. Both were dead on the production path from t-f11-48 - the
       workflow reads through `AgentMailbox.read_answer` and passes those bytes on
       unchanged - and both are now deleted, because the only thing that could have made
       one safe against the other's input is an "is it already wrapped?" sniff, and a
       hostile peer writes its own bytes and passes that sniff on purpose.

       So what this file defends is now the absence: one boundary, applied in `mailbox.py`,
       with no second one available here to nest inside it. That the boundary itself is
       correct is `test_peer_mailbox.py`'s job, and that the runner delivers exactly one
       wrap to the model is `test_runner_deferred.py`'s.

This is a pure unit test: no Postgres, no DBOS, no agent run. `ask_peer` is a plain
function raising a plain exception, which exists before any infrastructure gets involved.
"""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import CallDeferred

from agent_core.adapters.driven.tools import peers as peer_tools
from agent_core.adapters.driven.tools.peers import ask_peer, build_toolset


def test_ask_peer_suspends_instead_of_returning() -> None:
    """Calling the tool must never produce a result - only a deferred call.

    No `AgentMailbox` is even constructed in this test. If `ask_peer` tried to enqueue or
    wait on one, this test would fail with a `TypeError` on the missing argument long
    before it got anywhere near a `CallDeferred` - the absence of any mailbox parameter is
    itself part of what proves the call cannot be blocking on I/O.
    """
    with pytest.raises(CallDeferred) as excinfo:
        ask_peer(target="billing", question="has invoice 402 been paid?")

    assert excinfo.value.metadata is not None, (
        "CallDeferred carried no metadata; whatever turns this into a real "
        "AgentMailbox.ask() call has nothing to enqueue and no peer would ever be asked."
    )


def test_ask_peer_defers_the_exact_target_and_question() -> None:
    """The runner (a later wave) must be able to recover what the model asked for.

    `target` and `question` travel in `CallDeferred.metadata` rather than being dropped,
    the same way `request_evidence` carries `kind` and `reason` forward.
    """
    with pytest.raises(CallDeferred) as excinfo:
        ask_peer(target="personal-assistant", question="what is on the calendar tomorrow?")

    metadata = excinfo.value.metadata
    assert metadata is not None
    assert metadata["target"] == "personal-assistant"
    assert metadata["question"] == "what is on the calendar tomorrow?"


def test_ask_peer_never_returns_even_when_arguments_look_answerable() -> None:
    """No branch anywhere quietly computes a placeholder instead of deferring.

    A version that special-cased some `target`/`question` combination ("this one doesn't
    really need a peer") would type-check and pass a casual smoke test, then one day hand
    the model a fabricated answer nobody's peer ever gave.
    """
    for target, question in (
        ("self", "ping"),
        ("", ""),
        ("customer-service", "what did the customer already say?"),
    ):
        with pytest.raises(CallDeferred):
            ask_peer(target=target, question=question)


def test_build_toolset_exposes_exactly_ask_peer() -> None:
    """No auto-discovery, no implied second tool (CLAUDE.md #8) - mirrors evidence.py."""
    toolset = build_toolset()
    tool_names = {tool.name for tool in toolset.tools.values()}
    assert tool_names == {"ask_peer"}


def test_the_ask_peer_module_offers_no_way_to_wrap_an_answer() -> None:
    """The retired pair must stay retired. See point 2 of the module docstring.

    A defence that consists of a function NOT existing is exactly the shape CLAUDE.md
    non-negotiable #8 uses for the knowledge-write tool, and it fails the same silent way:
    re-adding `ask_peer_result` would type-check, read as helpful, and give the one module
    a vertical imports for peers a second untrusted boundary to nest inside the one
    `mailbox.py` already applied. A model shown a nested `<untrusted-tool-output>` cannot
    tell which delimiter is the real one, and no check here can distinguish wrapped bytes
    from a hostile peer's imitation of them - it writes its own bytes.

    Named members rather than a whole-module sweep: `build_toolset` and `ask_peer` are the
    surface, and a future member should be judged on its own, not caught by a pattern.
    """
    for retired in ("ask_peer_result", "ask_peer_result_for"):
        assert not hasattr(peer_tools, retired), (
            f"`{retired}` is back on adapters/driven/tools/peers.py. Nothing in production "
            "calls it: `_step_answer_peer` reads through `AgentMailbox.read_answer` and "
            "passes those bytes on unchanged. Whoever needs raw bytes wrapped calls "
            "`mailbox.wrap_peer_answer`, which is the one place the boundary is applied."
        )
