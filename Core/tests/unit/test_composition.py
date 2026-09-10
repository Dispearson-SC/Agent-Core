"""The composition root, and the layer rule it exists to make checkable.

Phase:   F0
Tasks:   docs/TASKS.md#t-f0-02

TWO THINGS ARE PROVED HERE, AND THEY ARE THE SAME THING SEEN FROM BOTH SIDES.

    1. `AuditSink` writes through a pool object that is NOT the domain pool.
       CLAUDE.md non-negotiable #6. Sharing the pool is the easy wiring mistake and it
       erases the evidence of exactly the turns you most need to explain, silently.
       The check is behavioural, not a field comparison: a recording pool is injected,
       one audit row is appended, and the assertion is that the AUDIT pool was checked
       out and the domain pool was not touched. A container could hold two distinct pool
       objects and still hand the sink the wrong one.

    2. No module under `domain/`, `application/` or `ports/` imports a concrete adapter,
       and `composition.py` does. The second half is what makes the first half mean
       something: "the imports live in one file" is only true if that file is actually
       the one importing them.

Both walk the source with `ast` rather than importing and inspecting `sys.modules`.
A static walk sees an import that a conditional or a `TYPE_CHECKING` guard hides at
runtime, and this is a rule about what the source is allowed to say.

No Postgres, no network, no model. `build_container` must be callable on a laptop with
nothing running - a wiring bug that only surfaces when a database is up is a wiring bug
nobody finds until deploy day.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import agent_core
from agent_core.domain.turn import TurnId, Usage

PACKAGE_ROOT = Path(agent_core.__file__).resolve().parent
COMPOSITION = PACKAGE_ROOT / "composition.py"
PURE_LAYERS = ("domain", "application", "ports")

# The layer rule as ruff cannot state it: ruff's banned-api catches the runtime libraries
# (pydantic_ai, dbos, litellm, fastapi) but has nothing to say about our own adapters
# package, which is the import that actually punches through the hexagon.
ADAPTER_PACKAGE = "agent_core.adapters"


def _composition() -> ModuleType:
    """The composition module, with a readable assertion when the entry point is missing.

    Deliberately not a top-level `from agent_core.composition import build_container`:
    that turns "not implemented yet" into a collection error, and a collection error is
    not a red test - it proves the file failed to load, not that the behaviour is absent.
    """
    module = importlib.import_module("agent_core.composition")
    assert hasattr(module, "build_container"), (
        "agent_core.composition.build_container does not exist. The composition root is "
        "the ONE place concrete adapters are chosen and wired (docs/TASKS.md#t-f0-02)."
    )
    return module


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module name `path` imports, relative imports resolved.

    A relative import is resolved against the module's own package so that
    `from ..adapters.driven.x import Y` inside `application/` is seen for what it is.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(("agent_core", *path.relative_to(PACKAGE_ROOT).parts[:-1]))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
                continue
            base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
            found.add(".".join((*base, node.module) if node.module else tuple(base)))

    return found


def _pure_layer_sources() -> list[Path]:
    return sorted(
        path
        for layer in PURE_LAYERS
        for path in (PACKAGE_ROOT / layer).rglob("*.py")
    )


class _RecordingConnection:
    """Stands in for a psycopg connection. Records the SQL, executes nothing."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append(sql)
        return self


class _RecordingPool:
    """Stands in for `psycopg_pool.ConnectionPool`, and counts its checkouts.

    Only `connection()` is needed: every Postgres adapter in the tree reaches its
    connection through that one method, which is precisely why a pool can be swapped for
    this without the adapters knowing.
    """

    def __init__(self, conninfo: str, **_: object) -> None:
        self.conninfo = conninfo
        self.checkouts = 0
        self.connections: list[_RecordingConnection] = []

    @contextmanager
    def connection(self) -> Iterator[_RecordingConnection]:
        self.checkouts += 1
        connection = _RecordingConnection()
        self.connections.append(connection)
        yield connection

    def close(self) -> None:
        return None


@pytest.mark.phase("F0")
def test_audit_sink_writes_through_a_pool_that_is_not_the_domain_pool() -> None:
    """CLAUDE.md non-negotiable #6, proved by which pool actually gets checked out."""
    composition = _composition()
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    assert container.audit_pool is not container.domain_pool, (
        "AuditSink shares the domain pool. A rolled-back turn then rolls back its own "
        "evidence - CLAUDE.md non-negotiable #6."
    )

    domain_pool = cast(_RecordingPool, container.domain_pool)
    audit_pool = cast(_RecordingPool, container.audit_pool)

    asyncio.run(
        container.audit.record_turn_end(TurnId("t-1"), Usage(), Decimal("0.01"))
    )

    assert audit_pool.checkouts == 1, "the audit write did not go through the audit pool"
    assert domain_pool.checkouts == 0, (
        "the audit write reached the domain pool, so it can join - and die with - the "
        "domain transaction"
    )


@pytest.mark.phase("F0")
def test_the_default_wiring_builds_with_nothing_running() -> None:
    """The production path, on a laptop with no Postgres and no profiles pre-loaded.

    The test above proves the pool binding through a stand-in; this one proves the same
    invariant holds for the real `ConnectionPool` objects the process actually gets, and
    that constructing them touches no network. Without it, `pool_factory` could be the
    only path anybody ever exercises.
    """
    composition = _composition()
    container = composition.build_container()

    try:
        assert container.audit_pool is not container.domain_pool
        # A profile with version 0 never passed a registry, and `append_request` refuses
        # to persist one (D20). The composition root is where the version is assigned.
        assert container.profiles, "no profile was loaded; every turn would 404"
        assert all(profile.version >= 1 for profile in container.profiles.values())
    finally:
        container.domain_pool.close()
        container.audit_pool.close()


@pytest.mark.phase("F1")
def test_the_container_carries_the_runner_and_still_refuses_a_half_built_start_turn() -> None:
    """The MISSING SEATS note in `composition.py`, in executable form.

    `AgentRunner` used to be one of four seats with no constructible adapter. t-f1-12
    landed, so the runner is wired like any other adapter - and it holds NO turn state,
    because `run` takes the `turn_id` per call and a container-level id would have been
    the same id for every turn in the process.

    `start_turn` is still absent, and the day it appears without `ToolProvider` (F1/F4),
    `ContextEngine` (F5) and `SkillRegistry` (F6) existing, this test is what says so.
    """
    composition = _composition()
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    assert isinstance(
        container.runner,
        importlib.import_module(
            "agent_core.adapters.driven.agent_pydantic.runner"
        ).PydanticAgentRunner,
    )
    # No turn state on a process-wide object. CLAUDE.md non-negotiable #2: the id belongs
    # to the call, so there is nothing here for one turn to leak into the next.
    assert not any("turn" in name for name in vars(container.runner)), (
        f"the container's runner is holding turn state: {sorted(vars(container.runner))}"
    )

    assert not hasattr(container, "start_turn"), (
        "start_turn is wired, but ToolProvider, ContextEngine and SkillRegistry still have "
        "no constructible adapter and PgConversationStore.append_outcome still raises. "
        "Either those seats are filled - update this test - or the container builds and "
        "dies on the first turn."
    )


@pytest.mark.phase("F0")
@pytest.mark.contract
def test_the_pure_layers_import_no_concrete_adapter() -> None:
    """`domain/`, `application/` and `ports/` never name the adapters package."""
    offenders = [
        f"{path.relative_to(PACKAGE_ROOT).as_posix()} imports {name}"
        for path in _pure_layer_sources()
        for name in sorted(_imported_modules(path))
        if name == ADAPTER_PACKAGE or name.startswith(f"{ADAPTER_PACKAGE}.")
    ]

    assert offenders == [], (
        "A pure layer reached for a concrete adapter; the hexagon has a hole:\n"
        + "\n".join(offenders)
    )


@pytest.mark.phase("F0")
@pytest.mark.contract
def test_composition_is_the_file_that_imports_the_concrete_adapters() -> None:
    """The other half of the rule above, and the half that can rot unnoticed.

    "Every adapter import lives in one file" is a claim about that file too. If
    `composition.py` imports no adapter, the pure layers are clean for the uninteresting
    reason that nothing is wired anywhere.
    """
    imported = sorted(
        name
        for name in _imported_modules(COMPOSITION)
        if name.startswith(f"{ADAPTER_PACKAGE}.")
    )

    assert imported, (
        "composition.py imports no concrete adapter. It is the ONE legitimate importer "
        "of adapters/ (docs/TASKS.md#t-f0-02); an empty one makes the layer rule vacuous."
    )
