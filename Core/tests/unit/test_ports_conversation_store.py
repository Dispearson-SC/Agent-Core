"""`ConversationStore` is append-only, and the protocol is the thing that says so.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-08

WHY THIS TEST EXISTS
    CLAUDE.md non-negotiable #6 and the `save_checkpoint` docstring both state the same
    rule: history and checkpoints are appended, never rewritten. That rule is only real
    if the object a use case holds has no method capable of breaking it. A prompt
    injection cannot call `update_message` if `update_message` is not on the protocol -
    the absence IS the defence, exactly as with the missing knowledge-write tool.

    So this file asserts a shape and an absence. It is a lock on the contract, not a
    driver of behaviour: the day someone adds `update_turn(...)` "just for a migration
    script", this fails and the conversation about append-only happens before the merge
    rather than after the audit trail is already unreliable.
"""

import inspect
from typing import get_type_hints

from agent_core.domain.turn import TurnId, TurnRequest
from agent_core.ports.conversation_store import ConversationStore

MUTATING_PREFIXES = ("update_", "delete_")


def _protocol_members() -> frozenset[str]:
    """The names `ConversationStore` itself declares, without object/Protocol noise."""
    declared = getattr(ConversationStore, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(
        name for name in vars(ConversationStore) if not name.startswith("_")
    )


def test_protocol_exposes_append_request() -> None:
    """The ordering rule - persist the inbound turn BEFORE the model runs - needs a
    method to hang on, and the caller must be able to await it (D13)."""
    members = _protocol_members()

    assert "append_request" in members

    append_request = ConversationStore.append_request
    assert inspect.iscoroutinefunction(append_request)

    signature = inspect.signature(append_request)
    assert list(signature.parameters) == ["self", "turn_id", "request"]

    hints = get_type_hints(append_request)
    assert hints["turn_id"] is TurnId
    assert hints["request"] is TurnRequest
    assert hints["return"] is type(None)


def test_protocol_has_no_mutating_member() -> None:
    """No `update_*`, no `delete_*`. An append-only store that offers a way to rewrite
    history is not append-only; it is a store with a convention nobody enforces."""
    offenders = sorted(
        name
        for name in _protocol_members()
        if name.startswith(MUTATING_PREFIXES)
    )

    assert offenders == [], (
        "ConversationStore is append-only (CLAUDE.md non-negotiable #6). "
        f"Remove: {offenders}. Chain a checkpoint via `supersedes` instead of "
        "rewriting one."
    )
