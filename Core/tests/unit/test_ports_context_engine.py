"""`ContextEngine`'s trigger is TOTAL. An unreported window is the common case, not an edge.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-02

WHY THIS TEST EXISTS
    `CLAUDE.md` lists the compaction strategy as a silent-bug area: a wrong answer here
    shows up on the BILL or as a context overflow in production, never as a red test. Two
    properties are frozen before any adapter is written, and neither is observable from
    anywhere else in the suite.

    1. `should_compress` IS TOTAL OVER `window_used is None`. `Usage.context_window_used`
       is None whenever the provider does not report it, and `domain/compaction.py` says
       in as many words that None is COMMON. A trigger that answers None, raises, or
       shortcuts to False on that input never fires, so the agent never compacts and dies
       of context overflow - against a provider that DOES report usage the same code looks
       perfect. That is why totality is asserted against the port itself and not against
       some adapter's implementation of it: the fallback is part of the contract, and
       `docs/TASKS.md#t-f5-02` calls it mandatory. A docstring cannot make anything
       mandatory; an inherited default can.

       The unknown-capacity case (`context_window <= 0`) is in here too, because it is the
       arithmetic that would crash: the fallback divides by it.

    2. THE ASYNC SPLIT (D13), exactly as on `ToolPolicy`. `compress` is the one member
       that can reach a model - L3 and L4 each cost a summariser call - so it is the one
       coroutine. The trigger runs on every response and is pure arithmetic over an
       already-gathered `ContextState`; making it awaitable would buy an await point with
       nothing behind it, for the same reason `ToolPolicy.decide` stays sync.

    Nothing here drives behaviour beyond the trigger. The rest is a lock on a contract the
    F5 use case, the ladder engine and the DBOS step are all built against.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import cast

import pytest

from agent_core.domain.compaction import (
    CompactionPolicy,
    CompactionResult,
    ContextState,
    Rung,
)
from agent_core.domain.turn import SessionRef, Usage
from agent_core.ports.context_engine import ContextEngine

# The port's own trigger, reached unbound so the DEFAULT is what gets exercised. An
# adapter is free to specialise it; what this module pins is the answer it inherits when
# it does not.
_Trigger = Callable[[ContextEngine, ContextState, CompactionPolicy], bool]
_PORT_TRIGGER = cast("_Trigger", ContextEngine.should_compress)

POLICY = CompactionPolicy()  # trigger_fraction 0.75


class _AccountingEngine:
    """A minimal engine that keeps a local token estimate and nothing else.

    It is also the type-level conformance check: mypy proves it satisfies the port at the
    annotated assignment in `_engine()`, so the port cannot drift away from the shape
    asserted below without the project-wide mypy run failing too.

    It deliberately does NOT reimplement the trigger. Delegating to the port's default is
    what an adapter with no reason to specialise should do, and it is the reason the None
    fallback cannot be forgotten one adapter at a time.
    """

    def __init__(self) -> None:
        self.estimated_tokens = 0

    def on_session_start(self, session: SessionRef) -> None:
        self.estimated_tokens = 0

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        self.estimated_tokens += usage.input_tokens + usage.output_tokens

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        return _PORT_TRIGGER(self, state, policy)

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        return CompactionResult(
            compacted_history=history,
            checkpoint=None,
            rungs_applied=(Rung.L1_PRUNE_TOOL_OUTPUT,),
            tokens_before=self.estimated_tokens,
            tokens_after=self.estimated_tokens,
        )

    def on_session_end(self, session: SessionRef) -> None:
        self.estimated_tokens = 0


def _engine() -> ContextEngine:
    engine: ContextEngine = _AccountingEngine()
    return engine


def _state(
    session: SessionRef,
    *,
    window_used: float | None,
    estimated_tokens: int,
    context_window: int,
) -> ContextState:
    return ContextState(
        session=session,
        window_used=window_used,
        estimated_tokens=estimated_tokens,
        context_window=context_window,
        message_count=40,
    )


def _protocol_members() -> frozenset[str]:
    declared = getattr(ContextEngine, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(name for name in vars(ContextEngine) if not name.startswith("_"))


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_should_compress_is_total_when_the_provider_reports_no_window(
    session: SessionRef,
) -> None:
    """THE ANCHOR. None means "the provider was silent", never "there is room left".

    An unknown window must still produce a DEFINED answer - a real `bool` - computed from
    the local estimate. Answering None (the shape a bare `...` protocol body hands back),
    raising, or defaulting to False all read as "no compaction needed" at the call site,
    and all three kill the agent only in production.
    """
    state = _state(session, window_used=None, estimated_tokens=90_000, context_window=100_000)

    verdict = _PORT_TRIGGER(_engine(), state, POLICY)

    assert isinstance(verdict, bool), (
        "ContextEngine.should_compress answered "
        f"{verdict!r} for an unreported window. The trigger must be TOTAL: None is the "
        "COMMON case, and a non-bool answer is read as False by every call site."
    )
    assert verdict is True, (
        "90k of an estimated 100k window is 0.90, over trigger_fraction 0.75, yet the "
        "trigger said no. NEVER answer False just because the provider was silent - that "
        "is the bug that cannot be reproduced against a provider that reports usage."
    )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_estimate_fallback_is_a_real_decision_and_not_a_constant(
    session: SessionRef,
) -> None:
    """Totality is not "always True". A trigger that always fires compacts every turn.

    Compacting often costs MORE than it saves - every pass rewrites the prompt prefix and
    re-bills the whole prompt at full price on the next request. So the fallback has to
    say no on a nearly empty history just as readily as it says yes on a full one.
    """
    state = _state(session, window_used=None, estimated_tokens=10_000, context_window=100_000)

    verdict = _PORT_TRIGGER(_engine(), state, POLICY)

    assert verdict is False, (
        "0.10 of the estimated window is far under trigger_fraction 0.75 and must not "
        "trigger a pass. A trigger that always fires is the expensive failure mode."
    )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_should_compress_is_total_when_the_window_size_itself_is_unknown(
    session: SessionRef,
) -> None:
    """`context_window <= 0` is the divisor of the fallback. It must not crash the turn.

    The answer is pinned to the one the domain already chose for this exact input:
    `domain.compaction.target_tokens` resolves an unknown window to a target of zero,
    "rather than stop early against a number nobody knows". The trigger agrees with it -
    disagreeing would mean a ladder that is only ever climbed when the trigger it
    contradicts happens to fire.
    """
    state = _state(session, window_used=None, estimated_tokens=50_000, context_window=0)

    verdict = _PORT_TRIGGER(_engine(), state, POLICY)

    assert isinstance(verdict, bool), (
        f"An unknown window size produced {verdict!r} instead of a decision. The fallback "
        "divides by context_window; a ZeroDivisionError here fails the whole turn."
    )
    assert verdict is True, (
        "An unknown window must not read as headroom. domain.compaction.target_tokens "
        "already resolves this input towards compaction; the trigger must not disagree."
    )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_a_reported_window_is_believed_over_the_local_estimate(session: SessionRef) -> None:
    """The estimate is a FALLBACK. When the provider does report, its number wins."""
    reported_full = _state(session, window_used=0.95, estimated_tokens=0, context_window=100_000)
    reported_empty = _state(
        session, window_used=0.05, estimated_tokens=99_000, context_window=100_000
    )

    assert _PORT_TRIGGER(_engine(), reported_full, POLICY) is True
    assert _PORT_TRIGGER(_engine(), reported_empty, POLICY) is False, (
        "A reported 0.05 was overridden by a local estimate of 0.99. The estimate exists "
        "only for the turns where the provider says nothing; preferring it re-bills the "
        "prompt prefix on evidence the provider already contradicted."
    )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_trigger_fraction_boundary_belongs_to_the_profile(session: SessionRef) -> None:
    """`trigger_fraction` is per profile, so the comparison must read it, not a constant."""
    state = _state(session, window_used=None, estimated_tokens=50_000, context_window=100_000)
    eager = CompactionPolicy(trigger_fraction=0.40)
    patient = CompactionPolicy(trigger_fraction=0.90)

    assert _PORT_TRIGGER(_engine(), state, eager) is True
    assert _PORT_TRIGGER(_engine(), state, patient) is False


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_compress_is_the_only_awaitable_member() -> None:
    """D13, and the same split as `ToolPolicy`: one I/O member, the rest pure.

    `compress` can reach a summariser model (L3, L4), so it awaits. The trigger runs after
    every response and the session hooks are local accounting; a coroutine on any of them
    would add an await point with nothing behind it.
    """
    awaitable = {
        name
        for name, member in inspect.getmembers(ContextEngine, inspect.isfunction)
        if inspect.iscoroutinefunction(member)
    }

    assert awaitable == {"compress"}, (
        f"Awaitable members are {sorted(awaitable)}. `compress` is the only one that can "
        "reach a model; the trigger stays sync for the same reason ToolPolicy.decide does."
    )


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_the_port_shape_is_frozen() -> None:
    """Five members, and no sixth one that compacts a little every turn.

    Hermes ships per-exchange micro-compaction OFF BY DEFAULT because every pass breaks
    the provider's prompt cache. A member for it here is how it gets turned on by default.
    """
    members = sorted(_protocol_members())

    assert members == [
        "compress",
        "on_session_end",
        "on_session_start",
        "should_compress",
        "update_from_response",
    ], f"ContextEngine's shape changed: {members}."


@pytest.mark.silent
@pytest.mark.phase("F5")
def test_compress_returns_a_result_and_does_not_mutate_the_conversation() -> None:
    """The separation that lets compaction run inside a retryable `@DBOS.step()`."""
    signature = inspect.signature(ContextEngine.compress, eval_str=True)
    parameters = [name for name in signature.parameters if name != "self"]

    assert parameters == ["session", "history", "policy"]
    assert signature.return_annotation is CompactionResult, (
        "compress must hand a result BACK. An engine that mutates the live conversation "
        "cannot be discarded when its DBOS step is retried."
    )
