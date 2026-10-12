"""C15 (D41): per-peer rules survive the file -> `policy_rules` -> `PgToolPolicy` round trip.

The unit tests prove the reduction over in-memory snapshots; this proves the stored shape. A rule
file naming a `resource` is reconciled into a real migrated database (`apply_policy_rules`), loaded
for a caller (`PgToolPolicy.load_rules`) and decided per peer, so a `resource_pattern` the writer
and the reader spell differently would fail here and nowhere else.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.policy_fs.loader import apply_policy_rules, parse_policy_rules
from agent_core.domain.policy import Effect
from agent_core.domain.turn import CallerIdentity, TenantId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DATABASE = "agent_core_peer_policy_rules_test"

_RULES = """
rules:
  - rule_id: peers-any
    tool_pattern: ask_peer
    effect: allow
    reason: Peers may be asked.
  - rule_id: peers-ask-fraud
    tool_pattern: ask_peer
    resource: fraud
    effect: ask
    reason: The fraud desk sees customer data; a person agrees first.
  - rule_id: peers-no-payroll
    tool_pattern: ask_peer
    resource: pay*
    effect: deny
    reason: Payroll answers nobody.
"""


def _reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="module")
def conninfo() -> Iterator[str]:
    if not _reachable():
        pytest.skip("no reachable Postgres instance")
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)')
    asyncio.run(migrations.ensure_databases(_ADMIN_CONNINFO, app_database=_DATABASE))
    app = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_DATABASE}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.apply_all_migrations(app))
    yield app
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)')


def test_a_resource_rule_written_by_the_loader_is_decided_per_peer_by_the_adapter(
    conninfo: str,
) -> None:
    caller = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))
    asyncio.run(apply_policy_rules(conninfo, parse_policy_rules(_RULES, source="rules.yaml")))
    policy = PgToolPolicy(lambda: psycopg.connect(conninfo))

    rules = asyncio.run(policy.load_rules(caller))

    def effect(target: str) -> Effect:
        return policy.decide(rules, "ask_peer", {"target": target, "question": "q"}).effect

    assert effect("billing") is Effect.ALLOW
    assert effect("fraud") is Effect.NEEDS_APPROVAL
    assert effect("payroll") is Effect.DENY
    assert effect("paycheck") is Effect.DENY, "a trailing * is a prefix"
    assert policy.filter_toolset(rules, ("ask_peer",)) == ("ask_peer",)
    decision = policy.decide(rules, "ask_peer", {"target": "fraud"})
    assert decision.rule_id == "peers-ask-fraud"
    assert decision.reason == "The fraud desk sees customer data; a person agrees first."


def test_the_stored_definition_of_an_unscoped_rule_has_no_resource_key(conninfo: str) -> None:
    asyncio.run(apply_policy_rules(conninfo, parse_policy_rules(_RULES, source="rules.yaml")))

    with psycopg.connect(conninfo) as conn:
        rows = dict(
            conn.execute("SELECT rule_id, definition FROM policy_rules").fetchall()  # type: ignore[arg-type]
        )

    assert "resource_pattern" not in rows["peers-any"]
    assert rows["peers-ask-fraud"]["resource_pattern"] == "fraud"
    assert rows["peers-ask-fraud"]["effect"] == "needs_approval"
