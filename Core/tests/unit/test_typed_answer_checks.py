"""C9 (D44): checking an answer against a profile's schema, and refusing a bad schema at startup.

Subject: adapters/driven/peers/typed_answer.py, composition.py (`_refuse_unusable_answer_schemas`).
"""

from __future__ import annotations

import pytest

from agent_core import composition
from agent_core.adapters.driven.peers.typed_answer import (
    answer_problem,
    answer_text_problem,
    check_answer_schema,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.typed_answer import AnswerSchemaError, canonical_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["low", "high"]},
        "score": {"type": "number"},
    },
    "required": ["risk"],
    "additionalProperties": False,
}


def test_a_valid_answer_has_no_problem() -> None:
    schema = canonical_schema(SCHEMA)

    assert answer_problem(schema, {"risk": "low", "score": 0.2}) is None
    assert answer_text_problem(schema, '{"risk": "high"}') is None


@pytest.mark.parametrize(
    "value",
    [{"score": 1}, {"risk": "medium"}, {"risk": "low", "extra": 1}, {"risk": "low", "score": "x"}],
)
def test_an_answer_that_does_not_match_the_schema_names_the_problem(
    value: dict[str, object],
) -> None:
    problem = answer_problem(canonical_schema(SCHEMA), value)

    assert problem is not None and problem.strip()


def test_text_that_is_not_json_or_not_an_object_is_a_problem() -> None:
    schema = canonical_schema(SCHEMA)

    assert "JSON" in (answer_text_problem(schema, "just prose") or "")
    assert answer_text_problem(schema, '["risk"]') is not None


def test_a_root_object_with_a_broken_body_is_not_a_schema() -> None:
    broken = canonical_schema({"type": "object", "properties": 3})

    with pytest.raises(AnswerSchemaError, match="valid JSON Schema"):
        check_answer_schema(broken)


def _profile(profile_id: str, **extra: object) -> AgentProfile:
    return AgentProfile.from_mapping({"id": profile_id, "persona": "P", "model": "m", **extra})


def test_startup_refuses_a_profile_whose_schema_is_not_valid_and_names_it() -> None:
    profiles = {
        "good": _profile("good", answer_schema=SCHEMA),
        "plain": _profile("plain"),
        "bad": _profile("bad", answer_schema={"type": "object", "required": "risk"}),
    }

    with pytest.raises(ValueError, match="'bad'") as caught:
        composition._refuse_unusable_answer_schemas(profiles)  # noqa: SLF001

    assert "'good'" not in str(caught.value)


def test_startup_accepts_valid_and_absent_schemas() -> None:
    composition._refuse_unusable_answer_schemas(  # noqa: SLF001
        {"good": _profile("good", answer_schema=SCHEMA), "plain": _profile("plain")}
    )
