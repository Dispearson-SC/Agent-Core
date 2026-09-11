"""Scheduled turns: service identity, fresh session per run.

Phase:   Later - not required before D2
Tasks:   docs/TASKS.md#t-later-01
Status:  RED FIRST - written before cron.py builds the request

WHAT THIS FILE PINS
    A cron-triggered turn is a DRIVING adapter exactly like the HTTP route: it builds a
    `TurnRequest` and hands it to whatever starts a turn (`enqueue_turn` in production).
    Two properties must hold, and neither fails any OTHER test if broken:

    1. FRESH SESSION PER RUN. A scheduled firing never reuses a session id - not its own
       previous run's, not a human's. Reusing one grows a conversation nobody reads and
       lets "last night's cron job" answer with a human's context still in the prompt.
    2. SERVICE IDENTITY, NOT A HUMAN'S. `CallerIdentity`, never `AdminIdentity`
       (CLAUDE.md non-negotiable #9) - a cron job is a caller with no human behind it.

Fakes only: no database, no DBOS, no model. `start_turn` here is a plain async callable
that records what it was given, standing in for `enqueue_turn` (t-f2-01).
"""

from __future__ import annotations

import asyncio

from agent_core.adapters.driving.scheduler.cron import (
    SCHEDULER_CHANNEL,
    SCHEDULER_SUBJECT_ID,
    build_service_identity,
    run_scheduled_turn,
)
from agent_core.domain.turn import CallerIdentity, TenantId, TurnRequest

_TENANT = TenantId("tenant-under-test")


def _human_caller() -> CallerIdentity:
    """A caller a real human turn would carry, for contrast."""
    return CallerIdentity(
        subject_id="human-user-42",
        channel="telegram",
        tenant_id=_TENANT,
        roles=frozenset({"customer"}),
    )


class _RecordingStarter:
    """Stands in for `enqueue_turn`: records every request, starts nothing real."""

    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    async def __call__(self, request: TurnRequest) -> TurnRequest:
        self.requests.append(request)
        return request


def test_service_identity_is_a_caller_never_an_admin_and_never_a_human() -> None:
    """CLAUDE.md #9: a cron job is a caller with no human behind it, not an admin."""
    identity = build_service_identity(_TENANT)

    assert isinstance(identity, CallerIdentity)
    assert type(identity).__name__ != "AdminIdentity"

    human = _human_caller()
    assert identity.subject_id != human.subject_id, (
        "The scheduled identity must not be any human caller's subject_id."
    )
    assert identity.channel == SCHEDULER_CHANNEL
    assert identity.subject_id == SCHEDULER_SUBJECT_ID
    assert identity.channel != human.channel


def test_each_scheduled_run_starts_a_fresh_session_never_inherited() -> None:
    """Two firings of the same job must never land in the same session - and neither may
    land in a session a human conversation already used."""
    starter = _RecordingStarter()
    human_session_id = "human-conversation-in-progress"

    asyncio.run(
        run_scheduled_turn(starter, tenant_id=_TENANT, profile_id="delivery_optimizer")
    )
    asyncio.run(
        run_scheduled_turn(starter, tenant_id=_TENANT, profile_id="delivery_optimizer")
    )

    assert len(starter.requests) == 2
    first_session, second_session = (r.session for r in starter.requests)

    assert first_session.session_id != second_session.session_id, (
        "Two scheduled runs shared a session id - a run inherited another run's history."
    )
    assert str(first_session.session_id) != human_session_id
    assert str(second_session.session_id) != human_session_id

    # Both runs still carry the service identity, not the human one, on every firing.
    for request in starter.requests:
        assert request.caller.subject_id == SCHEDULER_SUBJECT_ID
        assert request.caller.channel == SCHEDULER_CHANNEL
