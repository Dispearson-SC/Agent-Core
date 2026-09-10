"""Budgets - the cheap, always-on guards against a runaway turn.

Phase:   F1 (iterations) / F5 (cost, once compaction makes spend meaningful)
Tasks:   docs/TASKS.md#t-f1-03
Status:  TYPES DEFINED / WRAP-UP NOTICE IMPLEMENTED (t-f1-03); COST GUARD LANDS IN F5

DESIGN NOTE - START WITH A BUDGET, NOT WITH FIVE MECHANISMS
    Hermes runs five independent anti-loop mechanisms totalling ~1,300 lines: iteration
    budget, stall guard on identical calls, repetition guard on degenerate output,
    wall-clock deadline, empty-response guard. All of it was earned in production.

    We start with TWO - iterations and cost - because a guardrail that fires on
    legitimate work gets switched off within a week and then protects nothing. OpenClaw
    ships loop detection OFF by default for exactly that reason. Add a third mechanism
    only when a real incident asks for it. docs/DECISIONS.md.

REFUND SEMANTICS
    Hermes refunds iterations spent on programmatic tool calling so batch work does not
    eat the conversational budget. We do not need that on day 1, but `consume` is shaped
    so adding a refund later is not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BudgetState:
    """Immutable counter. `consume` returns a NEW state; it never mutates.

    Immutability matters specifically because budget state crosses a DBOS step boundary.
    A mutable counter replayed after a crash would double-count."""

    max_iterations: int
    used_iterations: int = 0
    max_cost_usd: Decimal = Decimal("0")
    spent_usd: Decimal = Decimal("0")

    # What the counters read BEFORE the `consume` that produced this state. They are what
    # makes the wrap-up notice fire once: the crossing is a property of the step, not of
    # the position. Carrying them instead of a "notice already sent" flag keeps the state
    # a pure function of the counters, so a DBOS replay of the same step reaches the same
    # answer rather than depending on whether the caller remembered to mark it.
    previous_used_iterations: int = 0
    previous_spent_usd: Decimal = Decimal("0")

    @property
    def iterations_exhausted(self) -> bool:
        return self.used_iterations >= self.max_iterations

    @property
    def cost_exhausted(self) -> bool:
        return self.max_cost_usd > 0 and self.spent_usd >= self.max_cost_usd

    @property
    def exhausted(self) -> bool:
        return self.iterations_exhausted or self.cost_exhausted

    def consume(self, *, iterations: int = 1, cost_usd: Decimal = Decimal("0")) -> BudgetState:
        """Advance the counters and return a new state.

        Deliberately does NOT raise on exhaustion. Exhaustion is a legitimate turn
        outcome the caller reports to the user, not an exception that unwinds a DBOS
        step and loses work already done.
        """
        return replace(
            self,
            used_iterations=self.used_iterations + iterations,
            spent_usd=self.spent_usd + cost_usd,
            previous_used_iterations=self.used_iterations,
            previous_spent_usd=self.spent_usd,
        )

    def wrapup_notice_due(self, threshold: float = 0.8) -> bool:
        """True only on the state whose own `consume` carried the budget over `threshold`.

        The caller then injects a one-time system notice telling the model to stop
        exploring and deliver with what it has. Without it, an exhausted budget truncates
        mid-investigation and the user gets nothing. With it, the agent lands the plane.

        It fires ONCE per turn because it reports a CROSSING, not a position: the answer
        is true only while the previous counters were below the threshold and the current
        ones are at or past it. Asking "am I past 80%?" instead would re-inject the notice
        on every remaining iteration and burn the very budget it protects - and that
        mistake produces no error, only a bigger bill.

        Reading it is free and repeatable: the same state always gives the same answer.
        Whichever ceiling is crossed first - iterations or cost - triggers it.
        """
        limit = Decimal(str(threshold))
        return self._crossed(
            self.previous_used_iterations, self.used_iterations, self.max_iterations, limit
        ) or self._crossed(self.previous_spent_usd, self.spent_usd, self.max_cost_usd, limit)

    @staticmethod
    def _crossed(before: Decimal | int, after: Decimal | int, ceiling: Decimal | int,
                 threshold: Decimal) -> bool:
        """Did this step take `before` -> `after` over `threshold` of `ceiling`?

        A ceiling of zero or less means that dimension is not budgeted, so it can never
        be crossed. Fractions stay in Decimal: a float ratio would make the crossing
        depend on rounding, and a guard that fires one iteration late is a guard that
        fires after the budget is already gone.
        """
        if ceiling <= 0:
            return False
        return (Decimal(after) / Decimal(ceiling)) >= threshold > (
            Decimal(before) / Decimal(ceiling)
        )
