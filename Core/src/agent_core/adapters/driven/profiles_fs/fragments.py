"""An addon's prompt fragment, rendered with the addon's own settings.

Phase:   Phase 3 (white-label addons), G4
Tests:   Core/tests/unit/test_addon_fragment_text.py
Decision: docs/DECISIONS.md D89

WHY THIS EXISTS
    A fragment is the text an addon contributes to a persona (`provides.fragments`). A generic
    addon (`plain_language`) has to say different things for different deployments (who the
    reader is, which language, whether answers are spoken), and the manifest's `settings:` is
    where an operator already edits those. So a fragment may read the settings of the addon that
    ships it, through a deliberately small language:

        {{setting:name}}                   inline; replaced by the setting's value
        {{if:name}}                        a block, each marker alone on its line;
        ...                                kept when the setting is true (a non-empty string, a
        {{endif}}                          non-zero number, `true`), dropped with its markers if not

    Nothing else: no expressions, no nesting, no loops. A name the addon does not declare, an
    unclosed or nested block and a stray `{{endif}}` raise `ValueError`, which registration turns
    into an `AddonRegistrationError` naming the addon and the file. A value is inserted as is and
    never expanded again. Core's own fragments are never rendered, and `{{slot:x}}` is untouched.

HOW IT REACHES THE LOADER
    `AddonFragment` looks like the `Path` the loaders already read (`is_file`, `read_text`,
    `name`, `stem`), so `fragment_reader` and the slot inserts need no second code path: the
    registration puts an `AddonFragment` where it used to put the `Path`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

__all__ = ["AddonFragment", "FragmentFile", "render_fragment"]

_SETTING = re.compile(r"\{\{setting:([A-Za-z_][A-Za-z0-9_]*)\}\}")
_IF = re.compile(r"\{\{if:([A-Za-z_][A-Za-z0-9_]*)\}\}")
_ENDIF = "{{endif}}"


class FragmentFile(Protocol):
    """What a loader needs of a fragment: a `Path`, or an `AddonFragment`."""

    def is_file(self) -> bool: ...

    def read_text(self, encoding: str | None = None) -> str: ...


def _value(settings: Mapping[str, Any], name: str) -> Any:
    if name not in settings:
        raise ValueError(f"{{{{...:{name}}}}} names a setting the addon does not declare")
    return settings[name]


def render_fragment(text: str, settings: Mapping[str, Any]) -> str:
    """`text` with `{{setting:x}}` replaced and `{{if:x}}` blocks kept or dropped."""
    out: list[str] = []
    open_block: str | None = None
    keep = True
    for line in text.split("\n"):
        stripped = line.strip()
        start = _IF.fullmatch(stripped)
        if start is not None:
            if open_block is not None:
                raise ValueError(
                    f"{{{{if:{start.group(1)}}}}} inside {{{{if:{open_block}}}}}: no nesting"
                )
            open_block = start.group(1)
            keep = bool(_value(settings, open_block))
            continue
        if stripped == _ENDIF:
            if open_block is None:
                raise ValueError("{{endif}} without a {{if:...}} before it")
            open_block, keep = None, True
            continue
        if "{{if:" in line or _ENDIF in line:
            raise ValueError(
                f"{{{{if:...}}}} and {{{{endif}}}} must each be alone on their line: {line!r}"
            )
        if keep:
            out.append(_SETTING.sub(lambda m: str(_value(settings, m.group(1))), line))
    if open_block is not None:
        raise ValueError(f"{{{{if:{open_block}}}}} is never closed by {{{{endif}}}}")
    return "\n".join(out)


@dataclass(frozen=True)
class AddonFragment:
    """One fragment file of an addon, read through its settings."""

    path: Path
    settings: Mapping[str, Any]

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    def is_file(self) -> bool:
        return self.path.is_file()

    def read_text(self, encoding: str | None = None) -> str:
        return render_fragment(self.path.read_text(encoding=encoding), self.settings)
