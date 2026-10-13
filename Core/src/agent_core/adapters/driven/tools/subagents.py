"""Driven adapter: `spawn_subagent` - the deferred tool that starts a child agent (C28, D62).

Phase:   Agent-comms v2, wave 5
Tests:   Core/tests/unit/test_spawn_subagent_tool.py
Composes: nothing. Like `peers.py` it names no port and imports no adapter.

`spawn_subagent(agent, task, background?, continue_session?)` is the sibling of `ask_peer` and
shares its mechanism: the turn suspends on an externally executed call, the workflow turns it into
a mailbox row and a durable child workflow, and the answer resolves the call as the C8 envelope.
Read `peers.py` for WHY THIS FUNCTION NEVER RETURNS A VALUE and WHAT TRAVELS IN THE METADATA; all of
it holds here, with `agent` and `task` where `ask_peer` has `target` and `question`.

What differs is who is asked for and what it gets: a subagent is started to do ONE task for this
turn, so it receives the task and nothing of this conversation, and it runs as its own profile with
that profile's own tools. This turn OWNS it (cancelled with it, shown under it in the task graph).

The menu is generated per turn like the peers' (C14): the `agent` argument is an enum of the agents
this profile may spawn, the caller's policy rules may deny an agent (and `deny` wins), and with
nobody to spawn, or on a turn that is itself too deep to spawn (C29), the tool is not offered.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.adapters.driven.tools.peers import _without_parameter
from agent_core.domain.tool_replay import safe_tool_metadata

_AVAILABLE_SUBAGENTS = "Available subagents (use the id as `agent`):"


def spawn_subagent(
    agent: str,
    task: str,
    continue_session: str | None = None,
    background: bool = False,
) -> str:
    """Start a subagent `agent` to do one task for you and report back. Always defers.

    The return type is `str` only so a type checker can tell what the AGENT eventually sees once
    the deferred call resolves: the subagent's result inside the untrusted-content delimiters.
    Every actual call raises before reaching that return.

    Args:
        agent: The id of the subagent to start.
        task: What it must do, with everything it needs: it sees this and nothing else of your
            conversation, and it cannot ask you questions.
        continue_session: To give an earlier subagent of the same kind a follow-up, the `session`
            of its earlier result. It then remembers that work. Omit it to start fresh.
        background: Set true for a long task you do not need the result of to carry on. The call
            returns at once with a handle (status "running"); the result arrives later as a new
            message in this conversation. Omit it to wait for the result in this turn.
    """
    metadata: dict[str, Any] = {"agent": agent, "task": task}
    if continue_session is not None:
        metadata["continue_session"] = continue_session
    if background:
        metadata["background"] = True
    raise CallDeferred(metadata=metadata)


async def _prepare_spawn_subagent(
    ctx: RunContext[Any], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Generate the tool's `agent` enum and description for THIS turn, or hide the tool.

    A turn without a computed menu (`subagent_catalogue is None`: a runner without a profile
    directory, a test) keeps the static tool. Otherwise the tool lists exactly the agents the gate
    and the policy allow; with nobody available, or when this turn may not spawn (`can_spawn` is
    false: it is a subagent already at its depth limit), it is not offered at all.
    """
    deps = ctx.deps
    if not isinstance(deps, TurnContext):
        return tool_def
    if not deps.can_spawn:
        return None
    if not deps.allow_continuation:
        tool_def = _without_parameter(tool_def, "continue_session")
    if not deps.allow_background:
        tool_def = _without_parameter(tool_def, "background")
    catalogue = deps.subagent_catalogue
    if catalogue is None:
        return tool_def
    if not catalogue:
        return None
    schema = copy.deepcopy(tool_def.parameters_json_schema)
    schema["properties"]["agent"]["enum"] = [entry.agent_id for entry in catalogue]
    lines = "\n".join(f"- {entry.agent_id}: {entry.summary}" for entry in catalogue)
    base = (tool_def.description or "").rstrip()
    description = f"{base}\n\n{_AVAILABLE_SUBAGENTS}\n{lines}"
    return replace(tool_def, parameters_json_schema=schema, description=description)


def build_toolset() -> FunctionToolset[None]:
    """This mechanism's toolset - exactly `spawn_subagent`. A fresh one per call, for the reason
    `peers.build_toolset` gives."""
    # C30. The body only defers (`CallDeferred`): running it again does nothing.
    return FunctionToolset(
        [Tool(spawn_subagent, prepare=_prepare_spawn_subagent, metadata=safe_tool_metadata())]
    )
