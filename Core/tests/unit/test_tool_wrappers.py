"""A6 (D78): an addon wraps an existing tool and calls the original (the `super()` analogue).

Subject: domain/tool_wrapping.py, adapters/driven/tools/wrapping.py, the `LocalToolProvider`
seat, `provides.wrappers` in the manifest and its registration, and the audit/runner seams that
keep a wrapped tool one tool: same name, schema, replay class, policy and audit identity.

THE RULES UNDER TEST. A wrapper is declared in the manifest and lives in the addon. It may wrap a
tool of Core or of an addon in its dependency closure, nothing else; anything else stops startup
with an `AddonRegistrationError` naming the addon. The tool keeps its name, schema and replay
class, policy is still decided on the tool name BEFORE the wrapper runs, the audit trail records
the call once and names the wrapper in a safe field, several wrappers chain in activation order,
and a wrapper that raises is a tool error the model reads, not a crash. With no wrappers the
toolset is the one it always was.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import CombinedToolset, FunctionToolset

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError, ManifestError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.manifest import parse_manifest
from agent_core.adapters.driven.addons_fs.registration import EMPTY, register_addons
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.persistence_pg.audit_repository import (
    PRESENCE_RECORDED,
    PgAuditSink,
)
from agent_core.adapters.driven.tools.provider import LocalToolProvider, build_tool_provider
from agent_core.adapters.driven.tools.wrapping import CallOriginal, ToolWrapper, wrap_toolsets
from agent_core.composition import TOOL_PACKAGES
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.tool_replay import ReplayPolicy, replay_policy, safe_tool_metadata
from agent_core.domain.tool_wrapping import (
    WRAPPED_BY_AUDIT_KEY,
    WRAPPED_BY_METADATA_KEY,
    WRAPPED_BY_RECEIVED_AUDIT_KEY,
    wrapped_by,
)
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId
from tests.fakes import ports as fakes

# --- the marker the audit trail reads -----------------------------------------------------------


def test_wrapped_by_reads_addon_names_and_nothing_else() -> None:
    assert wrapped_by(None) == () and wrapped_by({}) == ()
    assert wrapped_by({WRAPPED_BY_METADATA_KEY: ["shop", "tax_rules"]}) == ("shop", "tax_rules")
    for unsafe in ("shop", 3, ["Shop Corp"], ["a b"], [1], ["ok", "x" * 80], [""]):
        assert wrapped_by({WRAPPED_BY_METADATA_KEY: unsafe}) == ()


def test_the_audit_sink_keeps_the_wrapper_names_and_still_drops_unlisted_arguments() -> None:
    sink = PgAuditSink(  # type: ignore[arg-type]
        lambda: None,
        argument_allowlist={"echo": frozenset({"text"})},
        argument_presence_only={},
    )

    kept = sink._redact("echo", {"text": "hi", "secret": "x", WRAPPED_BY_AUDIT_KEY: ["shop"]})
    forged = sink._redact("echo", {"text": "hi", WRAPPED_BY_AUDIT_KEY: ["not a name!", "secret x"]})

    assert kept == {"text": "hi", WRAPPED_BY_AUDIT_KEY: ["shop"]}
    assert forged == {"text": "hi"}


# --- the wrapping toolset ------------------------------------------------------------------------


def echo(text: str) -> str:
    """Return the text."""
    return text


def _toolset() -> FunctionToolset[None]:
    return FunctionToolset([Tool(echo, metadata=safe_tool_metadata())])


def _wrapper(addon: str, fn: Any, tool: str = "echo") -> ToolWrapper:
    return ToolWrapper(addon=addon, tool=tool, call=fn)


def _drive(
    toolset: Any, *, capabilities: list[Any] | None = None, text: str = "hi", tool: str = "echo"
) -> tuple[list[str], str | None]:
    """Run an agent that calls `echo` once. Returns what the model read as the tool result."""
    seen: list[str] = []

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool, {"text": text})])
        seen.extend(str(part.content) for part in returns)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model_fn), toolsets=[toolset], capabilities=capabilities or [])
    result = asyncio.run(agent.run("go"))
    return seen, result.output


def test_a_wrapper_calls_the_original_and_changes_the_result() -> None:
    async def shout(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> str:
        return str(await original()).upper() + "!"

    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", shout)])

    seen, _ = _drive(wrapped)

    assert seen == ["HI!"]


def test_a_wrapper_may_be_a_plain_function_and_sees_the_arguments_and_the_context() -> None:
    log: list[object] = []

    def watch(original: CallOriginal, arguments: Mapping[str, Any], ctx: RunContext[None]) -> Any:
        log.append((dict(arguments), type(ctx).__name__))
        return original()  # a coroutine: awaited for it

    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", watch)])

    seen, _ = _drive(wrapped, text="abc")

    assert seen == ["abc"] and log == [({"text": "abc"}, "RunContext")]


def test_a_wrapper_cannot_change_what_the_original_is_called_with() -> None:
    async def tamper(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> str:
        arguments = dict(arguments)  # a copy was handed over; even writing to it changes nothing
        arguments["text"] = "tampered"
        return str(await original())

    async def tamper_in_place(
        original: CallOriginal, arguments: Mapping[str, Any], ctx: Any
    ) -> str:
        try:
            arguments["text"] = "tampered"  # type: ignore[index]
        except TypeError:
            pass
        return str(await original())

    for fn in (tamper, tamper_in_place):
        (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", fn)])
        assert _drive(wrapped, text="original")[0] == ["original"]


def test_the_wrapped_tool_keeps_its_name_schema_and_replay_class() -> None:
    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    plain = _toolset()
    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", passthrough)])
    captured: dict[str, Any] = {}

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["tools"] = {t.name: t for t in info.function_tools}
        return ModelResponse(parts=[TextPart("x")])

    asyncio.run(Agent(FunctionModel(model_fn), toolsets=[wrapped]).run("go"))
    offered = captured["tools"]["echo"]
    original = asyncio.run(_tool_defs(plain))["echo"]
    marked = asyncio.run(_tool_defs(wrapped))["echo"]

    assert set(captured["tools"]) == {"echo"}
    assert offered.parameters_json_schema == original.parameters_json_schema
    assert offered.description == original.description
    assert replay_policy(marked.metadata) is ReplayPolicy.SAFE
    assert wrapped_by(marked.metadata) == ("shop",)
    assert wrapped_by(original.metadata) == ()
    assert marked.name == original.name


async def _tool_defs(toolset: Any) -> dict[str, Any]:
    holder: dict[str, Any] = {}

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        holder.update({t.name: t for t in info.function_tools})
        return ModelResponse(parts=[TextPart("x")])

    await Agent(FunctionModel(model_fn), toolsets=[toolset]).run("go")
    return holder


def test_wrappers_chain_in_activation_order_the_last_loaded_outermost() -> None:
    log: list[str] = []

    def layer(name: str) -> Any:
        async def call(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> str:
            log.append(f"{name}:in")
            result = str(await original())
            log.append(f"{name}:out")
            return f"{name}({result})"

        return call

    wrapped = wrap_toolsets(
        [_toolset()], [_wrapper("first", layer("first")), _wrapper("second", layer("second"))]
    )[0]

    seen, _ = _drive(wrapped)

    assert seen == ["second(first(hi))"]
    assert log == ["second:in", "first:in", "first:out", "second:out"]


def test_a_wrapper_that_raises_is_a_tool_error_the_model_reads_not_a_crash() -> None:
    async def broken(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> str:
        raise RuntimeError("the secret token is hunter2")

    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", broken)])

    seen, output = _drive(wrapped)

    assert output == "done"
    assert len(seen) == 1 and "shop" in seen[0] and "echo" in seen[0]
    assert "hunter2" not in seen[0]


def test_a_failure_of_the_original_reaches_the_caller_as_it_would_without_a_wrapper() -> None:
    def failing(text: str) -> str:
        """Always fails."""
        raise ValueError("original failed")

    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    (wrapped,) = wrap_toolsets(
        [FunctionToolset([Tool(failing, name="echo")])], [_wrapper("shop", passthrough)]
    )

    with pytest.raises(ValueError, match="original failed"):
        _drive(wrapped)


def test_the_original_error_survives_a_wrapper_that_retries_the_original() -> None:
    calls: list[int] = []

    def flaky(text: str) -> str:
        """Fails differently each time."""
        calls.append(1)
        raise ValueError(f"original failed attempt {len(calls)}")

    async def retry(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        try:
            return await original()
        except ValueError:
            return await original()  # the second failure is the tool's too

    (wrapped,) = wrap_toolsets(
        [FunctionToolset([Tool(flaky, name="echo")])], [_wrapper("shop", retry)]
    )

    with pytest.raises(ValueError, match="original failed attempt 2"):
        _drive(wrapped)


def test_the_original_error_survives_a_chain_of_wrappers() -> None:
    def failing(text: str) -> str:
        """Always fails."""
        raise ValueError("original failed")

    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    (wrapped,) = wrap_toolsets(
        [FunctionToolset([Tool(failing, name="echo")])],
        [_wrapper("first", passthrough), _wrapper("second", passthrough)],
    )

    with pytest.raises(ValueError, match="original failed"):
        _drive(wrapped)


def test_a_wrapper_may_catch_the_failure_of_the_original() -> None:
    def failing(text: str) -> str:
        """Always fails."""
        raise ValueError("original failed")

    async def recover(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        try:
            return await original()
        except ValueError:
            return "recovered"

    (wrapped,) = wrap_toolsets(
        [FunctionToolset([Tool(failing, name="echo")])], [_wrapper("shop", recover)]
    )

    assert _drive(wrapped)[0] == ["recovered"]


def test_a_tool_nobody_wraps_is_untouched() -> None:
    async def never(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        raise AssertionError("wrapped the wrong tool")

    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", never, tool="other")])

    assert _drive(wrapped)[0] == ["hi"]


def test_a_tool_in_a_toolset_that_is_not_a_function_toolset_is_wrapped_too() -> None:
    """The tool a wrapper names may live behind any toolset kind (a combined or prefixed one,
    an MCP one): it is wrapped, never silently left running as if nobody had declared a wrapper."""

    async def shout(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return str(await original()).upper()

    (wrapped,) = wrap_toolsets([CombinedToolset([_toolset()])], [_wrapper("shop", shout)])

    seen, _ = _drive(wrapped)

    assert seen == ["HI"]


def test_a_wrapper_for_a_tool_no_toolset_here_has_leaves_every_toolset_alone() -> None:
    async def never(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        raise AssertionError("wrapped the wrong tool")

    (wrapped,) = wrap_toolsets([CombinedToolset([_toolset()])], [_wrapper("shop", never, "other")])

    assert _drive(wrapped)[0] == ["hi"]


def test_with_no_wrappers_the_toolsets_are_returned_as_they_were() -> None:
    toolsets = [_toolset()]

    assert wrap_toolsets(toolsets, [])[0] is toolsets[0]


# --- policy and audit stay on the tool name ------------------------------------------------------


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1", channel="http", tenant_id=TenantId("t-1"), roles=frozenset({"agent"})
    )


def _enforcement(rules: tuple[PolicyRule, ...]) -> tuple[Any, fakes.FakeAuditSink]:
    ruleset = RuleSet.for_caller(_caller(), rules)
    audit = fakes.FakeAuditSink()
    hooks = runner_module.PolicyEnforcement(
        policy=fakes.FakeToolPolicy(ruleset),
        rules=ruleset,
        audit=audit,
        turn_id=TurnId("turn-1"),
        caller=_caller(),
    )
    return hooks, audit


def test_policy_is_decided_on_the_tool_name_before_the_wrapper_runs() -> None:
    ran: list[str] = []

    async def spy(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        ran.append("wrapper")
        return await original()

    deny = PolicyRule(
        rule_id="r-no-echo", tool_pattern="echo", effect=Effect.DENY, reason="Echo is off."
    )
    hooks, audit = _enforcement((deny,))
    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", spy)])

    seen, _ = _drive(wrapped, capabilities=[hooks])

    assert ran == [] and "Echo is off." in seen[0]
    assert [call.kind for call in audit.calls] == ["tool_call"]


def test_an_approval_rule_still_defers_a_wrapped_tool_before_the_wrapper_runs() -> None:
    ran: list[str] = []

    async def spy(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        ran.append("wrapper")
        return await original()

    ask = PolicyRule(
        rule_id="r-ask-echo", tool_pattern="echo", effect=Effect.NEEDS_APPROVAL, reason="Ask."
    )
    hooks, audit = _enforcement((ask,))
    (wrapped,) = wrap_toolsets([_toolset()], [_wrapper("shop", spy)])

    from pydantic_ai import DeferredToolRequests

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("echo", {"text": "hi"})])

    agent = Agent(
        FunctionModel(model_fn),
        toolsets=[wrapped],
        capabilities=[hooks],
        output_type=[str, DeferredToolRequests],
    )
    result = asyncio.run(agent.run("go"))

    assert isinstance(result.output, DeferredToolRequests) and result.output.approvals
    assert ran == [] and [call.kind for call in audit.calls] == ["tool_call"]


def test_the_audit_trail_records_a_wrapped_call_once_naming_the_wrappers() -> None:
    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))
    wrapped = wrap_toolsets(
        [_toolset()], [_wrapper("first", passthrough), _wrapper("second", passthrough)]
    )[0]

    seen, _ = _drive(wrapped, capabilities=[hooks])

    assert seen == ["hi"] and [call.kind for call in audit.calls] == ["tool_call"]
    _turn, _caller_recorded, tool_name, arguments, _decision = audit.calls[0].payload
    assert tool_name == "echo"
    assert arguments == {"text": "hi", WRAPPED_BY_AUDIT_KEY: ["first", "second"]}


def test_an_unwrapped_call_is_audited_exactly_as_before() -> None:
    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))

    _drive(_toolset(), capabilities=[hooks])

    assert audit.calls[0].payload[3] == {"text": "hi"}


def _loose_toolset() -> FunctionToolset[None]:
    """A tool that accepts extra keyword arguments, so a model CAN send it a key of its choosing."""

    def echo(text: str, **extra: Any) -> str:
        """Return the text."""
        return text

    return FunctionToolset([Tool(echo, metadata=safe_tool_metadata())])


def _drive_forging(
    toolset: Any,
    capabilities: list[Any],
    forged: object,
    extra: Mapping[str, object] | None = None,
) -> None:
    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            args = {"text": "hi", WRAPPED_BY_AUDIT_KEY: forged, **(extra or {})}
            return ModelResponse(parts=[ToolCallPart("echo", args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model_fn), toolsets=[toolset], capabilities=capabilities)
    asyncio.run(agent.run("go"))


def test_a_marker_the_model_forges_never_reaches_the_audit_of_an_unwrapped_tool() -> None:
    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))

    _drive_forging(_loose_toolset(), [hooks], ["decision_desk"])

    audited = audit.calls[0].payload[3]
    assert WRAPPED_BY_AUDIT_KEY not in audited  # not as the runner's marker
    # ... and the forged value is not lost either: it is labelled as what the tool received.
    assert audited == {"text": "hi", WRAPPED_BY_RECEIVED_AUDIT_KEY: ["decision_desk"]}


def test_the_marker_the_runner_sets_replaces_one_the_model_forges() -> None:
    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))
    (wrapped,) = wrap_toolsets([_loose_toolset()], [_wrapper("shop", passthrough)])

    _drive_forging(wrapped, [hooks], ["evil_addon"])

    assert audit.calls[0].payload[3][WRAPPED_BY_AUDIT_KEY] == ["shop"]


def _received_toolset(received: list[dict[str, Any]]) -> FunctionToolset[None]:
    """A tool that records the keyword arguments it actually got."""

    def echo(text: str, **extra: Any) -> str:
        """Return the text."""
        received.append(dict(extra))
        return text

    return FunctionToolset([Tool(echo, metadata=safe_tool_metadata())])


def test_the_audit_carries_an_argument_the_tool_received_under_the_reserved_name() -> None:
    """A7b: the runner's marker owns `_wrapped_by`; what the tool was handed under that name is
    not lost, it is audited as `_wrapped_by_received`."""
    received: list[dict[str, Any]] = []
    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))

    _drive_forging(_received_toolset(received), [hooks], ["decision_desk"])

    assert received == [{WRAPPED_BY_AUDIT_KEY: ["decision_desk"]}]  # the tool did receive it
    assert audit.calls[0].payload[3] == {
        "text": "hi",
        WRAPPED_BY_RECEIVED_AUDIT_KEY: ["decision_desk"],
    }


def test_a_received_label_the_model_sends_is_dropped_so_it_cannot_pose_as_the_runners() -> None:
    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))

    # Only the received-label is forged: nothing was sent under the reserved name itself.
    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            args = {"text": "hi", WRAPPED_BY_RECEIVED_AUDIT_KEY: ["fake_addon"]}
            return ModelResponse(parts=[ToolCallPart("echo", args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model_fn), toolsets=[_loose_toolset()], capabilities=[hooks])
    asyncio.run(agent.run("go"))

    assert audit.calls[0].payload[3] == {"text": "hi"}


def test_forging_both_reserved_keys_audits_only_what_arrived_under_the_marker_name() -> None:
    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))
    (wrapped,) = wrap_toolsets([_loose_toolset()], [_wrapper("shop", passthrough)])

    _drive_forging(
        wrapped, [hooks], ["evil_addon"], extra={WRAPPED_BY_RECEIVED_AUDIT_KEY: ["other_evil"]}
    )

    assert audit.calls[0].payload[3] == {
        "text": "hi",
        WRAPPED_BY_AUDIT_KEY: ["shop"],
        WRAPPED_BY_RECEIVED_AUDIT_KEY: ["evil_addon"],
    }


def test_the_audit_of_a_wrapped_call_has_the_runner_marker_and_the_received_argument() -> None:
    async def passthrough(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> Any:
        return await original()

    received: list[dict[str, Any]] = []
    allow = PolicyRule(rule_id="r-echo", tool_pattern="echo", effect=Effect.ALLOW, reason="ok")
    hooks, audit = _enforcement((allow,))
    (wrapped,) = wrap_toolsets([_received_toolset(received)], [_wrapper("shop", passthrough)])

    _drive_forging(wrapped, [hooks], ["evil_addon"])

    assert audit.calls[0].payload[3] == {
        "text": "hi",
        WRAPPED_BY_AUDIT_KEY: ["shop"],
        WRAPPED_BY_RECEIVED_AUDIT_KEY: ["evil_addon"],
    }


def test_the_sink_audits_the_received_argument_only_when_the_tool_allows_the_reserved_name() -> (
    None
):
    sink = PgAuditSink(  # type: ignore[arg-type]
        lambda: None,
        argument_allowlist={"echo": frozenset({"text", "_wrapped_by"}), "other": frozenset({"x"})},
        argument_presence_only={"quiet": frozenset({"_wrapped_by"})},
    )
    row = {"text": "hi", WRAPPED_BY_RECEIVED_AUDIT_KEY: ["a"], WRAPPED_BY_AUDIT_KEY: ["shop"]}

    assert sink._redact("echo", row) == {
        "text": "hi",
        WRAPPED_BY_RECEIVED_AUDIT_KEY: ["a"],
        WRAPPED_BY_AUDIT_KEY: ["shop"],
    }
    assert sink._redact("other", row) == {WRAPPED_BY_AUDIT_KEY: ["shop"]}
    assert sink._redact("quiet", row)[WRAPPED_BY_RECEIVED_AUDIT_KEY] == PRESENCE_RECORDED


# --- the provider ------------------------------------------------------------------------------


def _profile(*toolsets: str) -> AgentProfile:
    return AgentProfile.from_mapping(
        {
            "id": "p",
            "description": "d",
            "persona": "x",
            "model": "m",
            "toolsets": list(toolsets),
            "max_iterations": 3,
            "max_cost_usd": "0.10",
        }
    )


def test_the_provider_wraps_a_tool_only_for_a_profile_that_has_it() -> None:
    async def shout(original: CallOriginal, arguments: Mapping[str, Any], ctx: Any) -> str:
        return str(await original()).upper()

    provider = LocalToolProvider(
        {"a": _toolset, "b": lambda: FunctionToolset([])}, wrappers=[_wrapper("shop", shout)]
    )

    with_tool = asyncio.run(provider.toolset_for(_profile("a")))
    without = asyncio.run(provider.toolset_for(_profile("b")))

    assert _drive(with_tool)[0] == ["HI"]
    assert isinstance(without, CombinedToolset) and all(
        type(inner).__name__ == "FunctionToolset" for inner in without.toolsets
    )
    assert asyncio.run(provider.tool_names_for(_profile("a"))) == ("echo",)


def test_a_provider_without_wrappers_composes_what_it_always_did() -> None:
    provider = build_tool_provider({"a": _toolset})

    toolset = asyncio.run(provider.toolset_for(_profile("a")))

    assert isinstance(toolset, CombinedToolset)
    assert [type(inner).__name__ for inner in toolset.toolsets] == ["FunctionToolset"]


# --- the manifest ------------------------------------------------------------------------------

HEAD = "name: shop\nversion: 1.0.0\ncore: '>=0'\n"


def test_the_manifest_declares_wrappers() -> None:
    manifest = parse_manifest(
        HEAD + "depends: [base]\nprovides:\n  wrappers:\n    - {tool: echo, wrapper: shout}\n"
    )

    (declared,) = manifest.provides.wrappers
    assert (declared.tool, declared.wrapper) == ("echo", "shout")
    assert parse_manifest(HEAD).provides.wrappers == ()


@pytest.mark.parametrize(
    "entry",
    [
        "{tool: echo}",
        "{wrapper: shout}",
        "{tool: echo, wrapper: ../shout}",
        "{tool: 'a b', wrapper: shout}",
        "{tool: echo, wrapper: shout, extra: 1}",
    ],
)
def test_a_malformed_wrapper_entry_is_a_manifest_error(entry: str) -> None:
    with pytest.raises(ManifestError, match="provides.wrappers"):
        parse_manifest(HEAD + f"provides:\n  wrappers: [{entry}]\n")


def test_the_same_wrapper_declared_twice_is_a_manifest_error() -> None:
    entry = "{tool: echo, wrapper: shout}"

    with pytest.raises(ManifestError, match="provides.wrappers"):
        parse_manifest(HEAD + f"provides:\n  wrappers: [{entry}, {entry}]\n")


# --- registration ------------------------------------------------------------------------------

TOOLSET = '''
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


def {tool}(text: str) -> str:
    """Return the text."""
    return text


def build_toolset() -> FunctionToolset[None]:
    return FunctionToolset([Tool({tool})])
'''

WRAP = """
async def wrap(original, arguments, ctx):
    return await original()
"""


def _addon(root: Path, name: str, manifest: str = "", **files: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\n{dedent(manifest)}", encoding="utf-8"
    )
    for key, text in files.items():
        target = directory / key.replace("__", "/").replace("_dot_", ".")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text), encoding="utf-8")


def _register(root: Path, enabled: list[str]) -> Any:
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    return register_addons(
        addons,
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=("http", "cli"),
    )


def _refused(root: Path, enabled: list[str]) -> AddonRegistrationError:
    with pytest.raises(AddonRegistrationError) as caught:
        _register(root, enabled)
    return caught.value


def _base(root: Path, name: str = "base", tool: str = "echo_a", depends: str = "") -> None:
    manifest = f"{depends}provides:\n  toolsets: [{tool}]\n"
    _addon(root, name, manifest, **{f"toolsets__{tool}_dot_py": TOOLSET.format(tool=tool)})


def _wrapping(
    root: Path, name: str, tool: str, depends: list[str], wrapper: str = WRAP, **extra: str
) -> None:
    _addon(
        root,
        name,
        f"depends: [{', '.join(depends)}]\nprovides:\n  wrappers: [{{tool: {tool}, wrapper: w}}]\n",
        wrappers__w_dot_py=wrapper,
        **extra,
    )


def test_with_no_wrappers_the_contributions_carry_none() -> None:
    assert EMPTY.wrappers == ()


def test_an_addon_registers_a_wrapper_for_a_tool_of_an_addon_it_depends_on(tmp_path: Path) -> None:
    _base(tmp_path)
    _wrapping(tmp_path, "shop", "echo_a", ["base"])

    contributions = _register(tmp_path, ["base", "shop"])

    (wrapper,) = contributions.wrappers
    assert (wrapper.addon, wrapper.tool) == ("shop", "echo_a") and callable(wrapper.call)


def test_an_addon_may_wrap_a_tool_of_core(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        "provides:\n  wrappers: [{tool: pricing_quote, wrapper: w}]\n",
        wrappers__w_dot_py=WRAP,
    )

    (wrapper,) = _register(tmp_path, ["shop"]).wrappers

    assert wrapper.tool == "pricing_quote"


def test_the_dependency_closure_is_transitive(tmp_path: Path) -> None:
    _base(tmp_path)
    _addon(tmp_path, "middle", "depends: [base]\n")
    _wrapping(tmp_path, "shop", "echo_a", ["middle"])

    (wrapper,) = _register(tmp_path, ["base", "middle", "shop"]).wrappers

    assert wrapper.addon == "shop"


def test_wrappers_come_in_activation_order(tmp_path: Path) -> None:
    _base(tmp_path)
    _wrapping(tmp_path, "zeta", "echo_a", ["base"])
    _wrapping(tmp_path, "alpha", "echo_a", ["base"])

    first = _register(tmp_path, ["base", "zeta", "alpha"]).wrappers
    second = _register(tmp_path, ["base", "alpha", "zeta"]).wrappers

    assert [w.addon for w in first] == ["zeta", "alpha"]
    assert [w.addon for w in second] == ["alpha", "zeta"]


def test_wrapping_a_tool_nothing_provides_stops_startup_naming_the_addon(tmp_path: Path) -> None:
    _wrapping(tmp_path, "shop", "ghost_tool", [])

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.wrappers"
    assert "ghost_tool" in str(error)


def test_wrapping_a_tool_of_an_addon_outside_the_closure_stops_startup(tmp_path: Path) -> None:
    _base(tmp_path)
    _addon(tmp_path, "unrelated")
    _wrapping(tmp_path, "shop", "echo_a", ["unrelated"])

    error = _refused(tmp_path, ["base", "unrelated", "shop"])

    assert error.addon == "shop" and error.field == "provides.wrappers"
    assert "base" in str(error) and "depend" in str(error)


def test_an_addon_cannot_wrap_its_own_tool(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        "provides:\n  toolsets: [echo_a]\n  wrappers: [{tool: echo_a, wrapper: w}]\n",
        toolsets__echo_a_dot_py=TOOLSET.format(tool="echo_a"),
        wrappers__w_dot_py=WRAP,
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and "own" in str(error)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (None, "wrappers/w.py"),
        ("x = 1\n", "wrap"),
        ("wrap = 3\n", "wrap"),
        ("raise RuntimeError('boom')\n", "failed to import"),
    ],
)
def test_a_wrapper_file_must_exist_import_and_define_wrap(
    tmp_path: Path, source: str | None, reason: str
) -> None:
    files = {} if source is None else {"wrappers__w_dot_py": source}
    _addon(
        tmp_path,
        "shop",
        "provides:\n  wrappers: [{tool: pricing_quote, wrapper: w}]\n",
        **files,
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.wrappers"
    assert reason in str(error)


# --- through the composition root ------------------------------------------------------------


def test_the_container_serves_a_wrapped_tool_for_the_sample_addons() -> None:
    from agent_core.composition import Settings, build_container

    from_env = load_addons(
        enabled=["sample_base", "sample_shout"],
        addons_root=Path(__file__).resolve().parents[3] / "addons",
        core_version="0.0.0",
    )
    container = build_container(Settings(addons=from_env))
    try:
        profile = container.profiles["sample_assistant"]
        toolset = asyncio.run(container.tools.toolset_for(profile))
        seen, _ = _drive(toolset, text="hello", tool="sample_echo")
        assert seen == ["HELLO"]
    finally:
        container.domain_pool.close()
        container.audit_pool.close()
