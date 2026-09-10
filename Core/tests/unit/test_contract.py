"""THE CONTRACT TEST. Do not weaken it to make a change pass.

    Adding a vertical is one profile file plus one tools package.
    Zero changes to domain/, application/ or ports/.

This is the single test that keeps the architecture from eroding one convenient exception
at a time. If it fails, the port cut is wrong - fix the design, not the test.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

CORE_PACKAGES = ("agent_core/domain", "agent_core/application", "agent_core/ports")

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_core"

# CLAUDE.md, "Layer rules": domain/ imports nothing external and reaches for nothing but
# itself - not even ports/. application/ imports domain/ and ports/ only. Anything else in
# either layer is a design defect, not a test to relax.
LAYER_ALLOWED_INTERNAL: dict[str, tuple[str, ...]] = {
    "domain": ("agent_core.domain",),
    "application": ("agent_core.domain", "agent_core.ports"),
}


def _package_parts(module_path: Path, package_root: Path) -> tuple[str, ...]:
    """Dotted package a module lives in, used to resolve its relative imports."""
    relative = module_path.relative_to(package_root.parent)
    return relative.parts[:-1]


def _resolve(module: str | None, level: int, package: tuple[str, ...]) -> str:
    """Absolute dotted name for an import, resolving `from .x import y` against `package`."""
    if level == 0:
        return module or ""
    base = package[: max(len(package) - (level - 1), 0)]
    return ".".join((*base, module)) if module else ".".join(base)


def _iter_imports(tree: ast.Module, package: tuple[str, ...]) -> Iterator[tuple[int, str]]:
    """Yield (line, absolute dotted module) for every import in one module.

    ast.walk descends into function bodies and `if TYPE_CHECKING:` blocks deliberately. An
    import that only runs for the type checker still declares a dependency, and a lazy
    import buried inside a function is precisely the exception this test exists to catch.
    Static analysis, not an import of the module: importing it would run its side effects
    and would miss the branches that never execute.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, _resolve(node.module, node.level, package)


def _is_allowed(module: str, allowed_internal: tuple[str, ...]) -> bool:
    if not module:
        return False
    top = module.split(".", 1)[0]
    if top == "agent_core":
        return any(
            module == prefix or module.startswith(f"{prefix}.") for prefix in allowed_internal
        )
    # sys.stdlib_module_names rather than a hand-written allowlist: a hand-written one
    # drifts, and the whole point is to catch the library nobody thought to ban.
    return top in sys.stdlib_module_names


def _forbidden_imports(layer: str, package_root: Path = PACKAGE_ROOT) -> list[str]:
    root = package_root / layer
    allowed_internal = LAYER_ALLOWED_INTERNAL[layer]
    modules = sorted(root.rglob("*.py"))
    assert modules, f"no modules found under {root} - the walker is pointed at nothing"

    offences: list[str] = []
    for module_path in modules:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        package = _package_parts(module_path, package_root)
        for lineno, imported in _iter_imports(tree, package):
            if _is_allowed(imported, allowed_internal):
                continue
            location = module_path.relative_to(package_root.parents[1]).as_posix()
            offences.append(f"{location}:{lineno} imports {imported!r}")
    return offences


@pytest.mark.contract
@pytest.mark.skip(reason="F4 - enable with the first vertical")
def test_adding_a_vertical_does_not_touch_the_core() -> None:
    """PSEUDO-CODE - implement in F4.

    1. git diff --name-only <base>..HEAD
    2. Assert no path matches CORE_PACKAGES.
    3. Skip when the diff is empty (nothing to judge).

    Deliberately a git-level check rather than an import check: it catches a change to a
    domain dataclass made "just to add one field for the new vertical", which is exactly
    how this kind of contract dies.
    """


@pytest.mark.contract
def test_domain_and_application_import_nothing_external() -> None:
    """Every import under domain/ and application/ is stdlib or agent_core.{domain,ports}.

    ruff's banned-api catches the four named libraries; this catches the fifth one nobody
    thought to ban. domain/ is held to the stricter rule: itself and the standard library,
    with no reach into ports/.

    If this fails, delete the import - do not widen the allowlist.
    """
    offences = _forbidden_imports("domain") + _forbidden_imports("application")
    assert not offences, (
        "domain/ may import the standard library and agent_core.domain.* only; "
        "application/ may also import agent_core.ports.*. Offending imports:\n  "
        + "\n  ".join(offences)
    )
