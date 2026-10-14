"""Intent plans: a typed intent answered by a declared, read-only tool plan (D97, S6 layer B). Pure.

A router (D95) may answer `intent(name, arguments)`. When the profile the turn started on opted in
(`routing.intents: true`) and an addon declared a plan for that intent (`provides.intents`), Core
runs the PLAN instead of the model's tool loop:

  * the plan's owning profile is the profile the turn runs under (it must be a route target the
    starting profile allows, exactly like a `route`),
  * each step calls one tool of that profile with arguments that are constants, the router's
    extracted arguments or a few context values; every call goes through the same policy, audit,
    wrappers and budgets as a call the model made,
  * the reply is either `compose` (ONE model call of the owning profile, offered no tools, told to
    use only the results) or `template` (deterministic text with the results put in).

Any failure (an argument missing, a tool refused, an error result, a timeout) is a FALLBACK: the
turn runs the normal model path of the owning profile from scratch, with the reason recorded. A plan
never invents data and never widens what a turn may do; only read-only (replay-safe) tools may be in
one, which registration checks.

This module holds the plan's shape, its parser, argument resolution and template rendering. It knows
no tool, no model and no addon.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CONTEXT_VALUES",
    "MAX_PLAN_STEPS",
    "PLAN_ARGUMENT_INVALID",
    "PLAN_NEEDS_APPROVAL",
    "PLAN_OVER_BUDGET",
    "PLAN_POLICY_DENIED",
    "PLAN_NOT_WIRED",
    "PLAN_REPLY_FAILED",
    "PLAN_TIMEOUT",
    "PLAN_TOOL_ERROR",
    "PLAN_TOOL_NOT_GRANTED",
    "PLAN_TOOL_NOT_READ_ONLY",
    "ArgumentSource",
    "IntentCall",
    "IntentPlan",
    "IntentPlanError",
    "IntentRecord",
    "PlanArgumentError",
    "PlanStep",
    "PlanToolResult",
    "PlannedCall",
    "ReplyMode",
    "SourceKind",
    "parse_intent_plans",
    "render_template",
    "resolve_arguments",
]

MAX_PLAN_STEPS: Final[int] = 8
"""A plan is a handful of reads. More than this is a job for the model's own loop."""

CONTEXT_VALUES: Final[frozenset[str]] = frozenset({"today", "channel", "tenant_id"})
"""The context values a step argument may name: the UTC date the plan runs (ISO), the channel and
the tenant of the turn. Nothing a person typed and nothing a model wrote."""

# Why a plan fell back to the normal path (`IntentRecord.fallback_reason`). Fixed codes, or
# `error:<ExceptionClass>` (domain/routing.py `error_reason`) for anything unexpected.
PLAN_ARGUMENT_INVALID: Final[str] = "argument_invalid"
PLAN_TOOL_NOT_GRANTED: Final[str] = "tool_not_granted"
PLAN_TOOL_NOT_READ_ONLY: Final[str] = "tool_not_read_only"
PLAN_POLICY_DENIED: Final[str] = "policy_denied"
PLAN_NEEDS_APPROVAL: Final[str] = "needs_approval"
PLAN_TOOL_ERROR: Final[str] = "tool_error"
PLAN_TIMEOUT: Final[str] = "timeout"
PLAN_OVER_BUDGET: Final[str] = "over_budget"
PLAN_REPLY_FAILED: Final[str] = "reply_failed"
# The turn's recorded route handed an intent on but this process has no executor bound (routing was
# turned off between the run and its recovery): the plan falls back, keeping the step shape.
PLAN_NOT_WIRED: Final[str] = "not_wired"

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PLAN_KEYS = frozenset({"profile", "steps", "reply", "template"})
_STEP_KEYS = frozenset({"id", "tool", "arguments"})
_SOURCE_KEYS = frozenset({"argument", "context", "constant", "default"})


class IntentPlanError(ValueError):
    """A plans document that cannot work. The message names the intent and what is wrong."""


class PlanArgumentError(ValueError):
    """A step's argument cannot be resolved (the router did not extract it and it has no default).
    The message names the argument, never its value."""


class ReplyMode(StrEnum):
    COMPOSE = "compose"
    TEMPLATE = "template"


class SourceKind(StrEnum):
    CONSTANT = "constant"
    ARGUMENT = "argument"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class ArgumentSource:
    """Where one argument of a step comes from. `value` is the constant (or the default of an
    `argument` source when `has_default`); `name` the router argument or context value."""

    kind: SourceKind
    value: Any = None
    name: str = ""
    has_default: bool = False


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One tool call of a plan. `id` names its result in a template (the tool name by default)."""

    id: str
    tool: str
    arguments: tuple[tuple[str, ArgumentSource], ...] = ()


@dataclass(frozen=True, slots=True)
class IntentPlan:
    intent: str
    profile_id: str
    steps: tuple[PlanStep, ...]
    reply: ReplyMode = ReplyMode.COMPOSE
    template: str | None = None

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(step.tool for step in self.steps))


@dataclass(frozen=True, slots=True)
class IntentCall:
    """The intent a router answered and the arguments it extracted, carried from the route step to
    the plan (`RoutedTurn.intent`). The arguments are the router's values: never audited."""

    intent: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A plain dict: the call is pickled into the workflow's operation log.
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """One step with its arguments resolved: what the executor runs."""

    step_id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class PlanToolResult:
    """How one planned call ended. `content` is the result as the model would read it (text);
    `failure` the plan fallback code when it did not end well (`ok` is False)."""

    step_id: str
    tool: str
    content: str = ""
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class IntentRecord:
    """The audit row of one plan (`RouteAudit.record_intent`): whether the plan answered the turn,
    how many tools it called, its reply mode and, when it fell back, why. No argument values."""

    intent: str
    profile_id: str
    plan_used: bool
    tool_count: int
    reply_mode: str | None = None
    fallback_reason: str | None = None


# --- parsing --------------------------------------------------------------------------------------


def _refuse(where: str, reason: str) -> IntentPlanError:
    return IntentPlanError(f"{where}: {reason}")


def _source(where: str, raw: object) -> ArgumentSource:
    """A constant, or a one-of `{argument | context | constant}` mapping (`default` only with
    `argument`). A mapping that names none of them is a constant mapping."""
    if not isinstance(raw, Mapping) or not (set(raw) & {"argument", "context", "constant"}):
        return ArgumentSource(SourceKind.CONSTANT, value=raw)
    extra = set(raw) - _SOURCE_KEYS
    if extra:
        raise _refuse(where, f"unknown keys {sorted(map(str, extra))}")
    named = [key for key in ("argument", "context", "constant") if key in raw]
    if len(named) != 1:
        raise _refuse(where, "names more than one of argument, context and constant")
    kind = SourceKind(named[0])
    if "default" in raw and kind is not SourceKind.ARGUMENT:
        raise _refuse(where, "only an `argument` source may have a default")
    if kind is SourceKind.CONSTANT:
        return ArgumentSource(kind, value=raw["constant"])
    name = raw[kind.value]
    if not isinstance(name, str) or not _NAME.match(name):
        raise _refuse(where, f"{kind.value} must be a name")
    if kind is SourceKind.CONTEXT and name not in CONTEXT_VALUES:
        raise _refuse(where, f"context {name!r} is not one of {sorted(CONTEXT_VALUES)}")
    return ArgumentSource(
        kind, name=name, value=raw.get("default"), has_default="default" in raw
    )


def _step(where: str, raw: object) -> PlanStep:
    if not isinstance(raw, Mapping):
        raise _refuse(where, "must be a mapping with `tool`")
    extra = set(raw) - _STEP_KEYS
    if extra:
        raise _refuse(where, f"unknown keys {sorted(map(str, extra))}")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not _NAME.match(tool):
        raise _refuse(where, "`tool` must name a tool")
    step_id = raw.get("id", tool)
    if not isinstance(step_id, str) or not _NAME.match(step_id):
        raise _refuse(where, "`id` must be a name")
    arguments = raw.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        raise _refuse(where, "`arguments` must be a mapping")
    resolved: list[tuple[str, ArgumentSource]] = []
    for name, value in arguments.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise _refuse(where, f"argument name {name!r} is not a name")
        resolved.append((name, _source(f"{where}, argument {name!r}", value)))
    return PlanStep(id=step_id, tool=tool, arguments=tuple(resolved))


def _template_fields(template: str) -> set[str]:
    fields: set[str] = set()
    for _, name, spec, conversion in string.Formatter().parse(template):
        if name is None:
            continue
        if not _NAME.match(name) or spec or conversion:
            raise ValueError(f"placeholder {{{name}}} must be a bare step id")
        fields.add(name)
    return fields


def _plan(intent: str, raw: object, source: str) -> IntentPlan:
    where = f"{source}, intent {intent!r}"
    if not _NAME.match(intent):
        raise _refuse(where, "an intent name is letters, digits and underscores")
    if not isinstance(raw, Mapping):
        raise _refuse(where, "must be a mapping with `profile` and `steps`")
    extra = set(raw) - _PLAN_KEYS
    if extra:
        raise _refuse(where, f"unknown keys {sorted(map(str, extra))}")
    profile = raw.get("profile")
    if not isinstance(profile, str) or not _PROFILE.match(profile):
        raise _refuse(where, "`profile` must name the profile that owns the plan")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _refuse(where, "`steps` must be a non-empty list")
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise _refuse(where, f"has {len(raw_steps)} steps; at most {MAX_PLAN_STEPS}")
    steps = tuple(_step(f"{where}, step {i + 1}", s) for i, s in enumerate(raw_steps))
    ids = [step.id for step in steps]
    if len(set(ids)) != len(ids):
        raise _refuse(where, "two steps share an id; give repeated tools an `id`")
    try:
        reply = ReplyMode(str(raw.get("reply", ReplyMode.COMPOSE.value)))
    except ValueError:
        raise _refuse(where, "`reply` is `compose` or `template`") from None
    template = raw.get("template")
    if reply is ReplyMode.TEMPLATE:
        if not isinstance(template, str) or not template.strip():
            raise _refuse(where, "a `template` reply needs a non-empty `template`")
        try:
            unknown = _template_fields(template) - set(ids)
        except ValueError as error:
            raise _refuse(where, str(error)) from None
        if unknown:
            raise _refuse(where, f"template names unknown steps {sorted(unknown)}")
    elif template is not None:
        raise _refuse(where, "`template` is only for `reply: template`")
    return IntentPlan(
        intent=intent, profile_id=profile, steps=steps, reply=reply, template=template
    )


def parse_intent_plans(document: object, *, source: str) -> dict[str, IntentPlan]:
    """The plans of one document `{intents: {<intent>: {profile, steps, reply, template}}}`."""
    if not isinstance(document, Mapping) or set(document) != {"intents"}:
        raise IntentPlanError(f"{source}: a plans file is a mapping with one key, `intents`")
    raw = document["intents"]
    if not isinstance(raw, Mapping) or not raw:
        raise IntentPlanError(f"{source}: `intents` must map at least one intent to its plan")
    plans: dict[str, IntentPlan] = {}
    for intent, body in raw.items():
        if not isinstance(intent, str):
            raise IntentPlanError(f"{source}: intent names are strings, got {intent!r}")
        plans[intent] = _plan(intent, body, source)
    return plans


# --- running a plan (pure parts) ------------------------------------------------------------------


def resolve_arguments(
    step: PlanStep, arguments: Mapping[str, Any], context: Mapping[str, Any]
) -> PlannedCall:
    """The call `step` makes, its arguments resolved. An `argument` the router did not extract
    uses its default, or raises `PlanArgumentError` naming it. The tool validates the types."""
    resolved: dict[str, Any] = {}
    for name, source in step.arguments:
        match source.kind:
            case SourceKind.CONSTANT:
                resolved[name] = source.value
            case SourceKind.CONTEXT:
                if source.name not in context:
                    raise PlanArgumentError(f"context value {source.name!r} is not available")
                resolved[name] = context[source.name]
            case SourceKind.ARGUMENT:
                if source.name in arguments:
                    resolved[name] = arguments[source.name]
                elif source.has_default:
                    resolved[name] = source.value
                else:
                    raise PlanArgumentError(f"the router extracted no {source.name!r}")
    return PlannedCall(step_id=step.id, tool=step.tool, arguments=resolved)


def render_template(template: str, results: Mapping[str, str]) -> str:
    """`template` with each `{step_id}` replaced by that step's result text. Deterministic."""
    return template.format_map(dict(results))
