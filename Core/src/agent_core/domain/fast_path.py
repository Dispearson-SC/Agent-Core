"""The fast path: System One decides, code executes, the model only writes (Phase 5, S10). Pure.

A router that supports it (`docs/ADDONS.md`, "Routers") answers, in the same call as its route
choice, several questions about the message (an `Assessment`): which of the configured intents it
asks for (one yes/no score per intent), whether it asks to DO something, whether it depends on the
earlier conversation (asked only when the session has one) and how complex it is. This module holds
the switch, the thresholds and the rules that turn those answers into a verdict:

  1. an action request                       -> `pass:action_request`   (the entry profile's model)
  2. depends on the conversation, or unsure  -> `pass:depends_on_history`
  3. complex                                 -> `pass:complex`
  4. more intents than `max_intents`         -> `pass:too_many_intents`
  5. one or more intents above the threshold -> `composite`  (all their plans, one compose)
  6. otherwise                               -> `defer:no_intent` (the existing route/pass logic)

`application/route_turn.py` then checks that every intent of a composite has a declared plan whose
owner the entry profile may ask (the S8 delegation guard, per owner); one that cannot is
`defer:<reason>` and the existing logic runs. A verdict never widens what a turn may do: a pass
runs the entry profile with its full policy, and every planned call runs through its owner's
pipeline.

THE SWITCH (`FastPathMode`)
    off     today's behaviour exactly: the router is asked what it was always asked.
    shadow  the router answers the extra questions and the verdict is AUDITED, but the turn runs
            today's path: safe live evaluation of what the fast path would have done.
    on      the verdict is applied.

A noul answer has no confidence of its own: its value IS the probability of "yes". So "depends on
the conversation with low confidence" is a value above a low threshold (the router is not sure
the message stands alone), and an intent is detected at a value at or above its threshold.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from agent_core.domain.intents import PlanToolResult

__all__ = [
    "COMPOSITE",
    "DEFER_NO_INTENT",
    "MAX_ASSESSED_INTENTS",
    "PASS_ACTION",
    "PASS_COMPLEX",
    "PASS_FOLLOW_UP",
    "PASS_TOO_MANY",
    "Assessment",
    "MAX_COMPOSITE_CALLS",
    "CompositeCall",
    "CompositeMember",
    "CompositeRun",
    "FastPathMode",
    "FastPathThresholds",
    "FastPathVerdict",
    "decide_fast_path",
]

COMPOSITE: Final[str] = "composite"
PASS_ACTION: Final[str] = "pass:action_request"
PASS_FOLLOW_UP: Final[str] = "pass:depends_on_history"
PASS_COMPLEX: Final[str] = "pass:complex"
PASS_TOO_MANY: Final[str] = "pass:too_many_intents"
DEFER_NO_INTENT: Final[str] = "defer:no_intent"

MAX_COMPOSITE_CALLS: Final[int] = 12
"""The most distinct tool calls one composite runs (each owner's `max_iterations` applies too)."""

MAX_ASSESSED_INTENTS: Final[int] = 254
"""As many intents as one Typesafe choice question may list (255 options, an escape among them)."""


class FastPathMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


def _unit(name: str, value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"the fast path {name} must be a number between 0 and 1, got {value!r}")


@dataclass(frozen=True, slots=True)
class FastPathThresholds:
    """Where each answer tips the verdict. `max_intents` bounds one composite (its budget)."""

    intent: float = 0.5
    action: float = 0.5
    follow_up: float = 0.3
    complexity: float = 0.6
    max_intents: int = 3

    def __post_init__(self) -> None:
        for name in ("intent", "action", "follow_up", "complexity"):
            _unit(f"{name} threshold", getattr(self, name))
        if self.max_intents < 1:
            raise ValueError(
                f"the fast path max_intents must be at least 1, got {self.max_intents}"
            )


@dataclass(frozen=True, slots=True)
class Assessment:
    """What a router answered about the message beyond its route choice.

    `intents` maps each assessed intent to the probability the message asks for it (sorted by
    name). `follow_up` is None when it was not asked (no conversation yet). `complexity` is the
    router's score normalised to [0, 1] (0 the simplest level, 1 the most complex)."""

    intents: tuple[tuple[str, float], ...] = ()
    action: float | None = None
    follow_up: float | None = None
    complexity: float | None = None

    def __post_init__(self) -> None:
        if len(self.intents) > MAX_ASSESSED_INTENTS:
            raise ValueError(f"an assessment holds at most {MAX_ASSESSED_INTENTS} intents")
        names = [name for name, _ in self.intents]
        if len(set(names)) != len(names):
            raise ValueError("an assessment names an intent twice")
        for name, value in self.intents:
            if not name or len(name) > 64:
                raise ValueError("an assessed intent name must be 1-64 characters")
            _unit(f"score of intent {name!r}", value)
        for name in ("action", "follow_up", "complexity"):
            value = getattr(self, name)
            if value is not None:
                _unit(name, value)
        object.__setattr__(self, "intents", tuple(sorted(self.intents)))

    @classmethod
    def of(
        cls,
        intents: Mapping[str, float],
        *,
        action: float | None = None,
        follow_up: float | None = None,
        complexity: float | None = None,
    ) -> Assessment:
        return cls(
            tuple((str(k), float(v)) for k, v in intents.items()),
            action=action,
            follow_up=follow_up,
            complexity=complexity,
        )

    def summary(self) -> str:
        """Every answer with its value, compact, for the audit row: `intents=a:0.93,b:0.10;
        action=0.02;follow_up=-;complexity=0.21`."""

        def value(number: float | None) -> str:
            return "-" if number is None else f"{number:.2f}"

        intents = ",".join(f"{name}:{score:.2f}" for name, score in self.intents)
        return (
            f"intents={intents};action={value(self.action)};"
            f"follow_up={value(self.follow_up)};complexity={value(self.complexity)}"
        )


@dataclass(frozen=True, slots=True)
class FastPathVerdict:
    """What the rules say: `code` is one of the constants above (or a `defer:<reason>` the use case
    adds); `intents` the detected intents (sorted), whatever the verdict."""

    code: str
    intents: tuple[str, ...] = ()

    @property
    def composite(self) -> bool:
        return self.code == COMPOSITE

    @property
    def passes(self) -> bool:
        return self.code.startswith("pass:")


def decide_fast_path(
    assessment: Assessment, thresholds: FastPathThresholds, *, has_history: bool
) -> FastPathVerdict:
    """The verdict of the rules in the module docstring, in their order. A missing action or
    complexity answer is not a "no": the router said nothing, so nothing is assumed safe and the
    existing logic runs (`defer:unassessed`)."""
    detected = tuple(
        sorted(name for name, score in assessment.intents if score >= thresholds.intent)
    )
    if assessment.action is None or assessment.complexity is None:
        return FastPathVerdict("defer:unassessed", detected)
    if assessment.action >= thresholds.action:
        return FastPathVerdict(PASS_ACTION, detected)
    if has_history and (
        assessment.follow_up is None or assessment.follow_up >= thresholds.follow_up
    ):
        return FastPathVerdict(PASS_FOLLOW_UP, detected)
    if assessment.complexity >= thresholds.complexity:
        return FastPathVerdict(PASS_COMPLEX, detected)
    if not detected:
        return FastPathVerdict(DEFER_NO_INTENT, detected)
    if len(detected) > thresholds.max_intents:
        return FastPathVerdict(PASS_TOO_MANY, detected)
    return FastPathVerdict(COMPOSITE, detected)


@dataclass(frozen=True, slots=True)
class CompositeMember:
    """One intent of a composite: the profile owning its plan and the hop its calls run at.
    `hop` None: the owner is the entry profile itself and its calls run as the person; otherwise
    they run delegated (S8), as an ask_peer to the owner would."""

    intent: str
    profile_id: str
    hop: int | None = None


@dataclass(frozen=True, slots=True)
class CompositeCall:
    """The intents a composite turn answers, sorted by intent: carried from the route step to the
    composite steps (`RoutedTurn.composite`), so a recovered turn runs the same plans."""

    members: tuple[CompositeMember, ...]

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("a composite answers at least one intent")
        object.__setattr__(self, "members", tuple(sorted(self.members, key=lambda m: m.intent)))

    @property
    def intents(self) -> tuple[str, ...]:
        return tuple(member.intent for member in self.members)


@dataclass(frozen=True, slots=True)
class CompositeRun:
    """How a composite's calls ended (a recorded step result).

    `groups` holds each intent's results in its plan's order, the intents in order; `cached` a
    remembered reply (S10 answer cache: no call ran); `fallback` why the composite cannot answer;
    `tool_count` how many distinct calls ran."""

    groups: tuple[tuple[str, tuple[PlanToolResult, ...]], ...] = ()
    cached: str | None = None
    fallback: str | None = None
    tool_count: int = 0

