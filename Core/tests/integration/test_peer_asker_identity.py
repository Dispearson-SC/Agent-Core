"""Integration tests: the ASKING agent's identity travels with the ask.

Phase:   F12 - Agent-to-agent orchestration
Tasks:   docs/TASKS.md#t-f11-48
Covers:  adapters/driven/peers/mailbox.py
         adapters/driven/peers/mailbox_a2a.py
         adapters/driven/persistence_pg/peer_asker_migration.py

WHAT IS BEING DEFENDED

    THE SECOND LOCK ON A TWO-SIDED ALLOWLIST. `ports/agent_mailbox.py` says `may_ask` is
    "checked on BOTH sides: the asker checks it may ask, the answerer checks it accepts",
    and `hop_limit.authorise_hop` implements both halves - but the answering side can only
    re-run `callee_policy.may_ask(caller)` if it is told WHO asked. `PeerAsk` carried
    `from_session`, `turn_id` and `hop` and no asking `AgentId`, so an arriving question
    was taken on trust: the asking side said it was allowed.

    The asking side's gate is real and still applies, so this was never an open door. It
    is defence in depth, and CLAUDE.md non-negotiable #10 is the reason to want it: a peer
    that was itself misled is a confused deputy, and what it sends arrives with friendly
    provenance. A two-sided check enforced on one side is a one-sided check with extra
    words.

    NULL IS AN ABSENCE, NEVER A VALUE. `peer_messages` predates the column, so rows
    enqueued before migration `0025` have no asker. They claim `asker=None`, they are
    still deliverable (an old ask is a turn suspended on `DBOS.recv()`, and dropping it
    would strand that conversation), and `None` means the callee-side check is
    UNANSWERABLE for that row - never that anyone may ask. Nothing backfills them: a
    manufactured identity in an audit-adjacent table is a claim nobody made.

    BOTH ADAPTERS, OR NEITHER (docs/WAVES.md rule 4). `PgAgentMailbox` persists the asker
    in a column; `A2AAgentMailbox` sends it as A2A message metadata beside `hop`, for the
    same reason `hop` travels there - so a compliant peer, or a future receiving-side
    adapter, can enforce its own side of the allowlist.

The Postgres tests need a real instance and skip cleanly without one, the same pattern as
test_peer_mailbox.py. The A2A test needs no infrastructure: a mock transport captures the
request this adapter puts on the wire.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import uuid
from typing import Any

import httpx
import psycopg
import pytest

from agent_core.adapters.driven.peers.hop_limit import HopRefusal, authorise_hop
from agent_core.adapters.driven.peers.mailbox import PgAgentMailbox
from agent_core.adapters.driven.peers.mailbox_a2a import A2AAgentMailbox
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TRIAGE = AgentId("support_triage")
_BILLING = AgentId("billing_specialist")
_STRANGER = AgentId("marketing_outreach")

_TRIAGE_REF = AgentRef(agent_id=_TRIAGE, display_name="Support triage")
_BILLING_REF = AgentRef(agent_id=_BILLING, display_name="Billing specialist")

# A's policy: it may ask B.
_ASKING_POLICY = PeerPolicy(enabled=True, peers=(_BILLING_REF,))
# B's policy: it accepts being asked by A. This is the half that needs the asker's id.
_ANSWERING_POLICY = PeerPolicy(enabled=True, peers=(_TRIAGE_REF,))
# B's policy on a day nobody added A to it. The refusal this whole anchor exists to make
# reachable from the answering side.
_ANSWERING_POLICY_WITHOUT_A = PeerPolicy(
    enabled=True, peers=(AgentRef(agent_id=_STRANGER, display_name="Marketing outreach"),)
)

_A2A_ENDPOINT = "http://peer.invalid/a2a"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


def _migrated_conninfo() -> str:
    """A fully migrated app database for this module. Idempotent, so every test calls it.

    `apply_all_migrations` rather than a hand-picked pair: it applies every DISCOVERED
    migration in id order, which is the only way to be sure `0025` lands after the `0014`
    table it alters - and it is what production runs.
    """
    app_db = "agent_core_peer_asker_test"
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO,
            app_database=app_db,
            dbos_database=migrations.dbos_system_database(app_db),
        )
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.apply_all_migrations(app_conninfo))
    return app_conninfo


def _session() -> SessionRef:
    return SessionRef(session_id=SessionId(f"s-{uuid.uuid4()}"), tenant_id=TenantId("t-1"))


@pytest.mark.phase("F12")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_claimed_ask_names_the_agent_that_asked_so_the_callee_can_re_run_its_allowlist() -> None:
    """The answering side gets the caller's id, and the callee-side gate becomes real.

    Both assertions matter and they are different claims: that the identity survives the
    queue, and that it is the identity `authorise_hop` needs - the same `caller` argument
    the asking side passed to the same function before the question ever left.
    """
    signature = inspect.signature(PgAgentMailbox.ask)
    assert "asker" in signature.parameters, (
        "PgAgentMailbox.ask cannot be told who is asking, so nothing can ever be written "
        "to the row for the answering side to check. The callee half of the two-sided "
        "allowlist is unenforceable by construction. docs/TASKS.md#t-f11-48"
    )

    conninfo = _migrated_conninfo()
    turn_id = TurnId(str(uuid.uuid4()))
    question = f"what was this customer charged in March? [{uuid.uuid4()}]"

    asking = PgAgentMailbox(conninfo)
    correlation_id = asyncio.run(
        asking.ask(
            _ASKING_POLICY,
            _BILLING,
            question,
            from_session=_session(),
            turn_id=turn_id,
            hop=1,
            asker=_TRIAGE,
        )
    )

    answering = PgAgentMailbox(conninfo)
    claimed = asyncio.run(answering.claim_next(_BILLING))

    assert claimed is not None and claimed.correlation_id == correlation_id
    assert claimed.asker == _TRIAGE, (
        f"the claimed ask names {claimed.asker!r} as its asker, not {_TRIAGE!r}. The "
        "answering agent cannot re-run `callee_policy.may_ask(caller)` on arrival and "
        "takes the question on trust because the asking side said it was allowed"
    )

    accepted = authorise_hop(
        caller=claimed.asker,
        caller_policy=_ASKING_POLICY,
        callee=claimed.target,
        callee_policy=_ANSWERING_POLICY,
        hop=claimed.hop,
    )
    assert accepted.allowed, (
        f"the answering side refused an ask it does allow: {accepted.reason!r}. The "
        "claimed row carries the wrong caller identity"
    )

    refused = authorise_hop(
        caller=claimed.asker,
        caller_policy=_ASKING_POLICY,
        callee=claimed.target,
        callee_policy=_ANSWERING_POLICY_WITHOUT_A,
        hop=claimed.hop,
    )
    assert refused.reason is HopRefusal.CALLEE_DOES_NOT_ALLOW_CALLER, (
        f"a callee whose allowlist does not name the asker answered anyway "
        f"({refused.reason!r}). The second lock does not turn, so the two-sided allowlist "
        "is one-sided with extra words"
    )


@pytest.mark.phase("F12")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_row_enqueued_before_the_column_existed_claims_no_asker_and_none_is_invented() -> None:
    """NULL means UNKNOWN. It is still delivered, and nothing fabricates an identity for it.

    The row is written straight through SQL, naming exactly the columns migration `0014`
    created - which is what a row enqueued before `0025` looks like. Two properties are
    asserted because the wrong reading of NULL fails in both directions: dropping the row
    strands a turn that is suspended on `DBOS.recv()`, and filling the column in makes the
    answering side check an identity nobody claimed.
    """
    conninfo = _migrated_conninfo()
    correlation_id = f"legacy-{uuid.uuid4()}"
    session = _session()
    turn_id = TurnId(str(uuid.uuid4()))
    question = f"legacy ask from before the asker travelled [{uuid.uuid4()}]"

    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO peer_messages
                (correlation_id, target_agent_id, from_session_id, from_tenant_id,
                 turn_id, hop, question)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                correlation_id,
                _BILLING,
                session.session_id,
                session.tenant_id,
                turn_id,
                1,
                question,
            ),
        )

    claimed = asyncio.run(PgAgentMailbox(conninfo).claim_next(_BILLING))

    assert claimed is not None and claimed.correlation_id == correlation_id, (
        "an ask enqueued before the asker column existed was not delivered at all. It is "
        "a turn suspended on DBOS.recv(), and refusing to claim it strands that "
        "conversation forever - the migration must not make old rows undeliverable"
    )
    assert claimed.asker is None, (
        f"a row that never recorded an asker claims {claimed.asker!r}. A backfilled or "
        "inferred identity in an audit-adjacent table is a claim nobody made, and the "
        "answering side would check it as though the asking side had asserted it"
    )


@pytest.mark.phase("F12")
def test_the_a2a_ask_sends_the_asking_agent_id_as_message_metadata() -> None:
    """docs/WAVES.md rule 4: the other implementation of the port follows in this change.

    Over A2A there is no row to read the caller off, so the identity travels where `hop`
    already does - the message metadata a compliant peer, or a future receiving-side
    adapter, enforces its own side of the allowlist from.
    """
    signature = inspect.signature(A2AAgentMailbox.ask)
    assert "asker" in signature.parameters, (
        "A2AAgentMailbox.ask cannot be told who is asking, so swapping the transport "
        "silently drops the caller identity the Postgres adapter carries. One adapter "
        "enforcing a check the other cannot is the port cut leaking. docs/WAVES.md rule 4"
    )

    sent: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"id": "task-1", "status": {"state": "submitted"}},
            },
        )

    async def _ask() -> str:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await A2AAgentMailbox(client).ask(
                PeerPolicy(
                    enabled=True,
                    peers=(
                        AgentRef(
                            agent_id=_BILLING,
                            display_name="Billing specialist",
                            endpoint=_A2A_ENDPOINT,
                        ),
                    ),
                ),
                _BILLING,
                "what was this customer charged in March?",
                from_session=_session(),
                turn_id=TurnId(str(uuid.uuid4())),
                hop=1,
                asker=_TRIAGE,
            )

    correlation_id = asyncio.run(_ask())

    assert correlation_id == "task-1"
    metadata = sent[0]["params"]["message"]["metadata"]
    assert metadata.get("from_agent_id") == _TRIAGE, (
        f"the A2A message carried {metadata.get('from_agent_id')!r} as its asker beside "
        f"hop={metadata.get('hop')!r}. The receiving side has the hop count and no "
        "caller identity, so it can enforce the limit and not the allowlist"
    )
