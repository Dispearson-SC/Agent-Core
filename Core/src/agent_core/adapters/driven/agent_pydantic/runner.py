"""Driven adapter: AgentRunner over Pydantic AI.

Phase:   F1 (hooks, policy, audit) / F3 (deferred) / F5 (compaction) / F6 (MCP, skills)
Tasks:   docs/TASKS.md#t-f1-12
Implements: ports/agent_runner.py

THIS FILE IS THE SECURITY ENFORCEMENT POINT OF THE WHOLE SYSTEM
    Policy is consulted here. Audit is written here. Untrusted content is wrapped here.
    All three happen in `before_tool_execute`, i.e. BEFORE any side effect.

    Two of the five silent-bug areas in CLAUDE.md live in this file. Nothing here fails a
    test when it is wrong; it simply stops protecting.

WHY PYDANTIC AI PASSED THE SELECTION TEST (verified against its documentation)
    The question that decides any agent SDK: can I put my own code in the middle of a
    turn? Pydantic AI answers yes. Hooks exist at seven lifecycle stages, each with
    before_ / after_ / wrap_ / _error variants:

        run -> node -> model_request -> tool_validate -> tool_execute
            -> output_validate -> output_process

    `before_tool_execute` receives VALIDATED arguments, may modify them (return `args`),
    and may block execution entirely by raising `SkipToolExecution(result)`.

    That is the approval gate, without writing an approval gate. docs/DECISIONS.md#d3.

THE HOOK SKETCH (verify exact names against the installed version before coding)

    @hooks.on.before_tool_execute
    async def enforce(ctx, *, call, tool_def, args):
        decision = policy.decide(caller, call.tool_name, args)
        audit.record_tool_call(turn_id, caller, call.tool_name, args, decision)   # BEFORE
        if decision.effect is Effect.DENY:
            raise SkipToolExecution(refusal_result(decision.reason))
        return args

    @hooks.on.after_tool_execute
    async def wrap_untrusted(ctx, *, call, result):
        if is_untrusted(call.tool_name):        # mcp_*, web_*, media
            return wrap_in_delimiters(truncate(result, budget_for(call.tool_name)))
        return result
"""

from __future__ import annotations

from typing import Final

# Tool-name prefixes whose results are third-party text the model will read: the natural
# vector for indirect prompt injection. Their results are ALWAYS wrapped in
# untrusted-content delimiters. CLAUDE.md non-negotiable #4.
UNTRUSTED_PREFIXES: Final[tuple[str, ...]] = ("mcp_", "web_", "browser_", "media_")

# Smaller result budget for MCP than for local tools. Hermes uses 50K against 100K, with
# the stated reason that MCP servers routinely return un-paginated 20-50K payloads.
MCP_RESULT_BUDGET_CHARS: Final[int] = 50_000
LOCAL_RESULT_BUDGET_CHARS: Final[int] = 100_000

# Case-insensitive so a differently-cased tag cannot forge or prematurely close the
# boundary. A model that has read a hostile page will try exactly that.
UNTRUSTED_OPEN: Final[str] = "<untrusted-tool-output>"
UNTRUSTED_CLOSE: Final[str] = "</untrusted-tool-output>"


class PydanticAgentRunner:
    """PSEUDO-CODE ONLY - implement across F1, F3, F5, F6.

    CONSTRUCTION (F1)
        Build a Pydantic AI Agent per AgentProfile and CACHE it. Rebuilding per turn
        re-registers tools and re-resolves MCP servers on every request - slow, and it
        spawns stdio children far more often than intended.

        Cache key must include everything that changes behaviour: profile id, model, and a
        fingerprint of the toolset. A stale cached agent serving a changed profile is a
        permissions bug that survives a deploy.

    run() (F1)
        1. Resolve/build the agent for the profile.
        2. Attach hooks (see module docstring).
        3. Execute with `history` prepended.
        4. Translate the outcome:
             deferred requests present -> TurnOutcome(pending=...)
             otherwise                 -> TurnOutcome(result=...)

        NEVER generate the TurnId here - it is passed in, generated inside a DBOS step.

    resume() (F3)
        Rebuild the deferred-results structure and continue.

        THE SILENT BUG TO GUARD: every `tool_call_id` must be the id Pydantic AI issued.
        A regenerated or re-cased id is dropped WITHOUT an exception, and the agent asks
        the same question forever. Assert ids round-trip in an integration test.

    COMPACTION WIRING (F5)
        Attach the ContextEngine as a `ProcessHistory` capability. The engine owns the
        strategy; this adapter only wires it in and must not second-guess the trigger.

    MULTIMODAL (F7)
        Honour `profile.media.delivery`:
            BYTES      -> BinaryContent with the payload    (default, private)
            SIGNED_URL -> ImageUrl / AudioUrl               (provider downloads it)

        Verified: given a URL, Pydantic AI SENDS THE URL TO THE PROVIDER, which fetches
        the file itself. For evidence containing personal data that is a disclosure. The
        default is BYTES for that reason; SIGNED_URL is an explicit, written choice.

    STREAMING NOTE (F3, saves an afternoon)
        A DEFERRED tool call never reaches `event_stream_handler` - deferred means not
        executed, and only executing tools emit events. The pause context arrives via
        separate events (DeferredToolRequestsEvent). Without knowing this you will hunt a
        phantom bug in the UI.
    """

    def __init__(self) -> None:
        raise NotImplementedError("F1 - docs/TASKS.md#t-f1-12")
