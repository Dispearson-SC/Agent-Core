"""C23 (D54): the ownership tree of a turn, and `GET /turns/{id}/graph`.

Subjects: domain/task_graph.py (`build_task_graph`) and the route in adapters/driving/http/routes.py
(`create_app(task_graph=...)`). The Postgres reader is `tests/integration/test_task_graph.py`.

What is defended: a root with two peers and one nested ask is a three-level tree; a background ask
is a child like any other, flagged; an ask whose parent is unknown is `unlinked`, never hung on the
root; durations come from the recorded times; and the route has the same authorisation as reading
the turn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.routes import TurnView, create_app
from agent_core.domain.peers import BackgroundState, peer_workflow_id
from agent_core.domain.task_graph import AskRecord, AskWindow, build_task_graph
from agent_core.domain.turn import CallerIdentity, TurnId

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ROOT = "root-turn"


def _record(handle: str, target: str, parent_turn: str, n: int, **changes: object) -> AskRecord:
    base: dict[str, object] = {
        "handle": handle,
        "target": target,
        "asker": "orchestrator",
        "asking_turn_id": parent_turn,
        "state": BackgroundState.ANSWERED,
        "background": False,
        "redeliveries": 0,
        "asked_at": T0 + timedelta(seconds=n),
        "started_at": T0 + timedelta(seconds=n + 1),
        "finished_at": T0 + timedelta(seconds=n + 4),
    }
    return AskRecord(**{**base, **changes})  # type: ignore[arg-type]


def _two_peers_and_a_nested_ask() -> tuple[AskRecord, ...]:
    return (
        _record("ask-a", "billing", ROOT, 0),
        _record("ask-b", "support", ROOT, 1),
        # billing asked the ledger in turn: its asking turn is billing's own callee turn.
        _record("ask-c", "ledger", peer_workflow_id("ask-a"), 2, asker="billing"),
    )


def test_two_peers_and_one_nested_ask_are_a_three_level_tree() -> None:
    graph = build_task_graph(ROOT, _two_peers_and_a_nested_ask())

    assert graph.root.depth == 3 and graph.unlinked == ()
    assert graph.root.turn_id == ROOT and graph.root.agent == "orchestrator"
    billing, support = graph.root.children
    assert (billing.agent, support.agent) == ("billing", "support")
    (ledger,) = billing.children
    assert (ledger.agent, ledger.handle, ledger.children) == ("ledger", "ask-c", ())
    assert support.children == ()
    assert billing.turn_id == peer_workflow_id("ask-a"), "a callee's turn id is derived, not stored"


def test_each_node_reports_its_state_and_how_long_it_waited_and_ran() -> None:
    graph = build_task_graph(
        ROOT,
        (
            _record("done", "billing", ROOT, 0),
            _record("running", "support", ROOT, 1, state=BackgroundState.RUNNING, finished_at=None),
            _record(
                "queued",
                "ledger",
                ROOT,
                2,
                state=BackgroundState.QUEUED,
                started_at=None,
                finished_at=None,
            ),
        ),
    )

    done, running, queued = graph.root.children
    assert (done.state, done.queued_ms, done.run_ms) == (BackgroundState.ANSWERED, 1000, 3000)
    assert (running.state, running.queued_ms, running.run_ms) == (
        BackgroundState.RUNNING,
        1000,
        None,
    )
    assert (queued.state, queued.queued_ms, queued.run_ms) == (BackgroundState.QUEUED, None, None)


def test_a_background_ask_is_a_flagged_child() -> None:
    graph = build_task_graph(ROOT, (_record("bg", "billing", ROOT, 0, background=True),))

    (child,) = graph.root.children
    assert child.background is True and graph.root.background is False


def test_an_ask_whose_parent_is_unknown_is_unlinked_not_hung_on_the_root() -> None:
    graph = build_task_graph(
        ROOT, (_record("ask-a", "billing", ROOT, 0), _record("lost", "ledger", "no-such-turn", 1))
    )

    assert [c.handle for c in graph.root.children] == ["ask-a"]
    assert [u.handle for u in graph.unlinked] == ["lost"]


def test_a_redelivered_callee_keeps_its_children_under_any_generation() -> None:
    parent = _record("ask-a", "billing", ROOT, 0, redeliveries=1)
    first_attempt_child = _record("ask-c", "ledger", peer_workflow_id("ask-a", 0), 2)
    second_attempt_child = _record("ask-d", "audit", peer_workflow_id("ask-a", 1), 3)

    graph = build_task_graph(ROOT, (parent, first_attempt_child, second_attempt_child))

    (billing,) = graph.root.children
    assert [c.handle for c in billing.children] == ["ask-c", "ask-d"]
    assert billing.turn_id == peer_workflow_id("ask-a", 1), "the live generation"


def test_a_row_that_points_at_itself_cannot_loop() -> None:
    looping = _record("ask-a", "billing", peer_workflow_id("ask-a"), 0)

    graph = build_task_graph(ROOT, (looping,))

    assert graph.root.children == () and [u.handle for u in graph.unlinked] == ["ask-a"]


def test_an_empty_tree_is_a_lone_root() -> None:
    graph = build_task_graph(ROOT, ())

    assert graph.root.children == () and graph.root.depth == 1 and graph.root.agent is None


# --- the route ------------------------------------------------------------------------------------

HEADERS = {"X-Subject-Id": "u", "X-Tenant-Id": "t"}


class _Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def asks_under(self, tenant_id: str, root_turn_id: str, *, limit: int = 500) -> AskWindow:
        self.calls.append((tenant_id, root_turn_id))
        return AskWindow(_two_peers_and_a_nested_ask())


class _Lookup:
    async def __call__(self, turn_id: TurnId, caller: CallerIdentity) -> TurnView | None:
        if (str(caller.tenant_id), str(turn_id)) == ("t", ROOT):
            return TurnView(turn_id=turn_id, status="finished")
        return None


class _Starter:
    async def __call__(self, request: object) -> object:
        raise AssertionError("not used")


def _client(reader: _Reader | None) -> TestClient:
    return TestClient(
        create_app(start_turn=_Starter(), lookup_turn=_Lookup(), task_graph=reader)  # type: ignore[arg-type]
    )


def test_the_route_returns_the_three_level_tree_as_json() -> None:
    reader = _Reader()
    with _client(reader) as client:
        response = client.get(f"/turns/{ROOT}/graph", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["turn_id"] == ROOT and body["unlinked"] == []
    billing = body["children"][0]
    assert billing["agent"] == "billing" and billing["state"] == "answered"
    assert billing["queued_ms"] == 1000 and billing["run_ms"] == 3000
    assert billing["children"][0]["agent"] == "ledger"
    assert reader.calls == [("t", ROOT)], "the tenant is the authenticated one"


def test_the_route_refuses_what_reading_the_turn_refuses() -> None:
    reader = _Reader()
    with _client(reader) as client:
        anonymous = client.get(f"/turns/{ROOT}/graph")
        foreign = client.get(f"/turns/{ROOT}/graph", headers={**HEADERS, "X-Tenant-Id": "other"})
        unknown = client.get("/turns/never/graph", headers=HEADERS)

    assert anonymous.status_code == 401
    assert foreign.status_code == unknown.status_code == 404
    assert reader.calls == [], "an unauthorised caller never reaches the mailbox"


def test_a_process_without_a_graph_reader_says_unavailable() -> None:
    with _client(None) as client:
        assert client.get(f"/turns/{ROOT}/graph", headers=HEADERS).status_code == 503


def test_a_spawned_subagent_is_a_child_like_any_other_and_says_what_it_is() -> None:
    """C28: peers and subagents share one ownership tree; each node names its kind and the root
    (a turn a person started) has none."""
    from agent_core.adapters.driving.http.event_routes import graph_json
    from agent_core.domain.agent_mode import DelegationKind

    graph = build_task_graph(
        ROOT,
        (
            _record("ask-a", "billing", ROOT, 0),
            _record("spawn-b", "researcher", ROOT, 1, kind=DelegationKind.SUBAGENT),
            _record(
                "spawn-c",
                "writer",
                peer_workflow_id("spawn-b"),
                2,
                asker="researcher",
                kind=DelegationKind.SUBAGENT,
            ),
        ),
    )

    assert graph.root.kind is None
    billing, researcher = graph.root.children
    assert (billing.kind, researcher.kind) == (DelegationKind.PEER, DelegationKind.SUBAGENT)
    assert researcher.children[0].kind is DelegationKind.SUBAGENT
    body = graph_json(graph)
    assert body["kind"] is None
    assert [c["kind"] for c in body["children"]] == ["peer", "subagent"]
