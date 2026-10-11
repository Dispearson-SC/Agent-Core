"""A scripted model for tests: no network, no API key, fully deterministic.

Built on Pydantic AI's `FunctionModel` and inspired by the test doubles of two agent
frameworks (read for ideas only): OpenCode's `TestLLM` (push / always / serve, requests(),
gate) and Pi's faux provider (fauxText / fauxToolCall, factory responses, callCount).

    model = ScriptedModel()
    model.push(
        tool_calls(tool_call("ask_peer", {"target": "a", "question": "1"}, id="c1"),
                   tool_call("ask_peer", {"target": "b", "question": "2"}, id="c2")),
        text("done"),
    )
    runner = PydanticAgentRunner(..., model_factory=lambda model_id, base_url: model.model)

What it gives you
    * a queue of scripted responses, consumed one per model call, in order:
      `text("hi")`, `tool_call("name", {...}, id="c1")`, `tool_calls(a, b)` (several calls in
      ONE response), `reply(text("x"), tool_call(...))` (mixed parts), `hang()` (never
      returns until the test ends or the call is cancelled) and `fail(exc)` (raises).
    * `serve(fn)`: a fallback for calls after the queue is empty. `fn(messages, info)` returns
      any of the above (or a ready `ModelResponse`). Without a fallback an exhausted script
      FAILS the call loudly instead of inventing an answer.
    * `requests`: what the model saw on each call (`list[list[ModelMessage]]`), and
      `call_count`.
    * `gate()`: holds the NEXT unstarted call mid-flight. `await gate.started.wait()` says the
      model was entered; `gate.release()` lets it answer. Use it to prove ordering
      (e.g. nothing resumes before both peers answered) without sleeping.
    * `wait_for_calls(n)`: await until the model has been entered n times.

A tool_call without `id=` gets a deterministic `scripted-call-N` id, numbered by the MODEL
INSTANCE that emits it (1, 2, ... per model, in emission order), so assertions on `tool_call_id`
order are stable and never depend on which test ran first. The id is assigned when the model
answers, not when `tool_call(...)` is built: until then the part carries an empty id.
The helper itself is tested in
`tests/unit/test_scripted_model.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage


@dataclass(frozen=True)
class Reply:
    """One scripted response: the parts the model emits in a single step."""

    parts: tuple[TextPart | ToolCallPart, ...]


@dataclass(frozen=True)
class _Hang:
    pass


@dataclass(frozen=True)
class _Fail:
    error: BaseException


Scripted = Reply | _Hang | _Fail | ModelResponse
Factory = Callable[[list[ModelMessage], AgentInfo], "Scripted | Awaitable[Scripted]"]


def text(content: str) -> Reply:
    return Reply((TextPart(content),))


def tool_call(name: str, args: dict[str, Any] | None = None, *, id: str | None = None) -> Reply:
    return Reply((ToolCallPart(name, args or {}, tool_call_id=id if id is not None else ""),))


def reply(*items: Reply) -> Reply:
    """Combine replies into ONE response (e.g. text plus a tool call)."""
    return Reply(tuple(part for item in items for part in item.parts))


def tool_calls(*calls: Reply) -> Reply:
    """Several tool calls emitted by the model in one response."""
    return reply(*calls)


def hang() -> _Hang:
    return _Hang()


def fail(error: BaseException) -> _Fail:
    return _Fail(error)


@dataclass
class Gate:
    """Holds one model call open. `started` is set when the call is entered."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    _released: asyncio.Event = field(default_factory=asyncio.Event)

    def release(self) -> None:
        self._released.set()


class ScriptedModelExhaustedError(AssertionError):
    """The model was called more times than the script (and any `serve`) covers."""


class ScriptedModel:
    def __init__(self, *, model_name: str = "scripted") -> None:
        self._queue: list[Scripted] = []
        self._fallback: Factory | None = None
        self._gates: list[Gate] = []
        self._entered = asyncio.Condition()
        self.requests: list[list[ModelMessage]] = []
        self.call_count = 0
        self._generated_ids = 0
        self.model = FunctionModel(self._call, model_name=model_name)

    def push(self, *responses: Scripted) -> ScriptedModel:
        self._queue.extend(responses)
        return self

    def serve(self, factory: Factory) -> ScriptedModel:
        self._fallback = factory
        return self

    def gate(self) -> Gate:
        """Hold the next call that has not yet been entered (queue several to hold several)."""
        gate = Gate()
        self._gates.append(gate)
        return gate

    async def wait_for_calls(self, n: int, *, timeout: float = 5.0) -> None:
        async def wait() -> None:
            async with self._entered:
                await self._entered.wait_for(lambda: self.call_count >= n)

        await asyncio.wait_for(wait(), timeout)

    async def _call(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.call_count += 1
        self.requests.append(list(messages))
        gate = self._gates.pop(0) if self._gates else None
        async with self._entered:
            self._entered.notify_all()
        if gate is not None:
            gate.started.set()
            await gate._released.wait()

        if self._queue:
            scripted = self._queue.pop(0)
        elif self._fallback is not None:
            produced = self._fallback(messages, info)
            scripted = await produced if isinstance(produced, Awaitable) else produced
        else:
            raise ScriptedModelExhaustedError(
                f"model call #{self.call_count} has no scripted response and no serve() fallback"
            )

        if isinstance(scripted, _Hang):
            await asyncio.Event().wait()
        if isinstance(scripted, _Fail):
            raise scripted.error
        if isinstance(scripted, ModelResponse):
            return scripted
        return ModelResponse(
            parts=[self._with_id(part) for part in scripted.parts],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
        )

    def _with_id(self, part: TextPart | ToolCallPart) -> TextPart | ToolCallPart:
        """Give an unnamed tool call the next id of THIS model; leave everything else alone."""
        if isinstance(part, ToolCallPart) and not part.tool_call_id:
            self._generated_ids += 1
            return replace(part, tool_call_id=f"scripted-call-{self._generated_ids}")
        return part
