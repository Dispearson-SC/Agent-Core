"""The task graph on a REAL Postgres mailbox (C23, D54).

Phase:   Agent-comms v2, wave 3
Covers:  adapters/driven/peers/task_graph.py, persistence_pg/task_graph_migration.py,
         domain/task_graph.py over real rows

WHAT IS BEING DEFENDED: a root turn that asked two peers, one of which asked a third in turn (and
one background ask), is read back as a three-level tree through the callee turn ids that
`peer_workflow_id` derives; another tenant reads nothing; states follow the rows.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.peers.mailbox import PgAgentMailbox
from agent_core.adapters.driven.peers.task_graph import PgTaskGraphReader
from agent_core.domain.peers import AgentId, BackgroundState, peer_workflow_id
from agent_core.domain.task_graph import build_task_graph
from agent_core.domain.turn import CallerIdentity, SessionId, SessionRef, TenantId, TurnId
from tests.fakes import peer_world as w

_DATABASE = "agent_core_task_graph_test"
ROOT = "11111111-1111-4111-8111-111111111111"
SESSION = SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t"))
CALLER = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


async def _ask(
    mailbox: PgAgentMailbox,
    target: str,
    asker: str,
    parent_turn: str,
    key: str,
    *,
    session: SessionRef = SESSION,
    background: bool = False,
) -> str:
    policy = w.profiles()[w.A].peers
    return await mailbox.ask(
        policy,
        AgentId(target),
        f"q-{key}",
        from_session=session,
        turn_id=TurnId(parent_turn),
        hop=0,
        asker=AgentId(asker),
        request_key=key,
        root_turn_id=ROOT,
        background_caller=CALLER if background else None,
    )


def test_two_peers_and_a_nested_ask_are_read_back_as_a_three_level_tree() -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        await w.fresh_databases(_DATABASE)
        mailbox = PgAgentMailbox(w.conninfo(_DATABASE))
        first = await _ask(mailbox, w.B, w.A, ROOT, "k1")
        await _ask(mailbox, w.C, w.A, ROOT, "k2", background=True)
        # B asks C in turn: the asking turn is B's callee turn.
        nested = await _ask(mailbox, w.C, w.B, peer_workflow_id(first), "k3")
        reader = PgTaskGraphReader(w.conninfo(_DATABASE))
        mine = await reader.asks_under("t", ROOT)
        theirs = await reader.asks_under("other-tenant", ROOT)
        return mine, theirs, (first, nested)

    mine, theirs, (first, nested) = asyncio.run(scenario())

    graph = build_task_graph(ROOT, mine.records, omitted=mine.omitted)
    assert theirs.records == () and theirs.omitted == 0
    assert graph.unlinked == () and not graph.truncated
    assert graph.root.depth == 3 and graph.root.agent == w.A
    by_handle = {c.handle: c for c in graph.root.children}
    direct = by_handle[first]
    assert (direct.agent, direct.state, direct.background) == (w.B, BackgroundState.QUEUED, False)
    (child,) = direct.children
    assert (child.handle, child.agent) == (nested, w.C)
    (background,) = [c for c in graph.root.children if c.background]
    assert background.agent == w.C and background.children == ()


def test_the_migration_adds_the_root_index_and_states_follow_the_rows() -> None:
    async def scenario() -> Any:
        await w.fresh_databases(_DATABASE)
        mailbox = PgAgentMailbox(w.conninfo(_DATABASE))
        corr = await _ask(mailbox, w.B, w.A, ROOT, "k1")
        await mailbox.cancel(corr, "stopped")
        reader = PgTaskGraphReader(w.conninfo(_DATABASE))
        return await reader.asks_under("t", ROOT)

    (record,) = asyncio.run(scenario()).records

    assert record.state is BackgroundState.CANCELLED and record.finished_at is not None
    with psycopg.connect(w.conninfo(_DATABASE)) as conn:
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'peer_messages'"
            ).fetchall()
        }
    assert "ix_peer_messages_root" in indexes


def test_a_read_over_the_limit_keeps_the_oldest_asks_and_counts_the_rest() -> None:
    async def scenario() -> Any:
        await w.fresh_databases(_DATABASE)
        mailbox = PgAgentMailbox(w.conninfo(_DATABASE))
        for n in range(5):
            await _ask(mailbox, w.B, w.A, ROOT, f"k{n}")
        reader = PgTaskGraphReader(w.conninfo(_DATABASE))
        return await reader.asks_under("t", ROOT, limit=3), await reader.asks_under("t", ROOT)

    cut, whole = asyncio.run(scenario())

    assert len(cut.records) == 3 and cut.omitted == 2
    assert len(whole.records) == 5 and whole.omitted == 0
    assert [r.handle for r in cut.records] == [r.handle for r in whole.records[:3]]


def test_a_spawned_subagent_is_read_back_with_its_kind() -> None:
    """C28, migration 0041: the row says whether the child was asked or spawned, and the tree
    carries it (a row from before the migration reads as a peer)."""
    from agent_core.domain.agent_mode import DelegationKind

    async def scenario() -> Any:
        await w.fresh_databases(_DATABASE)
        mailbox = PgAgentMailbox(w.conninfo(_DATABASE))
        peer = await _ask(mailbox, w.B, w.A, ROOT, "k-peer")
        sub = await mailbox.ask(
            w.profiles()[w.A].peers,
            AgentId(w.C),
            "do it",
            from_session=SESSION,
            turn_id=TurnId(ROOT),
            hop=0,
            asker=AgentId(w.A),
            request_key="k-sub",
            root_turn_id=ROOT,
            kind=DelegationKind.SUBAGENT,
            subagent_depth=1,
        )
        reader = PgTaskGraphReader(w.conninfo(_DATABASE))
        return peer, sub, build_task_graph(ROOT, (await reader.asks_under("t", ROOT)).records)

    peer, sub, graph = asyncio.run(scenario())

    kinds = {child.handle: child.kind for child in graph.root.children}
    assert kinds == {peer: DelegationKind.PEER, sub: DelegationKind.SUBAGENT}
