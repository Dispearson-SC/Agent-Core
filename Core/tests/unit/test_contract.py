"""THE CONTRACT TEST. Do not weaken it to make a change pass.

    Adding a vertical is one profile file plus one tools package.
    Zero changes to domain/, application/ or ports/.

Phase:   F1 (the import half) / F4 (the vertical half)
Tasks:   docs/TASKS.md#t-f4-03
Status:  BOTH HALVES LIVE - no skip, no marker

This is the single test that keeps the architecture from eroding one convenient exception
at a time. If it fails, the port cut is wrong - fix the design, not the test.

The sentence has two halves and they ask different questions. The import half - does the
core reach for a library or a layer it may not - caught `import yaml` in domain/profile.py
on its first real run. The vertical half - does the core name one specific agent - is the
one this file was skipped for until the first vertical existed to test against. Neither
subsumes the other: a core file can name `pricing_apply` in a tuple without importing
anything, and an `import yaml` names no vertical at all.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync

CORE_PACKAGES = ("agent_core/domain", "agent_core/application", "agent_core/ports")

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_core"
PROFILES_ROOT = Path(__file__).resolve().parents[2] / "profiles"

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


# ---------------------------------------------------------------------------------------
# The vertical half of the contract - docs/TASKS.md#t-f4-03
#
# WHAT "A VERTICAL'S DIFF" MEANS HERE, AND WHY IT IS NOT `git diff`
#     The stub this replaced prescribed `git diff --name-only <base>..HEAD`. That shape was
#     rejected for two reasons, both of which turn it into a green tick rather than a test:
#
#     1. `<base>` is a human deciding which commits belong to a vertical. A test that needs
#        that judgement is not a test - it is a checklist with a runner attached.
#     2. It skips when the diff is empty, so on any tree where the vertical is already
#        committed - a fresh clone, a squashed merge, CI on a release tag - it certifies
#        nothing while looking like coverage. This repository has shipped two of those
#        already (docs/TASKS.md#t-f10-01).
#
#     So the diff is computed from the TREE, not from history. A vertical is discovered
#     mechanically: one package directory under adapters/driven/tools/, plus every profile
#     in Core/profiles/ whose `toolsets` names it - exactly the two artefacts the contract
#     sentence allows, and exactly the procedure domain/profile.py documents. Its "diff" is
#     then everything that identifies it: its dotted module path, the public symbols its
#     package defines (its tool names), and the ids of the profiles that select it. If any
#     of those appears in code under domain/, application/ or ports/, adding that vertical
#     touched the core, and this test goes red.
#
# WHAT THIS CANNOT SEE, STATED SO NOBODY MISTAKES SILENCE FOR PROOF
#     A generic field added to a domain dataclass "just for the new vertical" names nothing
#     and is invisible to any static rule. Reviewing the diff is still the reviewer's job;
#     this test removes the failure mode where nobody notices at all.
#
# WHY THE PACKAGE NAME ITSELF IS NOT IN THE VOCABULARY
#     `delivery` is an ordinary English word and the core already uses it in an unrelated
#     sense: `MediaPolicy.delivery` is a field, and domain/profile.py reads the key
#     `"delivery"` out of a mapping. Matching the bare name would paint a correct tree red
#     on day one, and a test that is red on a correct tree gets deleted rather than heeded.
#     The bare name is still matched inside a dotted or slashed path (`tools.delivery`,
#     `tools/delivery`), which is the only way a core file can actually reach the package.
#     Tool names and profile ids are compound and distinctive, so they are matched whole.
#
# CODE, NOT PROSE
#     Docstrings are excluded. ports/audit_sink.py says "for the fraud vertical this is
#     EVIDENCE" and that is documentation doing its job, not a dependency. An identifier,
#     an attribute, a keyword, an import or a live string literal can create one.
# ---------------------------------------------------------------------------------------

CORE_LAYERS = tuple(package.rsplit("/", 1)[-1] for package in CORE_PACKAGES)
TOOLS_PACKAGE_PARTS = ("adapters", "driven", "tools")
VERTICALS_MODULE = ".".join(("agent_core", *TOOLS_PACKAGE_PARTS))


@dataclass(frozen=True)
class _Vertical:
    """One vertical as the tree describes it: a tools package and the profiles selecting it."""

    name: str
    package: Path
    profiles: tuple[Path, ...]
    symbols: frozenset[str]
    profile_ids: frozenset[str]

    @property
    def module(self) -> str:
        return f"{VERTICALS_MODULE}.{self.name}"

    @property
    def vocabulary(self) -> frozenset[str]:
        """Tokens that name THIS vertical and nothing else."""
        return self.symbols | self.profile_ids

    @property
    def path_fragments(self) -> tuple[str, ...]:
        return (f"tools.{self.name}", f"tools/{self.name}")


def _public_module_level_names(tree: ast.Module) -> Iterator[str]:
    """Public names a module defines at module level - a vertical's tools are these.

    Module level only, and deliberately: a nested helper is not part of what the package
    exports, and the question here is what a core file could possibly be naming.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not node.name.startswith("_"):
                yield node.name
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    yield target.id
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                yield node.target.id


def _docstring_constants(tree: ast.Module) -> set[int]:
    """`id()` of every Constant node that is a docstring, so the scan can skip prose."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _code_tokens(tree: ast.Module) -> Iterator[tuple[int, str, bool]]:
    """Yield (line, token, is_text) for every name and live string literal in a module.

    `is_text` separates a string literal from an identifier: only a literal is searched for
    a path fragment, because `tools/delivery` cannot be an identifier.
    """
    docstrings = _docstring_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id, False
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr, False
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg, False
        elif isinstance(node, ast.keyword):
            if node.arg is not None:
                yield node.value.lineno, node.arg, False
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.lineno, node.name, False
        elif isinstance(node, ast.alias):
            if node.asname is not None:
                yield node.lineno, node.asname, False
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings:
                yield node.lineno, node.value, True


def _verticals(package_root: Path, profiles_dir: Path) -> tuple[_Vertical, ...]:
    """Every vertical the tree contains, discovered without a human naming one.

    A vertical package is a directory under adapters/driven/tools/ carrying an
    `__init__.py`. `tools/fraud/` is docstring-only today and is still a vertical here:
    it has no symbols to match, but a core file importing it is caught all the same.
    """
    profiles_by_toolset: dict[str, list[tuple[Path, str]]] = {}
    for path in sorted(profiles_dir.glob("*.yaml")):
        profile = load_profile_sync(path)
        for toolset in profile.toolsets:
            profiles_by_toolset.setdefault(toolset, []).append((path, profile.id))

    tools_root = package_root.joinpath(*TOOLS_PACKAGE_PARTS)
    verticals: list[_Vertical] = []
    for package in sorted(p for p in tools_root.iterdir() if (p / "__init__.py").is_file()):
        symbols: set[str] = set()
        for module_path in sorted(package.rglob("*.py")):
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            symbols.update(_public_module_level_names(tree))
        selected = profiles_by_toolset.get(package.name, [])
        verticals.append(
            _Vertical(
                name=package.name,
                package=package,
                profiles=tuple(path for path, _ in selected),
                symbols=frozenset(symbols),
                profile_ids=frozenset(profile_id for _, profile_id in selected),
            )
        )
    return tuple(verticals)


def _vertical_references(
    package_root: Path = PACKAGE_ROOT, profiles_dir: Path = PROFILES_ROOT
) -> list[str]:
    """Every place a core module reaches for a vertical. Empty means the contract holds."""
    verticals = _verticals(package_root, profiles_dir)
    owner: dict[str, str] = {}
    for vertical in verticals:
        for token in sorted(vertical.vocabulary):
            owner.setdefault(token, vertical.name)

    offences: list[str] = []
    for layer in CORE_LAYERS:
        root = package_root / layer
        modules = sorted(root.rglob("*.py"))
        assert modules, f"no modules found under {root} - the walker is pointed at nothing"

        for module_path in modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            package = _package_parts(module_path, package_root)
            location = module_path.relative_to(package_root.parents[1]).as_posix()

            for lineno, imported in _iter_imports(tree, package):
                for vertical in verticals:
                    if imported == vertical.module or imported.startswith(f"{vertical.module}."):
                        offences.append(
                            f"{location}:{lineno} imports {imported!r} "
                            f"- the {vertical.name!r} vertical"
                        )

            for lineno, token, is_text in _code_tokens(tree):
                name = owner.get(token)
                if name is not None:
                    offences.append(f"{location}:{lineno} names {token!r} - the {name!r} vertical")
                if not is_text:
                    continue
                for vertical in verticals:
                    if any(fragment in token for fragment in vertical.path_fragments):
                        offences.append(
                            f"{location}:{lineno} spells out {token!r} "
                            f"- the {vertical.name!r} vertical"
                        )
    return offences


@pytest.mark.contract
def test_adding_a_vertical_does_not_touch_the_core() -> None:
    """No module under domain/, application/ or ports/ names any vertical.

    This is the sentence at the top of CLAUDE.md, mechanised. If it fails, the port cut is
    wrong: the core has grown a dependency on one specific agent, and every later vertical
    will now cost a change to a shared layer. Fix the design. Do NOT add the offending name
    to an allowlist, and do not narrow the layers scanned.
    """
    verticals = _verticals(PACKAGE_ROOT, PROFILES_ROOT)
    assert verticals, (
        f"no vertical package found under {PACKAGE_ROOT.joinpath(*TOOLS_PACKAGE_PARTS)} - "
        "the walker is pointed at nothing and would pass on an empty tree"
    )
    # Without this, a rename of the tools directory or of the profiles directory would empty
    # the vocabulary and turn every assertion below into a tautology - the exact failure the
    # t-f10-01 note describes.
    assert any(vertical.symbols and vertical.profile_ids for vertical in verticals), (
        "no vertical contributed both a tool name and a profile id, so the scan has nothing "
        "to look for. Verticals discovered: "
        + ", ".join(
            f"{v.name} ({len(v.symbols)} symbols, {len(v.profile_ids)} profiles)"
            for v in verticals
        )
    )

    offences = _vertical_references(PACKAGE_ROOT, PROFILES_ROOT)

    assert not offences, (
        "adding a vertical is ONE profile file plus ONE tools package, and zero changes to "
        "domain/, application/ or ports/. The core reaches for a vertical here:\n  "
        + "\n  ".join(offences)
    )


def _wrong_arrangement(tmp_path: Path) -> tuple[Path, Path]:
    """A tree where a vertical leaked into the core, built to prove the check can go red."""
    package_root = tmp_path / "src" / "agent_core"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True)
    vertical = package_root.joinpath(*TOOLS_PACKAGE_PARTS, "acme")
    vertical.mkdir(parents=True)
    (vertical / "__init__.py").write_text("", encoding="utf-8")
    (vertical / "tools.py").write_text(
        "def acme_settle(order_id: str) -> str:\n    return order_id\n", encoding="utf-8"
    )
    (profiles_dir / "acme_agent.yaml").write_text(
        'id: acme_agent\npersona: "wrong on purpose"\nmodel: claude-sonnet-5\n'
        "toolsets:\n  - acme\n",
        encoding="utf-8",
    )
    for layer in CORE_LAYERS:
        (package_root / layer).mkdir(parents=True)
        (package_root / layer / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "domain" / "leaked.py").write_text(
        '"""A docstring naming acme_settle must NOT count - prose is not a dependency."""\n'
        "\n"
        "from agent_core.adapters.driven.tools.acme import tools\n"
        "\n"
        "NEEDS_APPROVAL = ('acme_settle',)\n"
        "OWNER = 'acme_agent'\n"
        "PACKAGE = 'agent_core/adapters/driven/tools/acme'\n"
        "\n"
        "__all__ = ['NEEDS_APPROVAL', 'OWNER', 'PACKAGE', 'tools']\n",
        encoding="utf-8",
    )
    return package_root, profiles_dir


@pytest.mark.contract
def test_the_contract_check_goes_red_when_a_vertical_leaks_into_the_core(tmp_path: Path) -> None:
    """The falsifiability lock. A guard that cannot fail is documentation with a green tick.

    Four leaks, one per route a vertical can reach the core by - an import, a tool name, a
    profile id and a package path - against a tree built here rather than by damaging the
    real one. The docstring in that same module names a tool too, and must not be reported:
    if prose counted, the check would be red on the real tree for `ports/audit_sink.py`.
    """
    package_root, profiles_dir = _wrong_arrangement(tmp_path)

    offences = _vertical_references(package_root, profiles_dir)

    reported = "\n".join(offences)
    assert "imports" in reported, f"the import route was not caught:\n{reported}"
    assert "acme_settle" in reported, f"the tool-name route was not caught:\n{reported}"
    assert "acme_agent" in reported, f"the profile-id route was not caught:\n{reported}"
    assert "tools/acme" in reported, f"the package-path route was not caught:\n{reported}"
    assert all("leaked.py:1" not in offence for offence in offences), (
        f"a docstring was reported as a dependency:\n{reported}"
    )


@pytest.mark.contract
def test_a_profile_selects_exactly_one_tools_package() -> None:
    """The other half of the sentence: ONE profile file plus ONE tools package.

    A profile naming two toolsets is two verticals fused into one agent, and the cost of
    adding a vertical is no longer what CLAUDE.md says it is. If that is genuinely wanted,
    it is a change to the contract at the top of CLAUDE.md - a decision, not a test to relax.
    """
    packages = {vertical.name for vertical in _verticals(PACKAGE_ROOT, PROFILES_ROOT)}
    profiles = sorted(PROFILES_ROOT.glob("*.yaml"))
    assert profiles, f"no profile found under {PROFILES_ROOT} - the walker is pointed at nothing"

    for path in profiles:
        profile = load_profile_sync(path)
        assert len(profile.toolsets) == 1, (
            f"{path.name} selects {list(profile.toolsets)}; a vertical is one tools package"
        )
        assert profile.toolsets[0] in packages, (
            f"{path.name} selects the toolset {profile.toolsets[0]!r}, and no package "
            f"answers to it. Known packages: {sorted(packages)}"
        )


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
