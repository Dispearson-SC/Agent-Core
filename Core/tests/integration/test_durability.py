"""F2/F3 acceptance tests. These need a real Postgres and a real DBOS.

READ THIS FIRST
    Every bug these guard against is SILENT. A green unit suite says nothing about any of
    them, because they only appear when a process dies at the wrong moment.

    That is why they are integration tests with deliberate crash injection, not mocks.
"""

import pytest


@pytest.mark.skip(reason="F2 - the acceptance criterion for the phase")
def test_killing_the_process_midturn_does_not_rerun_the_tool() -> None:
    """PSEUDO-CODE - implement in F2.

    1. Start a turn that calls two tools; the first records a side effect.
    2. Kill the process after tool one, before the turn is persisted.
    3. Restart.
    4. Assert the turn completes AND the first tool ran EXACTLY ONCE.

    This is the test the whole durability phase exists for.
    """


@pytest.mark.skip(reason="F3 - the acceptance criterion for the phase")
def test_approval_survives_a_redeploy() -> None:
    """PSEUDO-CODE - implement in F3.

    1. Start a turn that suspends on approval.
    2. Tear the process down and bring it back (simulating a deploy).
    3. Approve.
    4. Assert the turn resumes correctly.

    Fake the clock rather than waiting 24 hours; the point is process death, not elapsed
    time.
    """


@pytest.mark.skip(reason="F3")
def test_tool_call_ids_round_trip_verbatim() -> None:
    """A regenerated or re-cased tool_call_id is dropped by Pydantic AI WITHOUT an
    exception, and the agent asks the same question forever. Assert byte equality."""


@pytest.mark.skip(reason="F3")
def test_resolving_the_same_request_twice_is_a_noop() -> None:
    """Humans double-click and retries re-post. The second resolution must return the
    stored outcome, not run the tool again."""


@pytest.mark.skip(reason="F7")
def test_evidence_request_resumes_with_the_uploaded_image() -> None:
    """The F7 acceptance criterion: the agent asks for a photo, the turn waits, the user
    uploads from another device, the turn resumes with the image as the tool result."""
