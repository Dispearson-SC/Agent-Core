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


def _bindings(tree: ast.AST) -> dict[str, str]:
    """Local name -> the dotted module it was bound to by an import (`import a.b as x`: x -> a.b;
    `import a.b`: a -> a; `from a import b as x`: x -> a.b). Absolute imports only."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    bound[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _dotted(node: ast.expr, bound: dict[str, str]) -> str | None:
    """`a.b.c` as a dotted path with its root name replaced by the module it was bound to; `None`
    when the chain is not rooted in an imported name (`self.domain.x`, a call result, ...)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bound:
        return None
    return ".".join([bound[node.id], *reversed(parts)])


def scan_source(source: str, filename: str = "<addon>") -> list[str]:
    """Every rule `source` breaks, as `file:line: sentence`. Empty when it is clean."""
    violations: list[str] = []

    def report(node: ast.AST, sentence: str) -> None:
        violations.append(f"{filename}:{getattr(node, 'lineno', 0)}: {sentence}")

    tree = ast.parse(source, filename=filename)
    bound = _bindings(tree)
    sys_names = {name for name, module in bound.items() if module == "sys"}
    inner: set[int] = set()  # the sub-chains of an attribute chain already judged as a whole
    for node in ast.walk(tree):
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
        elif isinstance(node, ast.ImportFrom) and node.level > 0 and node.module:
            # Relative to the addon's own package, so harmless unless it is dressed up as Core's.
            if _forbidden_import(node.module) or any(
                _forbidden_import(f"{node.module}.{alias.name}") for alias in node.names
            ):
                report(node, f"imports {node.module}, spelled relatively to look like Core's")
        elif isinstance(node, ast.Call):
            # A bare name is the builtin. `obj.exec(...)` is some object's method and is no
            # concern; the module routes (`importlib.import_module`) are caught at their import.
            function = node.func
            if isinstance(function, ast.Name) and function.id in _FORBIDDEN_CALLS:
                report(node, f"calls {function.id}(), a way around the import rules")
            elif (
                isinstance(function, ast.Attribute)
                and function.attr in _FORBIDDEN_CALLS
                and isinstance(function.value, ast.Name)
                and function.value.id in {"importlib", "builtins", "__builtins__"}
            ):
                report(node, f"calls {function.attr}(), a way around the import rules")
        elif isinstance(node, ast.Attribute) and id(node) not in inner:
            sub = node.value
            while isinstance(sub, ast.Attribute):
                inner.add(id(sub))
                sub = sub.value
            if isinstance(sub, ast.Name) and sub.id in sys_names and node.attr == "modules":
                report(node, "uses sys.modules, a way around the import rules")
                continue
            path = _dotted(node, bound)
            if path is not None and _forbidden_import(path):
                report(node, f"reaches {path} through attribute access, an inner layer of Core")
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
