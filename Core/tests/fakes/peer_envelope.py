"""Reading the C8 `<peer ...>` envelope back in tests that do not care about its attributes."""

from __future__ import annotations

import re

_ENVELOPE = re.compile(
    r'\A<peer id="[^"]*"(?: session="[^"]*")? status="(\w+)"[^>]*>\n(.*)\n</peer>\Z', re.S
)


def contents(payload: object, *, status: str = "ok") -> str:
    """The text between the tags of an envelope with the given status; fails if it is not one."""
    assert isinstance(payload, str), f"not a string: {payload!r}"
    found = _ENVELOPE.match(payload)
    assert found is not None, f"not a <peer> envelope: {payload!r}"
    assert found.group(1) == status, f"status {found.group(1)!r}, wanted {status!r}"
    return found.group(2)
