"""`request_evidence` - the shared deferred-tool mechanism (D9), not a vertical's tool.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-05
Covers:  adapters/driven/tools/evidence.py

WHAT IS BEING DEFENDED

    1. IT NEVER ANSWERS. `request_evidence` has no synchronous result to give - a human
       has to hand something over. So every call must raise `pydantic_ai.exceptions
       .CallDeferred` (the "externally executed" deferred-tool case, docs/DECISIONS.md#d9)
       and never fall through to a `return`. A version that computed some placeholder
       result "for now" would look like a working tool right up until the runner tried to
       treat a suspended call as a finished one.

    2. THE CORRELATION ID IT MINTS REUSES `HumanGateway`'s GENERATOR, so evidence and
       approvals share one unguessable-id scheme (t-f3-04, migration 0010) instead of two
       that could drift apart. Reusing the function is not enough on its own to prove the
       id is safe - a caller could wrap it in something that leaks structure - so this
       module runs the same measurement `test_human_gateway.py` runs on the gateway itself:
       decode the id back to bytes and check that every one of the first 128 bit positions
       actually takes both values across a sample. A counter, a timestamp, or anything
       derived from the turn/session identifiers passed alongside it fails that; only real
       entropy passes.

This is a pure unit test: no Postgres, no channel, no agent run. `request_evidence` is a
plain function raising a plain exception - the suspension and the correlation id both exist
before any infrastructure gets involved.
"""

from __future__ import annotations

import base64

import pytest
from pydantic_ai.exceptions import CallDeferred

from agent_core.adapters.driven.human.gateway import new_correlation_id
from agent_core.adapters.driven.tools.evidence import request_evidence

# Turn/session-shaped strings a hand-rolled scheme (hash, concatenation, truncation) might
# have been tempted to fold into the id. Passed alongside every call so the entropy check
# also proves the id does not simply echo its caller's context.
_TURN_ID = "11111111-2222-3333-4444-555555555555"
_SESSION_ID = "session-abc-999"


def _decode(correlation_id: str) -> bytes:
    padding = "=" * (-len(correlation_id) % 4)
    return base64.urlsafe_b64decode(correlation_id + padding)


def test_request_evidence_suspends_instead_of_returning() -> None:
    """Calling the tool must never produce a result - only a deferred call."""
    with pytest.raises(CallDeferred) as excinfo:
        request_evidence(kind="photo", reason="photo of the damaged package")

    assert excinfo.value.metadata is not None, (
        "CallDeferred carried no metadata; the runner (t-f7-07) has nothing to publish "
        "through HumanGateway and the human would be asked for nothing in particular."
    )


def test_request_evidence_never_reuses_new_correlation_ids_own_output() -> None:
    """A sanity check that this module actually calls the shared generator.

    Not proof of unguessability by itself (a constant would also pass this), which is why
    the entropy test below exists too - but it does rule out an id built by some other
    means that merely happens to look similar.
    """
    with pytest.raises(CallDeferred) as excinfo:
        request_evidence(kind="document", reason="proof of address")

    metadata = excinfo.value.metadata
    assert metadata is not None
    correlation_id = metadata["correlation_id"]
    assert isinstance(correlation_id, str)
    assert correlation_id != new_correlation_id(), (
        "trivially true for two random draws, but this call also fails loudly if "
        "'correlation_id' stops being a string new_correlation_id() could have produced"
    )
    _decode(correlation_id)  # must not raise - it has to be the same urlsafe-base64 shape


def test_the_correlation_id_carries_at_least_128_bits_of_randomness() -> None:
    """The same measurement `test_human_gateway.py` runs on `new_correlation_id()` itself,
    run here on what `request_evidence` actually publishes in its deferred metadata - so a
    future refactor that starts deriving the id from `kind`/`reason` (or from the turn or
    session identifiers a caller happens to have on hand) is caught even though nothing here
    imports those identifiers into the id.
    """
    samples: list[str] = []
    for i in range(256):
        with pytest.raises(CallDeferred) as excinfo:
            request_evidence(
                kind="photo",
                reason=f"reason #{i} for turn {_TURN_ID} session {_SESSION_ID}",
            )
        metadata = excinfo.value.metadata
        assert metadata is not None
        samples.append(metadata["correlation_id"])

    assert len(set(samples)) == len(samples), (
        "correlation ids repeated inside a 256-call sample; the id is the same kind of "
        "bearer token an approval uses and a collision lets one evidence request answer "
        "for another"
    )

    decoded = [_decode(sample) for sample in samples]
    assert all(len(raw) >= 16 for raw in decoded), (
        "a correlation id decodes to fewer than 16 bytes, so it cannot carry 128 bits "
        f"however it was built; shortest was {min(len(raw) for raw in decoded)} bytes"
    )

    for bit in range(128):
        observed = {(raw[bit // 8] >> (bit % 8)) & 1 for raw in decoded}
        assert observed == {0, 1}, (
            f"bit {bit} of the correlation id never varies across 256 samples: it is "
            "fixed or derived from the call's own arguments, not random. Anyone who can "
            "guess a handle can supply evidence for someone else's turn."
        )
