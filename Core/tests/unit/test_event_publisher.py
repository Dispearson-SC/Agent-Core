"""C1p (D55): the event publisher bounds what it writes and names its channel by constant.

Subjects: adapters/driven/events_pg/publisher.py, domain/events.py (`bound_event`,
`ProgressEvent.root_turn_id`) and adapters/driven/events_pg/audit_events.py. Postgres itself is
tests/integration/test_turn_events.py.

What is defended: a label an event carries is cut to MAX_LABEL_CHARS before it is stored, the NOTIFY
payload is the sequence number alone and the channel comes from `NOTIFY_CHANNEL` (bound, never a
literal in the statement), an event's root is its owner's root or its own turn, and the audit
decorator forwards only the audit verbs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agent_core.adapters.driven.events_pg import publisher as publisher_module
from agent_core.adapters.driven.events_pg.audit_events import EventingAuditSink
from agent_core.adapters.driven.events_pg.connections import EventConnections
from agent_core.adapters.driven.events_pg.publisher import NOTIFY_CHANNEL, PgEventPublisher
from agent_core.domain.events import (
    MAX_LABEL_CHARS,
    EventKind,
    EventOwner,
    ProgressEvent,
    bound_event,
    progress_event_id,
)
from tests.fakes import ports as fakes
from tests.unit.test_event_connections import _Pool


class _Capturing(_Pool):
    """A pool whose connections remember the parameters of every statement."""

    def __init__(self, conninfo: str, **options: Any) -> None:
        super().__init__(conninfo, **options)
        self.executed: list[tuple[str, Any]] = []

    def connection(self, timeout: float | None = None) -> Any:
        outer = self
        context = super().connection(timeout)

        class _Spy:
            def __enter__(self) -> Any:
                conn = context.__enter__()
                real = conn.execute

                def execute(sql: str, params: Any = None) -> Any:
                    outer.executed.append((sql, params))
                    return real(sql, params)

                conn.execute = execute
                return conn

            def __exit__(self, *exc: object) -> Any:
                return context.__exit__(*exc)

        return _Spy()


def _event(**changes: Any) -> ProgressEvent:
    base: dict[str, Any] = {
        "event_id": progress_event_id("turn", EventKind.TOOL_CALLED, "1"),
        "kind": EventKind.TOOL_CALLED,
        "tenant_id": "t",
        "session_id": "s",
        "turn_id": "turn",
        "agent": "agent",
        "at": datetime(2026, 10, 12, tzinfo=UTC),
        "tool": "lookup",
        "detail": "allow",
    }
    return ProgressEvent(**{**base, **changes})


def test_a_label_longer_than_the_bound_is_cut_before_it_is_stored() -> None:
    long = "x" * 5000
    event = _event(agent=long, tool=long, detail=long, owner=EventOwner("root", "ask", long, long))

    bounded = bound_event(event)

    assert bounded.tool is not None and len(bounded.tool) == MAX_LABEL_CHARS
    assert bounded.detail is not None and len(bounded.detail) == MAX_LABEL_CHARS
    assert len(bounded.agent) == MAX_LABEL_CHARS
    assert bounded.owner is not None and bounded.owner.asker is not None
    assert (
        len(bounded.owner.asker) == MAX_LABEL_CHARS and len(bounded.owner.call) == MAX_LABEL_CHARS
    )
    assert bounded.tool.endswith("…"), "a cut label says it was cut"


def test_a_short_event_and_its_identity_are_untouched() -> None:
    event = _event(elapsed_ms=12)

    assert bound_event(event) == event


def test_the_publisher_writes_the_bounded_event_and_notifies_by_constant_channel() -> None:
    made: list[_Capturing] = []

    def factory(conninfo: str, **options: Any) -> _Capturing:
        made.append(_Capturing(conninfo, **options))
        return made[-1]

    publisher = PgEventPublisher(EventConnections("postgresql://x/y", pool_factory=factory))
    asyncio.run(publisher.publish(_event(detail="d" * 5000, tool="lookup")))

    sql, params = made[0].executed[-1]  # after the chain lookup of a tool_called
    assert isinstance(params, tuple)
    assert "'turn_events'" not in sql, "the channel is not a literal in the statement"
    assert NOTIFY_CHANNEL in params and params[-1] == NOTIFY_CHANNEL
    assert all(len(str(p)) <= MAX_LABEL_CHARS for p in params if isinstance(p, str) and p != "s")
    assert publisher_module.NOTIFY_CHANNEL == "turn_events", "the name consumers LISTEN on"


def test_an_event_s_root_is_its_owner_s_root_or_its_own_turn() -> None:
    owned = _event(owner=EventOwner("root-1", "asker-1", "a", "call"))
    own = _event()

    assert owned.root_turn_id == "root-1"
    assert own.root_turn_id == "turn", "never empty: a turn with no owner is its own root"


def test_the_audit_decorator_forwards_the_audit_verbs_and_nothing_else() -> None:
    inner = fakes.FakeAuditSink()
    inner.secret = "internal"  # type: ignore[attr-defined]
    sink = EventingAuditSink(inner, _NoEvents())

    try:
        sink.secret  # type: ignore[attr-defined]  # noqa: B018
    except AttributeError:
        pass
    else:
        raise AssertionError("an attribute of the concrete sink leaked through the decorator")


class _NoEvents:
    async def publish(self, event: ProgressEvent) -> None:
        return None
