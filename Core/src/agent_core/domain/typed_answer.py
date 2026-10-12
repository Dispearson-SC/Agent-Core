"""Typed peer answers (C9, D44): a profile's `answer_schema`, as a value.

A profile may declare the shape of what it answers with, as a JSON Schema whose root is an object.
The callee then runs with structured output, the runner retries once when the model's output does
not match, and the asking side attaches the answer as `data` only after checking it again against
the callee's schema (the answer travelled through a mailbox, and over A2A it may have been written
by somebody else's code). Prose stays the default: a profile with no schema is untouched.

The schema is kept as CANONICAL JSON TEXT on the profile (sorted keys, compact), not as a mapping:
the profile is a frozen value that is hashed and compared, and the text is the same bytes whatever
order the YAML listed the keys in.

This module is the standard-library half: what can be said about a schema without a validator. The
checking itself (`jsonschema`) lives in `adapters/driven/peers/typed_answer.py`, because domain/
imports nothing external (tests/unit/test_contract.py).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class AnswerSchemaError(ValueError):
    """A profile's `answer_schema` is not usable."""


def canonical_schema(schema: object) -> str:
    """The canonical JSON text of `schema`, or `AnswerSchemaError` when it cannot be an answer.

    The root must be an object schema (`"type": "object"`): structured output is delivered as an
    object by every provider, and a schema that allows anything else could never be satisfied.
    Whether the rest is a VALID JSON Schema is checked at startup (`check_answer_schema`).
    """
    if not isinstance(schema, Mapping):
        raise AnswerSchemaError("answer_schema must be a JSON Schema object")
    if schema.get("type") != "object":
        raise AnswerSchemaError(
            'answer_schema must describe an object (its root needs "type": "object")'
        )
    try:
        return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise AnswerSchemaError(f"answer_schema is not plain JSON: {error}") from error


def render_answer(value: Mapping[str, Any]) -> str:
    """The structured answer as the text the mailbox stores: compact JSON, the model's key order."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
