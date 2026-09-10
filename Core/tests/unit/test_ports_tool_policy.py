"""ToolPolicy port shape - a SILENT-BUG AREA guarded structurally, not behaviourally.

A policy hole never fails a functional test; it just never fires. So the contract is
frozen here as a shape, before any adapter implements it.

Two properties are asserted, and both are load-bearing:

1. THE ASYNC SPLIT (D13). `load_rules` is the single I/O call and the only awaitable
   member. `filter_toolset` and `decide` stay sync because they are pure functions over
   an already-loaded snapshot - `decide` runs inside `before_tool_execute`, on every tool
   call, and a coroutine there buys an await point with nothing behind it.

2. NO CALLER ON THE SYNC METHODS. `RuleSet` carries the `subject_roles` and `channel` it
   was narrowed for, and `RuleSet.applicable` asks for a tool name alone. Passing a
   caller alongside the snapshot would type-check perfectly while answering caller A's
   rules about caller B - a silent authorisation bug in the one component whose failures
   only surface the day they matter. The snapshot IS the answer for exactly one caller.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, cast

import pytest

from agent_core.domain.policy import PolicyDecision, RuleSet
from agent_core.domain.turn import CallerIdentity
from agent_core.ports.tool_policy import ToolPolicy

SYNC_METHODS = ("filter_toolset", "decide")


def _member(name: str) -> Callable[..., Any]:
    member = getattr(ToolPolicy, name)
    assert callable(member), f"ToolPolicy.{name} is not callable"
    return cast("Callable[..., Any]", member)


def _signature(name: str) -> inspect.Signature:
    # eval_str resolves the port's `from __future__ import annotations` strings back into
    # the real domain types, so the assertions below compare types and not spelling.
    return inspect.signature(_member(name), eval_str=True)


def _parameters(name: str) -> list[inspect.Parameter]:
    return [p for p_name, p in _signature(name).parameters.items() if p_name != "self"]


@pytest.mark.silent
def test_load_rules_is_the_only_awaitable_member() -> None:
    """D13: one I/O call per turn, paid once, not once per tool call."""
    awaitable = {
        name
        for name, member in inspect.getmembers(ToolPolicy, inspect.isfunction)
        if inspect.iscoroutinefunction(member)
    }

    assert awaitable == {"load_rules"}


@pytest.mark.silent
def test_load_rules_narrows_by_the_caller_and_returns_the_snapshot() -> None:
    """The caller is named exactly once on this port: at the single narrowing point."""
    parameters = _parameters("load_rules")

    assert [p.name for p in parameters] == ["caller"]
    assert parameters[0].annotation is CallerIdentity
    assert _signature("load_rules").return_annotation is RuleSet


@pytest.mark.silent
@pytest.mark.parametrize("name", SYNC_METHODS)
def test_sync_methods_are_not_coroutines(name: str) -> None:
    """Pure over the snapshot, so there is nothing to await (D13)."""
    assert not inspect.iscoroutinefunction(_member(name))


@pytest.mark.silent
@pytest.mark.parametrize("name", SYNC_METHODS)
def test_sync_methods_take_no_caller_identity(name: str) -> None:
    """The snapshot already carries the narrowing; a second subject can only disagree."""
    parameters = _parameters(name)

    assert not any(p.annotation is CallerIdentity for p in parameters), (
        f"ToolPolicy.{name} still takes a CallerIdentity: nothing then stops it being "
        f"asked about caller B using caller A's rules."
    )
    assert not any("caller" in p.name for p in parameters)


@pytest.mark.silent
@pytest.mark.parametrize("name", SYNC_METHODS)
def test_sync_methods_answer_over_an_already_narrowed_snapshot(name: str) -> None:
    """First argument is the RuleSet: the snapshot is the subject of the question."""
    parameters = _parameters(name)

    assert parameters[0].name == "rules"
    assert parameters[0].annotation is RuleSet


@pytest.mark.silent
def test_filter_toolset_maps_names_to_names() -> None:
    parameters = _parameters("filter_toolset")

    assert [p.name for p in parameters] == ["rules", "tool_names"]
    assert parameters[1].annotation == tuple[str, ...]
    assert _signature("filter_toolset").return_annotation == tuple[str, ...]


@pytest.mark.silent
def test_decide_asks_about_one_call_with_its_arguments() -> None:
    """Arguments are the whole point of `decide`: "transfer $10" is not "transfer $10M"."""
    parameters = _parameters("decide")

    assert [p.name for p in parameters] == ["rules", "tool_name", "arguments"]
    assert parameters[1].annotation is str
    assert parameters[2].annotation == dict[str, object]
    assert _signature("decide").return_annotation is PolicyDecision


@pytest.mark.silent
def test_the_snapshot_really_carries_the_narrowing_the_port_drops() -> None:
    """The premise of every assertion above. If this fails, the caller must come back."""
    assert {"subject_roles", "channel"} <= set(RuleSet.__dataclass_fields__)

    applicable = inspect.signature(RuleSet.applicable)
    assert [name for name in applicable.parameters if name != "self"] == ["tool_name"]
