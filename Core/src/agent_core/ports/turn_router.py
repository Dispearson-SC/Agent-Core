"""Port: TurnRouter - which way should this turn go, before the model is asked? (D95)

Phase:   Phase 5 (fast communication), P1
Adapter: adapters/driven/routing/addon_router.py (the addons' `provides.routers`)
Per-vertical: NO - a vertical ships a router as an addon, never as a change here.

One question. Given the router a profile named and what a person said, answer `pass`,
`route(profile_id)` or `intent(name, arguments)` (`domain/routing.py`). The caller (`RouteTurn`)
owns everything around the answer: the timeout, the fallback, which targets are allowed and the
audit row. So an adapter may raise anything; a raise is a fallback to the normal path, never a
failed turn.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.routing import RouteDecision, RouteRequest

__all__ = ["TurnRouter"]


class TurnRouter(Protocol):
    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        """Ask the router named `router` about `request`. An unknown name raises `KeyError`."""
        ...
