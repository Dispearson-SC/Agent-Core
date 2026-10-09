"""The composition root wires `DecideApproval`, and its requester seat is bound.

Phase:   F3
Tasks:   docs/TASKS.md#t-f3-15
Decision: docs/DECISIONS.md#d25

D25's "Still open" note said it plainly: nothing constructs `DecideApproval` in
`composition.py`, so the requester seat defaults to `None` and an unwired use case never
evaluates the four-eyes rule. `Core/tests/unit/test_four_eyes.py` pins that unwired
default on the use case itself; this file pins the OTHER half - that the composition root
does not leave it that way.

No Postgres, no network, no DBOS running. `build_container` must stay callable on a laptop
with nothing up (see `test_composition.py`), so this test uses the same recording-pool
stand-in that file does rather than pulling in real infrastructure.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from agent_core.application.decide_approval import DecideApproval


class _RecordingConnection:
    """Stands in for a psycopg connection. Records nothing, executes nothing for real."""

    def execute(self, sql: str, params: Any = None) -> Any:
        return self

    def fetchone(self) -> None:
        return None


class _RecordingPool:
    """Stands in for `psycopg_pool.ConnectionPool` - never opens a socket."""

    def __init__(self, conninfo: str, **_: object) -> None:
        self.conninfo = conninfo

    @contextmanager
    def connection(self) -> Iterator[_RecordingConnection]:
        yield _RecordingConnection()

    def close(self) -> None:
        return None


def _composition() -> Any:
    return importlib.import_module("agent_core.composition")


def _body_raises_not_implemented(method: Any) -> bool:
    """True when `method`'s own body is a `raise NotImplementedError`.

    Duplicated from `test_composition.py` rather than imported from it - that module is
    another anchor's file this wave, and importing a private helper out of a sibling test
    module couples the two for no reason a reader could point at later.
    """
    source = textwrap.dedent(inspect.getsource(method))
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(raised, ast.Name) and raised.id == "NotImplementedError":
            return True
    return False


@pytest.mark.phase("F3")
def test_the_container_exposes_a_decide_approval_with_its_requester_seat_bound() -> None:
    """t-f3-15: the container builds a real `DecideApproval`, requester seat included.

    Three things are checked, and each rules out a different way this could look wired
    while not being wired:

    1. The seat exists at all - a container with no `decide_approval` gives the approval
       route (t-f3-11) nothing to call.
    2. It is the real class, not a double standing in for it.
    3. `execute`'s own body is not `raise NotImplementedError` - the exact shape the SEATS
       note in `composition.py` calls "a container that builds and dies on the first
       turn", here for the first decision instead of the first turn.
    4. The `requester` seat itself is bound to something other than `None`. D25's default
       of `None` is what lets an unwired use case skip the four-eyes rule entirely
       (`decide_approval.py`, `execute`: `if approved and self._requester is not None`) -
       so a `decide_approval` built with the seat still empty would look wired from the
       outside while enforcing nothing.
    """
    composition = _composition()
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    decide_approval = getattr(container, "decide_approval", None)
    assert decide_approval is not None, (
        "the container exposes no decide_approval - D25's requester seat has no binder "
        "and the approval route (t-f3-11) has nothing to call"
    )
    assert isinstance(decide_approval, DecideApproval), (
        f"container.decide_approval is a {type(decide_approval)!r}, not the real "
        "DecideApproval use case"
    )

    assert not _body_raises_not_implemented(decide_approval.execute), (
        "decide_approval.execute is a stub that raises - a container that builds and "
        "dies on the first decision is exactly the bug composition.py exists to prevent"
    )

    seats = vars(decide_approval)
    assert seats.get("_requester") is not None, (
        "DecideApproval's requester seat is unbound (None): an approval would never "
        "evaluate the four-eyes rule (D25), and every approver would silently be "
        "treated as a valid second person"
    )
