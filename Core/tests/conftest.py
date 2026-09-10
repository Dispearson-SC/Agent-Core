"""Shared fixtures. Every port has a fake, so no test needs Postgres, network or a model.

If a test in tests/unit/ ever needs a real dependency, a port is leaking - fix the port,
do not add the dependency to the test.
"""

import pytest

from agent_core.domain.turn import CallerIdentity, SessionRef


@pytest.fixture
def session() -> SessionRef:
    return SessionRef(session_id="s-1", tenant_id="t-1")  # type: ignore[arg-type]


@pytest.fixture
def caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id="t-1",  # type: ignore[arg-type]
        roles=frozenset({"operator"}),
    )
