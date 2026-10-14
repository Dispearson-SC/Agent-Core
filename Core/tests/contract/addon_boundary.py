"""The static scan that proves an addon's Python never reaches Core's inner layers (A8, D82).

Used by `test_addon_boundary.py`. Pure: parses source text, imports and executes nothing.

THE RULE
    An addon's Python may not import `agent_core.domain`, `agent_core.application` or
    `agent_core.ports` (or anything under them), with ONE exception list, the ALLOWED SURFACE:
    pure, read-only helpers that exist for tool authors. Today that is
    `agent_core.domain.tool_replay` (mark a tool replay-safe). Growing the list is a reviewed
    change to this file and to docs/ADDONS.md, never an addon's own decision.

    It also forbids the routes around an import statement: `__import__`, `importlib`,
    `import_module`, `exec`, `eval`, `sys.modules` and `builtins`. Nothing in the samples needs
    them, and they are how one would patch a module that was never imported.
"""

from __future__ import annotations

import ast
from pathlib import Path

__all__ = ["ALLOWED_SURFACE", "PROTECTED", "scan_directory", "scan_source"]

PROTECTED = ("agent_core.domain", "agent_core.application", "agent_core.ports")
ALLOWED_SURFACE: frozenset[str] = frozenset({"agent_core.domain.tool_replay"})

_FORBIDDEN_MODULES = frozenset({"importlib", "builtins"})
_FORBIDDEN_CALLS = frozenset({"__import__", "exec", "eval", "import_module"})


def _inside(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _forbidden_import(module: str) -> bool:
    if any(_inside(module, allowed) for allowed in ALLOWED_SURFACE):
        return False
    return any(_inside(module, package) for package in PROTECTED)


def scan_source(source: str, filename: str = "<addon>") -> list[str]:
    """Every rule `source` breaks, as `file:line: sentence`. Empty when it is clean."""
    violations: list[str] = []

    def report(node: ast.AST, sentence: str) -> None:
        violations.append(f"{filename}:{getattr(node, 'lineno', 0)}: {sentence}")

    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_import(alias.name):
                    report(node, f"imports {alias.name}, an inner layer of Core")
                if alias.name.split(".")[0] in _FORBIDDEN_MODULES:
                    report(node, f"imports {alias.name}, a way around the import rules")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] in _FORBIDDEN_MODULES:
                report(node, f"imports from {node.module}, a way around the import rules")
            if node.module == "sys" and any(alias.name == "modules" for alias in node.names):
                report(node, "imports sys.modules, a way around the import rules")
            for alias in node.names:
                combined = f"{node.module}.{alias.name}"
                if _forbidden_import(node.module) or _forbidden_import(combined):
                    report(node, f"imports {combined}, an inner layer of Core")
        elif isinstance(node, ast.Call):
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            if name in _FORBIDDEN_CALLS:
                report(node, f"calls {name}(), a way around the import rules")
        elif isinstance(node, ast.Attribute) and node.attr == "modules":
            if isinstance(node.value, ast.Name) and node.value.id == "sys":
                report(node, "uses sys.modules, a way around the import rules")
    return violations


def scan_directory(root: Path) -> tuple[int, list[str]]:
    """Scan every `.py` file under `root`. Returns (files read, violations)."""
    files = sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    violations: list[str] = []
    for path in files:
        violations.extend(
            scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(root)))
        )
    return len(files), violations
