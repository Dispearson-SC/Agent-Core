"""Integration tests for D20 profile-version persistence.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-18
Covers:  adapters/driven/persistence_pg/conversation_repository.py

WHAT IS BEING DEFENDED
    D20: an audit six months later must be able to show not only WHAT the agent did but
    WHAT IT HAD BEEN TOLD TO DO. That only holds if every turn row carries the profile
    version AND the resolved snapshot, and if editing the YAML afterwards cannot reach
    back and change a snapshot that is already stored.

    The first test proves that against a real Postgres and is skipped when one is not
    reachable - the same pattern as test_conversation_repository.py. The second needs no
    database: it pins the half of the guarantee that is pure, namely that the snapshot a
    turn row stores is a JSON value copy and never a path back to the file on disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import conversation_repository, migrations
from agent_core.adapters.driven.persistence_pg.conversation_repository import PgConversationStore
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TurnId,
    TurnRequest,
    UserInput,
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# One profile file, rendered twice with a different persona. `max_cost_usd` and the
# approval rule are here because they are what an auditor actually asks about: the
# snapshot has to reproduce the LIMITS in force, not merely the name of the agent.
_PROFILE_YAML = """
id: auditor
persona: {persona}
model: openai:gpt-4o-mini
max_iterations: 7
max_cost_usd: '2.50'
approval_rules:
  - tool_name: quote_price
    reason: a price change needs a human
"""


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


def _write_profile(path: Path, persona: str) -> AgentProfile:
    path.write_text(_PROFILE_YAML.format(persona=persona), encoding="utf-8")
    return load_profile_sync(path)


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id="t-1",  # type: ignore[arg-type]
        roles=frozenset({"operator"}),
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_every_turn_row_carries_its_profile_version_and_a_later_edit_leaves_it_alone(
    tmp_path: Path,
) -> None:
    app_db = "agent_core_profile_snapshot_test"
    dbos_db = "agent_core_profile_snapshot_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(conversation_repository.apply_profile_snapshot_migration(app_conninfo))

    yaml_path = tmp_path / "auditor.yaml"
    registry = ProfileVersionRegistry()

    first = registry.assign(_write_profile(yaml_path, "First persona"))
    assert first.version == 1

    session = SessionRef(session_id=f"s-{uuid.uuid4()}", tenant_id="t-1")  # type: ignore[arg-type]
    turn_one = TurnId(str(uuid.uuid4()))
    asyncio.run(
        PgConversationStore(app_conninfo, {first.id: first}).append_request(
            turn_one,
            TurnRequest(
                session=session,
                caller=_caller(),
                profile_id=first.id,
                input=UserInput(text="before the edit"),
            ),
        )
    )

    # The YAML changes under the running system - which is the whole point of D20.
    second = registry.assign(_write_profile(yaml_path, "Second persona"))
    assert second.version == 2

    turn_two = TurnId(str(uuid.uuid4()))
    asyncio.run(
        PgConversationStore(app_conninfo, {second.id: second}).append_request(
            turn_two,
            TurnRequest(
                session=session,
                caller=_caller(),
                profile_id=second.id,
                input=UserInput(text="after the edit"),
            ),
        )
    )

    with psycopg.connect(app_conninfo) as conn:
        rows = conn.execute(
            "SELECT turn_id, profile_version, profile_snapshot FROM turns WHERE session_id = %s",
            (session.session_id,),
        ).fetchall()

    assert len(rows) == 2
    # EVERY row, not just the one this test happens to look at afterwards: a turn row
    # without a version is the exact hole D20 exists to close.
    for _, version, snapshot in rows:
        assert version is not None
        assert snapshot

    stored = {str(turn_id): (version, snapshot) for turn_id, version, snapshot in rows}

    assert stored[turn_one][0] == 1
    assert stored[turn_one][1]["persona"] == "First persona"
    assert stored[turn_one][1]["content_hash"] == first.content_hash
    assert stored[turn_one][1]["max_iterations"] == 7

    assert stored[turn_two][0] == 2
    assert stored[turn_two][1]["persona"] == "Second persona"
    assert stored[turn_two][1]["content_hash"] == second.content_hash

    # The record is only worth having if the older row still shows what the older turn
    # was told to do.
    assert stored[turn_one][1] != stored[turn_two][1]


def test_a_stored_snapshot_is_a_value_copy_a_later_yaml_edit_cannot_reach(
    tmp_path: Path,
) -> None:
    # Fetched off the module rather than imported at the top so that the first run of
    # this test fails on THIS assertion instead of on a collection-time ImportError -
    # docs/WAVES.md requires a red that comes from the contract, not from the loader.
    render = getattr(conversation_repository, "profile_snapshot", None)
    assert render is not None, (
        "the adapter must expose a JSON-safe renderer for the resolved profile: a turn "
        "row stores the profile as data, never a path back to the file on disk"
    )

    yaml_path = tmp_path / "auditor.yaml"
    registry = ProfileVersionRegistry()

    first = registry.assign(_write_profile(yaml_path, "First persona"))
    # Round-tripping through JSON is the assertion, not a convenience: a snapshot that
    # cannot be serialised cannot be stored, and Decimal budgets and Enum members are
    # exactly where that breaks.
    snapshot = json.loads(json.dumps(render(first)))

    second = registry.assign(_write_profile(yaml_path, "Second persona"))

    assert snapshot["persona"] == "First persona"
    assert snapshot["version"] == 1
    assert snapshot["content_hash"] == first.content_hash
    # The limits in force, not just the identity of the agent.
    assert snapshot["max_iterations"] == 7
    assert snapshot["max_cost_usd"] == "2.50"
    assert snapshot["approval_rules"][0]["tool_name"] == "quote_price"

    later = json.loads(json.dumps(render(second)))
    assert later["version"] == 2
    assert later["persona"] == "Second persona"
    assert snapshot != later
