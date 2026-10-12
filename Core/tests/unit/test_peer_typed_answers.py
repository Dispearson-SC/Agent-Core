"""C9 (D44): the asker receives a typed answer as validated `data`.

Subject: adapters/driving/workflow/turn_workflow.py (`_peer_envelope`), the callee profile's
`answer_schema`. The workflow BODY is driven without DBOS, like `test_peer_fanout.py`.

The callee validated its output before it answered; the asker checks again because the answer went
through a mailbox (over A2A, through somebody else's code) before it reached the model.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.domain.typed_answer import canonical_schema
from tests.fakes import fanout

SCHEMA = canonical_schema(
    {
        "type": "object",
        "properties": {"risk": {"type": "string", "enum": ["low", "high"]}},
        "required": ["risk"],
    }
)


def _run_one(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    *,
    schema: str | None = SCHEMA,
    failed: bool = False,
    max_chars: int | None = None,
) -> str:
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", answer, failed=failed)]
    )
    fanout.callee_profile(answer_schema=schema)
    if max_chars is not None:
        fanout.asker_policy(max_answer_chars=max_chars)
    asyncio.run(fanout.body()(fanout.request()))
    return str(dict(world.resume.calls[0])["tc-1"])


def test_a_valid_typed_answer_reaches_the_asker_marked_as_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _run_one(monkeypatch, '{"risk":"low"}')

    assert 'status="ok" data="json"' in envelope
    assert wrap_peer_answer('{"risk":"low"}') in envelope, "still inside the untrusted wrapper"


def test_an_answer_that_does_not_match_the_callees_schema_is_an_error_without_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _run_one(monkeypatch, '{"risk":"extreme"}')

    assert 'status="error"' in envelope and "data=" not in envelope
    assert "extreme" not in envelope, "an answer that broke the contract is not shown"
    assert "schema" in envelope


def test_prose_from_a_typed_callee_is_an_error_too(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _run_one(monkeypatch, "the risk is low")

    assert 'status="error"' in envelope and "data=" not in envelope


def test_an_untyped_callee_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _run_one(monkeypatch, '{"risk":"extreme"}', schema=None)

    assert 'status="ok"' in envelope and "data=" not in envelope


def test_a_failed_typed_callee_is_an_error_and_not_data(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _run_one(monkeypatch, "failed", failed=True)

    assert 'status="error"' in envelope and "data=" not in envelope


def test_a_cut_answer_is_not_data(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _run_one(monkeypatch, '{"risk":"low","note":"' + "x" * 200 + '"}', max_chars=50)

    assert 'status="ok"' in envelope and 'truncated="true"' in envelope
    assert "data=" not in envelope, "cut JSON is not an object any more"
