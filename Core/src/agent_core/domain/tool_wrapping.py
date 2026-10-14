"""How a tool that an addon wraps is marked, so the audit trail can say so (A6, D78). Pure.

A wrapped tool is still ONE tool: the same name, schema, replay class and policy identity. What
changes is a fact about it - which addons wrap it - and that fact travels in two places:

  * `ToolDefinition.metadata[WRAPPED_BY_METADATA_KEY]`: the addon names, innermost first. The
    wrapping toolset sets it; the runner reads it.
  * the audit row's arguments under `WRAPPED_BY_AUDIT_KEY`, which the RUNNER sets and is the only
    one allowed to: it drops whatever the model sent under that key before writing its own (D79).
    The sink keeps it whatever the
    tool's allowlist says. It is safe to keep because `wrapped_by` returns ONLY names shaped like
    addon names (lower case words joined by single underscores): no value from the model, no free
    text, nothing a caller can use to smuggle a payload into the trail.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

__all__ = ["WRAPPED_BY_AUDIT_KEY", "WRAPPED_BY_METADATA_KEY", "wrapped_by"]

WRAPPED_BY_METADATA_KEY: Final[str] = "wrapped_by"
WRAPPED_BY_AUDIT_KEY: Final[str] = "_wrapped_by"

# The shape of an addon name (manifest `name`), bounded: anything else is not a name.
_ADDON_NAME: Final = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_MAX_NAME: Final = 64


def wrapped_by(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The addons wrapping a tool, from its metadata; `()` when it says nothing usable.

    All or nothing: a list holding anything that is not an addon name reads as no marker at all,
    so a malformed value is never half-trusted.
    """
    raw = (metadata or {}).get(WRAPPED_BY_METADATA_KEY)
    if not isinstance(raw, (list, tuple)):
        return ()
    names = tuple(raw)
    ok = all(
        isinstance(name, str) and len(name) <= _MAX_NAME and _ADDON_NAME.fullmatch(name)
        for name in names
    )
    return tuple(str(name) for name in names) if ok else ()
