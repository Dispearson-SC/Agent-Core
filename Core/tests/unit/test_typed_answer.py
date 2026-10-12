"""C9 (D44): `answer_schema` on a profile, as a value.

Subject: domain/typed_answer.py, domain/profile.py (`answer_schema`). Checking an answer against it
is the adapter's (`test_typed_answer_checks.py`): domain/ imports no validator.
"""

from __future__ import annotations

import json

import pytest

from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.typed_answer import AnswerSchemaError, canonical_schema, render_answer

SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["low", "high"]},
        "score": {"type": "number"},
    },
    "required": ["risk"],
    "additionalProperties": False,
}


def _profile(**extra: object) -> AgentProfile:
    return AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "m", **extra})


def test_the_canonical_schema_is_stable_whatever_the_key_order() -> None:
    reordered = dict(reversed(list(SCHEMA.items())))

    assert canonical_schema(SCHEMA) == canonical_schema(reordered)
    assert json.loads(canonical_schema(SCHEMA)) == SCHEMA


@pytest.mark.parametrize(
    "bad",
    [None, "object", [], {"type": "string"}, {}, {"type": "object", "x": {1, 2}}],
)
def test_a_schema_that_is_not_an_object_schema_is_refused(bad: object) -> None:
    with pytest.raises(AnswerSchemaError):
        canonical_schema(bad)


def test_the_answer_is_rendered_as_compact_json_in_the_models_key_order() -> None:
    assert render_answer({"risk": "low", "score": 1}) == '{"risk":"low","score":1}'
    assert render_answer({"name": "Zoë"}) == '{"name":"Zoë"}'


def test_a_profile_has_no_schema_by_default_and_prose_stays_the_default() -> None:
    assert _profile().answer_schema is None


def test_a_profile_loads_a_schema_and_it_is_part_of_the_content_hash() -> None:
    typed = _profile(answer_schema=SCHEMA)

    assert typed.answer_schema == canonical_schema(SCHEMA)
    assert typed.content_hash != _profile().content_hash
    assert (
        typed.content_hash
        == _profile(answer_schema=dict(reversed(list(SCHEMA.items())))).content_hash
    )


@pytest.mark.parametrize("bad", ["object", [], {"type": "array"}])
def test_a_profile_with_an_unusable_schema_refuses_to_load(bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="answer_schema"):
        _profile(answer_schema=bad)
