#!/usr/bin/env python
"""Point the eight glazed profiles at one model.

Run:  python Core/scripts/set_glazed_model.py minimax/MiniMax-M3.1-flash-preview

The Core has no env/config override for a profile's model (a profile's `model:` is the single
source of truth), so this rewrites the top-level `model:` line of every
`Core/profiles/glazed_*.yaml` and nothing else. Idempotent; prints the files it changed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_PROFILES_DIR = Path(__file__).resolve().parents[1] / "profiles"
_MODEL_LINE = re.compile(r"^model:.*$", re.MULTILINE)


def set_model(profiles_dir: Path, model: str) -> list[Path]:
    """Rewrite `model:` in each `glazed_*.yaml`; return the files that actually changed."""
    if "/" not in model or model.startswith("/") or model.endswith("/"):
        raise ValueError(f"model must be written as provider/model, got {model!r}")
    files = sorted(profiles_dir.glob("glazed_*.yaml"))
    texts = {}
    for path in files:
        text = path.read_text()
        if len(_MODEL_LINE.findall(text)) != 1:
            raise ValueError(f"{path.name} must have exactly one top-level `model:` line")
        texts[path] = text
    changed = []
    for path, text in texts.items():
        updated = _MODEL_LINE.sub(lambda _m: f"model: {model}", text)
        if updated != text:
            path.write_text(updated)
            changed.append(path)
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: set_glazed_model.py <provider/model>", file=sys.stderr)
        return 2
    try:
        changed = set_model(DEFAULT_PROFILES_DIR, argv[1])
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    for path in changed:
        print(f"updated {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
