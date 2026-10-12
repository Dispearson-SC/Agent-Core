"""Checking typed peer answers against a profile's `answer_schema` (C9, D44).

The value side lives in `domain/typed_answer.py` (canonical schema text, no validator: domain/
imports nothing external). Here `jsonschema` does the validating, for the three places that need
it: startup (is the schema itself valid), the callee's runner (does the model's output match: a
miss is a retry) and the asking workflow (does the stored answer match before it is labelled
`data`).
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_core.domain.typed_answer import AnswerSchemaError

# How many violations one message names. The model sees the message when it is asked to retry, and a
# schema with fifty required fields should not turn into fifty lines of retry prompt.
_MAX_PROBLEMS = 3


def check_answer_schema(schema_json: str) -> None:
    """Raise `AnswerSchemaError` when `schema_json` is not a valid JSON Schema."""
    try:
        Draft202012Validator.check_schema(json.loads(schema_json))
    except SchemaError as error:
        raise AnswerSchemaError(
            f"answer_schema is not a valid JSON Schema: {error.message}"
        ) from error


def answer_problem(schema_json: str, value: Any) -> str | None:
    """Why `value` does not match the schema, or None when it does. Short enough to hand back to a
    model as a retry reason; the paths name the fields, the values are not repeated."""
    validator = Draft202012Validator(json.loads(schema_json))
    errors = sorted(validator.iter_errors(value), key=lambda error: list(map(str, error.path)))
    if not errors:
        return None
    lines = []
    for error in errors[:_MAX_PROBLEMS]:
        where = ".".join(str(part) for part in error.path) or "(root)"
        lines.append(f"{where}: {error.message}")
    return "; ".join(lines)


def answer_text_problem(schema_json: str, text: str) -> str | None:
    """`answer_problem` for an answer that arrives as text: it must be a JSON object first."""
    try:
        value = json.loads(text)
    except ValueError:
        return "the answer is not valid JSON"
    if not isinstance(value, dict):
        return "the answer is not a JSON object"
    return answer_problem(schema_json, value)
