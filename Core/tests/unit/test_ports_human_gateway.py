"""`HumanGateway` publishes and correlates. It never waits, and it never raises on a miss.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-01

WHY THIS TEST EXISTS
    Two properties of this port are invisible to every other test in the suite, and both
    fail in production rather than in CI.

    1. THE WAIT DOES NOT LIVE HERE. The durable wait is `DBOS.recv()` in the workflow;
       that is what survives a restart, a redeploy and three days of a human not
       answering. A `wait_for_reply()` on this port would look reasonable, type-check,
       and pass a unit test against an in-process fake - and then lose the turn on the
       next deploy, because a sleep or a poll loop is not a durable wait. The defence is
       the absence of such a member, exactly as with `AuditSink`'s missing `update_*`, so
       the absence is what this module asserts.

       A sync `def` doing I/O is the same defect wearing a different hat: it blocks the
       event loop for as long as the channel takes to answer. D13 makes every member of
       this port a coroutine, and that is checked here rather than assumed.

    2. `correlate` RETURNS `None`, IT DOES NOT RAISE. An unknown or expired handle is the
       normal case, not an error: correlation ids expire, and strangers POST at the
       decision route. The HTTP adapter must be able to turn a miss into a 404 without a
       `try`. If the port were free to raise, every call site would grow its own except
       clause - and the one that forgets it turns a stray reply into a 500, or worse,
       into a guess. Guessing here approves an action nobody approved.

    Nothing below drives behaviour. It is a lock on a contract that F3's channel adapter,
    `ResumeTurn` and the decision route are all built against.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import get_args, get_type_hints

import pytest

from agent_core.domain.turn import PendingRequest, SessionRef, ToolCallId, TurnId
from agent_core.ports.human_gateway import HumanGateway

# Every way a "just wait here for the answer" member has ever been spelled. The list is
# deliberately generous: the point is to make the reviewer justify a new member, not to
# match one exact name.
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
    "next_",
    "get_result",
    "resolve_when",
)


def _protocol_members() -> frozenset[str]:
    """The names `HumanGateway` itself declares, without object/Protocol noise."""
    declared = getattr(HumanGateway, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(name for name in vars(HumanGateway) if not name.startswith("_"))


def _hints(name: str) -> dict[str, object]:
    return dict(get_type_hints(getattr(HumanGateway, name)))


def _parameters(name: str) -> list[str]:
    signature = inspect.signature(getattr(HumanGateway, name))
    return [p for p in signature.parameters if p != "self"]


class _EmptyGateway:
    """A gateway that has published nothing, so every correlation id is unknown to it.

    It is also the type-level conformance check: mypy proves this satisfies the port at
    the assignment below, so the port cannot drift away from the shape asserted here
    without the project-wide mypy run failing too.
    """

    def __init__(self) -> None:
        self.published: list[tuple[TurnId, SessionRef, tuple[PendingRequest, ...]]] = []

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        self.published.append((turn_id, session, requests))

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return None


@pytest.mark.phase("F3")
def test_protocol_exposes_no_blocking_call() -> None:
    """The port publishes and correlates. Anything that waits belongs in the workflow.

    Both halves of "blocking" are checked: no member NAMED for waiting, and no member
    that is a sync `def` - a synchronous channel round-trip blocks the event loop for
    every other turn in the process (D13).
    """
    members = sorted(_protocol_members())

    assert members == ["correlate", "publish"], (
        "HumanGateway answers one question - who do I ask, and how do I wait for the "
        f"answer - through exactly publish + correlate. Found: {members}. Outbound "
        "delivery of a finished TurnResult is a channel registry (D23), not this port."
    )

    waiting = sorted(
        name for name in members if any(fragment in name for fragment in BLOCKING_FRAGMENTS)
    )
    assert waiting == [], (
        f"HumanGateway grew a waiting member: {waiting}. The durable wait is DBOS.recv() "
        "in adapters/driving/workflow/; a sleep, a poll loop or a thread join here "
        "silently loses the turn on the next deploy."
    )

    for name in members:
        assert inspect.iscoroutinefunction(getattr(HumanGateway, name)), (
            f"HumanGateway.{name} is sync; ports/ is async end to end (D13). A blocking "
            "channel call here stalls every other turn in the process."
        )


@pytest.mark.phase("F3")
def test_correlate_returns_none_for_an_unknown_or_expired_id() -> None:
    """A miss is a value, not an exception.

    Declared as `tuple[TurnId, ToolCallId] | None` so the HTTP adapter is forced to
    handle the miss at the call site and can answer 404 without a `try`. The tool call id
    is the domain `ToolCallId`, not a bare `str`: `AuditSink.record_human_decision` and
    `ResumeTurn`'s idempotency key both take that type, and a correlate that hands back a
    loose string pushes an unchecked cast into every consumer.
    """
    hint = _hints("correlate")["return"]
    options = get_args(hint)

    assert type(None) in options, (
        "HumanGateway.correlate must be able to return None. An unknown or expired "
        "handle is the normal case; raising makes every call site grow an except clause."
    )
    assert tuple[TurnId, ToolCallId] in options, (
        "A resolved correlation is (turn_id, tool_call_id) with the DOMAIN ToolCallId - "
        f"the id PendingRequest carries. Found: {options}."
    )
    assert _parameters("correlate") == ["correlation_id"]
    assert _hints("correlate")["correlation_id"] is str

    gateway: HumanGateway = _EmptyGateway()
    resolved = asyncio.run(gateway.correlate("no-such-handle"))

    assert resolved is None, (
        "A gateway holding no correlations must answer None, not raise and not guess. "
        "Routing a stray reply into the wrong turn approves an action nobody approved."
    )


@pytest.mark.phase("F3")
def test_publish_is_frozen_at_turn_session_and_requests() -> None:
    """The published unit is the whole pending set of ONE turn, on ONE session.

    `requests` is a tuple, not a single request: a turn can suspend on several deferred
    calls at once and they must reach the human as one ask. Publishing them one at a time
    is how a human ends up answering half a turn.
    """
    assert _parameters("publish") == ["turn_id", "session", "requests"]

    hints = _hints("publish")
    assert hints["turn_id"] is TurnId
    assert hints["session"] is SessionRef
    assert hints["requests"] == tuple[PendingRequest, ...]
    assert hints["return"] is type(None), (
        "publish hands nothing back. A return value here is where a caller starts "
        "waiting on it."
    )
