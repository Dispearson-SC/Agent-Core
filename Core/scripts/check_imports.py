#!/usr/bin/env python
"""Assert every module under agent_core imports cleanly.

Run:  python Core/scripts/check_imports.py       (or: make imports)

WHY THIS EXISTS AS ITS OWN CHECK
    The repository is spec-and-skeleton: most functions raise NotImplementedError, so the
    test suite proves very little about whether the tree is even loadable. A broken import
    in a stub — a bad type reference, a circular import between domain modules — would sit
    undetected until the first phase that touches that file.

    This check is the cheapest possible guard: it fails in under a second and it catches the
    one class of breakage a skipped test suite cannot.

    It is deliberately NOT a pytest test. It must stay runnable before dev dependencies are
    installed, which is exactly when someone first breaks an import.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def main() -> int:
    sys.path.insert(0, str(SRC))
    import agent_core

    failures: list[tuple[str, str]] = []
    loaded = 0

    for module in pkgutil.walk_packages(agent_core.__path__, "agent_core."):
        try:
            importlib.import_module(module.name)
            loaded += 1
        except Exception:
            failures.append((module.name, traceback.format_exc(limit=3)))

    print(f"modules loaded: {loaded}   failed: {len(failures)}")

    for name, tb in failures:
        print(f"\n--- {name} ---\n{tb}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
