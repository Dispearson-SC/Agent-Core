"""Driving adapter: scheduled turns.

Phase:   later - not required before D2
Tasks:   docs/TASKS.md#t-later-01
Status:  IMPLEMENTED (t-later-01) - fresh session per run, service identity, thin seam for
         DBOS's scheduled-workflow decorator. Core/tests/unit/test_scheduler.py.

DBOS provides scheduled workflows with cron syntax, so this adapter is thin: resolve the
profile, mint a synthetic session, start the same workflow the HTTP adapter starts.

TWO THINGS TO GET RIGHT
    - A cron run needs a CallerIdentity too. Give it a dedicated service identity with its
      own policy rows. Reusing a human's identity means a scheduled job silently inherits
      that person's permissions.
    - Each run gets a FRESH session. Sharing one session across runs grows a conversation
      that nobody reads and that compaction then pays to summarise forever.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final
from uuid import uuid4

from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnRequest,
    UserInput,
)

__all__ = [
    "SCHEDULER_CHANNEL",
    "SCHEDULER_SUBJECT_ID",
    "TurnStarter",
    "build_service_identity",
    "run_scheduled_turn",
]

# WHO a cron-triggered turn is FROM. Not derived from any human caller - CLAUDE.md
# non-negotiable #9 forbids widening a chat client into a wider identity, and the same
# rule cuts the other way here: a cron job never borrows a human's subject_id either.
# These are constants, never parameters a caller could override into somebody else's
# identity.
SCHEDULER_CHANNEL: Final[str] = "scheduler"
SCHEDULER_SUBJECT_ID: Final[str] = "scheduled-job"

# `enqueue_turn` (t-f2-01, adapters/driving/workflow/turn_workflow.py) is what fills this
# seat in production. Injected rather than imported directly, for the same reason
# routes.py injects `TurnStarter`: this file must not gain the ability to AWAIT a turn,
# and it must stay importable - and testable - without DBOS or Postgres running.
TurnStarter = Callable[[TurnRequest], Awaitable[object]]


def build_service_identity(tenant_id: TenantId) -> CallerIdentity:
    """The identity every scheduled run carries.

    A `CallerIdentity`, never an `AdminIdentity` (CLAUDE.md #9): a cron job is a caller
    with no human behind it, not an administrator. It gets its own policy rows - the
    `ToolPolicy` rules for `SCHEDULER_SUBJECT_ID` are configured like any other caller's,
    never inherited from whoever happens to run the process.
    """
    return CallerIdentity(
        subject_id=SCHEDULER_SUBJECT_ID,
        channel=SCHEDULER_CHANNEL,
        tenant_id=tenant_id,
        roles=frozenset({"scheduled"}),
    )


async def run_scheduled_turn(
    start_turn: TurnStarter,
    *,
    tenant_id: TenantId,
    profile_id: str,
    text: str = "",
) -> object:
    """One cron firing, one turn.

    Mints a BRAND NEW session id every call - never one supplied by a caller, never one
    reused between firings. That is the whole of "fresh session per run": a scheduled
    turn cannot inherit a human conversation's history because nothing here ever reads an
    existing `SessionId`, and two firings of the same job never land in the same session
    because each one draws its own `uuid4()`.

    `start_turn` is `enqueue_turn` in production - this adapter only builds the request
    and calls the same seat the HTTP adapter starts (docs/ARCHITECTURE.md#3: adding a
    caller never changes `application/` or `ports/`).
    """
    session = SessionRef(session_id=SessionId(str(uuid4())), tenant_id=tenant_id)
    request = TurnRequest(
        session=session,
        caller=build_service_identity(tenant_id),
        profile_id=profile_id,
        input=UserInput(text=text),
    )
    return await start_turn(request)
