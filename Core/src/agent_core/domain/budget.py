"""Budgets - the cheap, always-on guards against a runaway turn.

Phase:   F1 (iterations) / F5 (cost, once compaction makes spend meaningful)
Tasks:   docs/TASKS.md#t-f1-03
Status:  TYPES DEFINED / BEHAVIOUR PENDING

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
        )

    def wrapup_notice_due(self, threshold: float = 0.8) -> bool:
        """PSEUDO-CODE - implement in F1. Borrowed from Hermes; worth copying.

        True once the budget crosses `threshold`. The caller then injects a one-time
        system notice telling the model to stop exploring and deliver with what it has.

        Without it, an exhausted budget truncates mid-investigation and the user gets
        nothing. With it, the agent lands the plane.

        Fire it ONCE per turn. Repeating it every iteration burns the very budget it is
        trying to protect - and that mistake produces no error, only a bigger bill.
        """
        raise NotImplementedError("F1 - docs/TASKS.md#t-f1-03")
