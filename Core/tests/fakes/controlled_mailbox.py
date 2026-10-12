"""A fault-injectable `AgentMailbox` wrapper: Pi's `ControlledStorage` for our mailbox (C7).

    mailbox = ControlledMailbox(inner)               # inner: any mailbox (memory, Postgres, A2A)
    mailbox.fail("claim_next", ProcessDied(), after=True)   # claimed, then the process 'died'
    gate = mailbox.hang("answer")                    # block the first answer ...
    await gate.entered.wait(); ...; gate.release()   # ... until the test lets it through
    mailbox.delay("read_answer", 0.05, times=3)
    mailbox.calls / mailbox.count("ask")             # what was called, in order

A fault applies to the Nth call of one verb (`on_call`, 1-based) and, with `times`, the calls after
it. `after=True` lets the verb TAKE EFFECT on the inner mailbox first and then fails or hangs: the
caller is told it failed although the write happened, which is how a lost acknowledgement and a
process killed right after a commit look from outside.

`ProcessDied` is a `BaseException` on purpose: no `except Exception` in the code under test can
swallow it, exactly as nothing survives a kill. In a real DBOS run a death is simulated by hanging
a verb and destroying the engine (the workflow stays `PENDING`, as after a kill); `ProcessDied` is
for the unit tests that drive a workflow body or a worker function without an engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.turn import SessionRef, TurnId


class ProcessDied(BaseException):  # noqa: N818 - a BaseException on purpose, see the module doc
    """The process was killed at this point."""


@dataclass(frozen=True)
class Call:
    """One call to the wrapped mailbox. `outcome` is `ok` or the name of what it raised."""

    verb: str
    number: int  # the nth call of this verb, 1-based
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    outcome: str = "pending"


@dataclass
class Gate:
    """Holds one call. `entered` is set when the held call arrives; `release()` lets it go."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    _released: asyncio.Event = field(default_factory=asyncio.Event)

    def release(self) -> None:
        self._released.set()


@dataclass
class _Plan:
    on_call: int
    times: int
    after: bool
    error: BaseException | None = None
    gate: Gate | None = None
    delay: float = 0.0

    def covers(self, number: int) -> bool:
        return self.on_call <= number < self.on_call + self.times


class ControlledMailbox:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._calls: list[Call] = []
        self._numbers: dict[str, int] = {}
        self._plans: dict[str, list[_Plan]] = {}

    # --- the fault plan ------------------------------------------------------------------------

    def fail(
        self,
        verb: str,
        error: BaseException,
        *,
        on_call: int = 1,
        times: int = 1,
        after: bool = False,
    ) -> None:
        self._plans.setdefault(verb, []).append(_Plan(on_call, times, after, error=error))

    def hang(self, verb: str, *, on_call: int = 1, after: bool = False) -> Gate:
        gate = Gate()
        self._plans.setdefault(verb, []).append(_Plan(on_call, 1, after, gate=gate))
        return gate

    def delay(self, verb: str, seconds: float, *, on_call: int = 1, times: int = 1) -> None:
        self._plans.setdefault(verb, []).append(_Plan(on_call, times, False, delay=seconds))

    # --- what happened -------------------------------------------------------------------------

    @property
    def calls(self) -> list[Call]:
        return list(self._calls)

    def count(self, verb: str) -> int:
        return self._numbers.get(verb, 0)

    # --- the port ------------------------------------------------------------------------------

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        return await self._invoke("discover", policy)  # type: ignore[no-any-return]

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
        asker: AgentId | None = None,
        request_key: str | None = None,
    ) -> str:
        return await self._invoke(  # type: ignore[no-any-return]
            "ask",
            policy,
            target,
            question,
            from_session=from_session,
            turn_id=turn_id,
            hop=hop,
            asker=asker,
            request_key=request_key,
        )

    async def answer(
        self, correlation_id: str, answer: str, *, answer_token: str | None = None
    ) -> None:
        await self._invoke("answer", correlation_id, answer, answer_token=answer_token)

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        return await self._invoke("cancel", correlation_id, reason)  # type: ignore[no-any-return]

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        return await self._invoke("cancel_open", turn_id, reason)  # type: ignore[no-any-return]

    async def is_cancelled(self, correlation_id: str) -> bool:
        return await self._invoke("is_cancelled", correlation_id)  # type: ignore[no-any-return]

    async def read_answer(self, correlation_id: str) -> str | None:
        return await self._invoke("read_answer", correlation_id)  # type: ignore[no-any-return]

    # --- the answering side's queue (when the inner mailbox has one) ---------------------------

    async def claim_next(self, target: AgentId) -> Any:
        return await self._invoke("claim_next", target)

    async def reclaim_stale(
        self,
        target: AgentId,
        *,
        lease: timedelta,
        exists: Callable[[str, int], Awaitable[bool]],
        retire: Callable[[str, int], Awaitable[None]] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        return await self._invoke(  # type: ignore[no-any-return]
            "reclaim_stale", target, lease=lease, exists=exists, retire=retire, now=now
        )

    # --- machinery -----------------------------------------------------------------------------

    async def _invoke(self, verb: str, *args: Any, **kwargs: Any) -> Any:
        number = self._numbers.get(verb, 0) + 1
        self._numbers[verb] = number
        index = len(self._calls)
        self._calls.append(Call(verb, number, args, dict(kwargs)))
        plans = [p for p in self._plans.get(verb, []) if p.covers(number)]
        before = [p for p in plans if not p.after]
        after = [p for p in plans if p.after]
        try:
            await self._apply(before)
            result = await getattr(self.inner, verb)(*args, **kwargs)
            await self._apply(after)
        except BaseException as error:
            self._calls[index] = Call(verb, number, args, dict(kwargs), type(error).__name__)
            raise
        self._calls[index] = Call(verb, number, args, dict(kwargs), "ok")
        return result

    @staticmethod
    async def _apply(plans: list[_Plan]) -> None:
        for plan in plans:
            if plan.delay:
                await asyncio.sleep(plan.delay)
            if plan.gate is not None:
                plan.gate.entered.set()
                await plan.gate._released.wait()  # noqa: SLF001 - the gate is ours
            if plan.error is not None:
                raise plan.error
