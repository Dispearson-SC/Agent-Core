"""Startup applies the schema. Every migration in the tree, not a hand-kept subset.

Phase:   F1 / F3
Tasks:   docs/TASKS.md#t-f1-23, docs/TASKS.md#t-f3-17
Covers:  agent_core/composition.py (start_container, bind_turn_workflow),
         adapters/driven/persistence_pg/migrations.py (discovery + apply_all_migrations)

WHY THIS FILE EXISTS
    Nine `Migration` objects live outside `APP_MIGRATIONS`, each in the sibling module that
    owns its table - the convention that keeps `migrations.py` from being a shared write.
    Nothing in production applied any of them. Every integration test applied its own by
    hand, so the suite was green and a fresh deployment had no schema. A per-test applier
    is a test double for a startup step nobody wrote; see the `t-f1-23` note in
    docs/TASKS.md for why that is the third instance of this exact shape.

THE STATIC TEST IS THE ONE THAT KEEPS THIS CLOSED
    `test_every_migration_defined_in_the_tree_is_discovered` reads the SOURCE with a
    regular expression and compares it against what `discover_app_migrations()` finds by
    IMPORTING. The two must never be derived from each other: a hand-maintained list in
    `composition.py` would be the original defect with an extra step, and a test that
    consulted the same list could not notice. Because the source scan is independent, an
    eighth sibling module that discovery cannot reach turns this red without a database
    being anywhere near it.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driving.workflow import turn_workflow

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DATABASE = "agent_core_startup_migrations_test"

# `agent_core/` - three parents up from Core/tests/integration/this file is `Core/`, and
# the package sits under `Core/src/`.
_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_core"

# `Migration(` followed by its `id=` keyword. Deliberately textual: this must find a
# migration written in a module nobody wired up, which is the only failure mode worth
# guarding here.
_MIGRATION_IN_SOURCE = re.compile(r"Migration\(\s*id=\"([^\"]+)\"")

_CREATES_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_CREATES_VIEW = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo() -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{_APP_DATABASE}\1", _ADMIN_CONNINFO)


def _recreate_empty_database() -> str:
    """A database with nothing in it - not even `schema_migrations`.

    Dropped and recreated rather than truncated, because the claim under test is what a
    FRESH DEPLOYMENT gets. A leftover `schema_migrations` row from an earlier run would
    make the applier skip the very migration this asserts it applies.
    """
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_APP_DATABASE}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_APP_DATABASE}"')
    return _app_conninfo()


def _discover() -> tuple[Any, ...]:
    """`migrations.discover_app_migrations()`, with its absence stated as a claim.

    Resolved by name rather than imported at module scope so that a missing production
    function fails on THIS assertion - naming the anchor - instead of collecting as an
    ImportError, which says nothing about what is wrong.
    """
    discover = getattr(migrations, "discover_app_migrations", None)
    assert callable(discover), (
        "migrations.py exposes no discovery of the Migration objects defined in its "
        "sibling modules, so any applier must be handed a hand-maintained list - which "
        "is the defect in docs/TASKS.md#t-f1-23 with an extra step."
    )
    found: tuple[Any, ...] = discover()
    return found


def _start(settings: composition.Settings) -> composition.Container:
    """The production startup path, with its absence stated as a claim."""
    start_container = getattr(composition, "start_container", None)
    assert callable(start_container), (
        "composition.py exposes no startup path that applies migrations: it calls "
        "neither run_migrations nor any apply_*_migration, so a fresh deployment has no "
        "schema (docs/TASKS.md#t-f1-23)."
    )
    container: composition.Container = asyncio.run(start_container(settings))
    return container


def _migration_ids_in_the_source() -> set[str]:
    ids: set[str] = set()
    for path in _SOURCE_ROOT.rglob("*.py"):
        ids.update(_MIGRATION_IN_SOURCE.findall(path.read_text(encoding="utf-8")))
    return ids


def _relations_the_schema_must_hold(discovered: tuple[Any, ...]) -> set[str]:
    """Every table and view the migration set claims to create.

    Derived from the SQL rather than listed here on purpose: a hand-written expectation
    would have to be updated by the same person who forgot to wire the migration up.
    """
    relations: set[str] = set()
    for migration in discovered:
        relations.update(_CREATES_TABLE.findall(migration.sql))
        relations.update(_CREATES_VIEW.findall(migration.sql))
    return relations


def test_every_migration_defined_in_the_tree_is_discovered() -> None:
    """No database required, and that is the point - this is the forget-proofing.

    The source scan and the import-based discovery are two independent answers to "which
    migrations exist". When an eighth sibling module lands somewhere discovery cannot
    reach, they disagree and this goes red before anyone deploys.
    """
    discovered = {migration.id for migration in _discover()}
    in_source = _migration_ids_in_the_source()

    assert in_source, "the source scan found no migrations at all - the regex has drifted"
    assert in_source - discovered == set(), (
        "these migrations are defined in the tree and nothing applies them: "
        f"{sorted(in_source - discovered)}. A Migration must live in a module "
        "discover_app_migrations() reaches, or production will never run it."
    )
    assert discovered - in_source == set(), (
        f"discovery reports migrations the source does not define: {sorted(discovered - in_source)}"
    )


def test_bind_turn_workflow_hands_the_workflow_the_container_s_human_gateway() -> None:
    """t-f3-17. No database required: `build_container` connects to nothing.

    `_step_publish` resolves the gateway off the bound dependencies, so an unbound seat is
    a turn that suspends on a human and raises `TurnWorkflowNotWiredError` at the last
    moment. The SAME instance the container hands `DecideApproval` must be the one bound
    here: the turn that ASKS and the route that ANSWERS share one correlation table.
    """
    container = composition.build_container(composition.Settings())

    bound = turn_workflow._dependencies
    assert bound is not None, "bind_turn_workflow never called bind_dependencies()"
    assert bound.human_gateway is container.human_gateway, (
        "bind_turn_workflow builds TurnWorkflowDependencies without human_gateway, so "
        "every turn that suspends on a human dies in _step_publish "
        "(docs/TASKS.md#t-f3-17)."
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_startup_leaves_every_table_the_system_needs_in_place() -> None:
    """Against an EMPTY database, starting the system creates the whole schema.

    Including the tables that live in sibling migration modules, which no production code
    path applied before this anchor.
    """
    conninfo = _recreate_empty_database()
    discovered = _discover()

    _start(composition.Settings(app_conninfo=conninfo))

    expected_relations = _relations_the_schema_must_hold(discovered)
    with psycopg.connect(conninfo) as conn:
        missing = [
            name
            for name in sorted(expected_relations)
            if conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()[0]  # type: ignore[index]
            is None
        ]
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}

    assert missing == [], f"startup left these relations uncreated: {missing}"
    assert applied == {migration.id for migration in discovered}, (
        "startup applied a different set of migrations than the tree defines: missing "
        f"{sorted({m.id for m in discovered} - applied)}"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_starting_twice_against_the_same_database_is_a_no_op() -> None:
    """Forward-only means a second start applies nothing and raises nothing.

    A process restart is the ordinary case, not the exceptional one.
    """
    conninfo = _recreate_empty_database()
    settings = composition.Settings(app_conninfo=conninfo)

    _start(settings)
    with psycopg.connect(conninfo) as conn:
        first = [
            row[0]
            for row in conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
        ]

    _start(settings)  # must not raise, must not re-apply
    with psycopg.connect(conninfo) as conn:
        second = [
            row[0]
            for row in conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
        ]

    assert len(second) == len(set(second)), "a migration id was recorded twice"
    assert second == first, "the second start changed the applied set"
