"""Tool wrappers: an addon wraps an existing tool and calls the original (A6, D78).

Phase:   Phase 2 (addon system)
Tests:   Core/tests/unit/test_tool_wrappers.py
Implements: no port - startup wiring consumed by `LocalToolProvider`.

WHAT A WRAPPER IS
    The `super()` analogue: `provides.wrappers: [{tool, wrapper}]` in `addon.yaml` points at
    `wrappers/<wrapper>.py`, whose `wrap(original, arguments, ctx)` runs INSTEAD of the tool and
    decides whether, when and how to call it:

        async def wrap(original, arguments, ctx):
            result = await original()           # the tool (or the wrapper below this one)
            return result.upper()

    `original()` takes NO arguments on purpose: it runs the tool with exactly the arguments the
    policy decided on and the audit recorded. `arguments` is a read-only view for the wrapper's
    own logic. A wrapper may transform the result, add behaviour around the call, or not call the
    original at all; it cannot change what the original is called with.

WHY IT IS A TOOLSET AND NOT A DIFFERENT TOOL
    The wrapping happens in the toolset, below every hook the runner has (policy, audit, the
    tool journal, the untrusted-result fence). The tool the model is offered is the original's:
    same name, schema, description and metadata (so the same replay class), plus ONE addition,
    `metadata["wrapped_by"]`, the addon names innermost first. Because nothing about the tool
    moves, a policy rule on its name still decides, `before_tool_execute` still runs first (so a
    denied or approval-pending call never reaches a wrapper) and the audit row is written once,
    by the same hook as before, naming the wrappers (`domain/tool_wrapping.py`).

CHAINING AND FAILURE
    Several wrappers of one tool nest in activation order (dependency order, then the enabled
    list): the first loaded is innermost, the last loaded outermost, as a later Odoo module's
    override calls the earlier one's. An exception raised by wrapper code (not by the original
    tool) becomes a `ToolFailed` the model reads - the addon and the tool named, the exception's
    text kept out of it and logged instead. What the original tool raises is left alone, as it
    is without a wrapper: the very exception, also when a wrapper calls the original again (a
    retry) and lets the last failure through. Control-flow exceptions (retry, defer, approval)
    pass through.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    SkipToolExecution,
    SkipToolValidation,
    ToolFailed,
)
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from agent_core.domain.tool_wrapping import WRAPPED_BY_METADATA_KEY

__all__ = ["CallOriginal", "ToolWrapper", "WrapFunction", "WrappedToolset", "wrap_toolsets"]

_LOG = logging.getLogger(__name__)

CallOriginal = Callable[[], Awaitable[Any]]
# (original, read-only arguments, run context) -> the result; may be sync or async.
WrapFunction = Callable[[CallOriginal, Mapping[str, Any], RunContext[Any]], Any]

# Not failures of the wrapper: the framework's own signals, which must reach it unchanged.
_PASS_THROUGH: tuple[type[BaseException], ...] = (
    ModelRetry,
    ApprovalRequired,
    CallDeferred,
    ToolFailed,
    SkipToolExecution,
    SkipToolValidation,
)


@dataclass(frozen=True)
class ToolWrapper:
    """One `provides.wrappers` entry, resolved: whose it is, what it wraps, what to call."""

    addon: str
    tool: str
    call: WrapFunction


@dataclass
class WrappedToolset(WrapperToolset[None]):
    """`wrapped`, with the tools named in `wrappers` running through their wrappers.

    `wrappers[tool]` is in activation order: the first is the innermost.
    """

    wrappers: Mapping[str, tuple[ToolWrapper, ...]]

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        tools = await self.wrapped.get_tools(ctx)
        marked: dict[str, ToolsetTool[None]] = {}
        for name, tool in tools.items():
            chain = self.wrappers.get(name)
            if chain:
                names = list(dict.fromkeys(wrapper.addon for wrapper in chain))
                metadata = {**(tool.tool_def.metadata or {}), WRAPPED_BY_METADATA_KEY: names}
                tool = replace(tool, tool_def=replace(tool.tool_def, metadata=metadata))
            marked[name] = tool
        return marked

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        chain = self.wrappers.get(name)
        if not chain:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)

        original_errors: list[BaseException] = []

        async def innermost() -> Any:
            try:
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)
            except BaseException as error:
                original_errors.append(error)
                raise

        call: CallOriginal = innermost
        for wrapper in chain:
            call = _layer(wrapper, call, tool_args, ctx, original_errors)
        return await call()


def _layer(
    wrapper: ToolWrapper,
    below: CallOriginal,
    tool_args: Mapping[str, Any],
    ctx: RunContext[None],
    original_errors: list[BaseException],
) -> CallOriginal:
    async def run() -> Any:
        try:
            result = wrapper.call(below, MappingProxyType(dict(tool_args)), ctx)
            return await result if inspect.isawaitable(result) else result
        except _PASS_THROUGH:
            raise
        except Exception as error:
            if any(error is original for original in original_errors):
                # The tool's own failure, which a wrapper let through - the first one or a later
                # attempt of a wrapper that retries: as without it, same type and message.
                raise
            _LOG.warning(
                "tool_wrapper_failed: addon=%s tool=%s error=%s",
                wrapper.addon,
                wrapper.tool,
                type(error).__name__,
                exc_info=True,
            )
            raise ToolFailed(
                f"The tool {wrapper.tool!r} could not complete: the extension from addon "
                f"{wrapper.addon!r} failed ({type(error).__name__})."
            ) from error

    return run


def wrap_toolsets(
    toolsets: Sequence[AbstractToolset[None]], wrappers: Sequence[ToolWrapper]
) -> list[AbstractToolset[None]]:
    """`toolsets`, with each one holding a wrapped tool put behind a `WrappedToolset`.

    With no wrappers (or none for any tool in a function toolset) the very same objects come back.
    A toolset of any other kind is always put behind a `WrappedToolset`, which wraps only the tools
    it turns out to have.
    """
    if not wrappers:
        return list(toolsets)
    by_tool: dict[str, list[ToolWrapper]] = {}
    for wrapper in wrappers:
        by_tool.setdefault(wrapper.tool, []).append(wrapper)
    result: list[AbstractToolset[None]] = []
    for toolset in toolsets:
        if isinstance(toolset, FunctionToolset):
            mine = {tool: tuple(chain) for tool, chain in by_tool.items() if tool in toolset.tools}
        else:
            # Any other kind (combined, prefixed, MCP...) cannot list its tools without a run
            # context, so every declared wrapper goes with it and `get_tools` / `call_tool` apply
            # the ones whose tool it has (D79). Never "not a function toolset, so unwrapped".
            mine = {tool: tuple(chain) for tool, chain in by_tool.items()}
        result.append(WrappedToolset(wrapped=toolset, wrappers=mine) if mine else toolset)
    return result
