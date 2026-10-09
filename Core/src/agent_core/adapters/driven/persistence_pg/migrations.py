"""Schema migrations.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-16, docs/TASKS.md#t-f1-23

TWO LOGICAL DATABASES ON ONE INSTANCE (day 1)
    app   - turns, messages, checkpoints, policy_rules, audit_*
    dbos  - DBOS's own workflow and step state; it owns this, we never touch it

Day 2 adds a THIRD database on the SAME instance for LiteLLM. A second Postgres instance
is never required by the design - splitting instances is an operations decision about
blast radius and backups, worth taking when an incident justifies it.

RULE
    Migrations are forward-only and never destructive on audit tables. An audit table that
    can be dropped by a migration is not an audit table.

WHERE THE REST OF THE MIGRATIONS LIVE, AND WHY NOBODY LISTS THEM (t-f1-23)
    `APP_MIGRATIONS` below is not the whole schema and was never meant to be. Nine more
    `Migration` objects live in the sibling module that owns each table - the convention
    `docs/TASKS.md` established so this file would never be a write two anchors share, and
    so a wave scheduler would not have to serialise every anchor that creates a table.

    The cost of that convention was that nothing applied them. `composition.py` called
    neither `run_migrations` nor any `apply_*_migration`, every integration test applied
    its own by hand, and a fresh deployment therefore had no schema while the suite stayed
    green - see the `t-f1-23` note in docs/TASKS.md.

    `discover_app_migrations()` closes it by IMPORTING every module of this package and
    collecting the `Migration` objects it finds. A hand-maintained tuple would be the same
    defect with an extra step: the next agent adds the module and forgets the line, and
    nothing anywhere says so. Discovery has no line to forget.

    ORDER IS THE MIGRATION ID, ALWAYS. Ids are zero-padded and pre-allocated in
    docs/TASKS.md, so sorting them lexicographically is sorting them by allocation, which
    is the order the dependencies were designed in: `0012` alters the table `0004`
    created, `0016`'s views name the tables `0010` and `0014` created. Nothing here reads
    module order, file order, or import order - all three vary by platform.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from dataclasses import dataclass
from types import ModuleType

import psycopg


@dataclass(frozen=True)
class Migration:
    """One forward-only, idempotent unit of schema change against the app database."""

    id: str
    sql: str


# Forward-only. Idempotent by construction (IF NOT EXISTS) and tracked in
# schema_migrations so re-running the set never re-applies one. Never add a DROP or
# ALTER against an audit_* table here - see the module RULE above.
APP_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="0001_turns",
        sql="""
        CREATE TABLE IF NOT EXISTS turns (
            turn_id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0002_messages",
        sql="""
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            role TEXT NOT NULL,
            content JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_messages_session_seq ON messages (session_id, seq);
        """,
    ),
    Migration(
        id="0003_checkpoints",
        sql="""
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            covers_through_message BIGINT NOT NULL,
            supersedes UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0004_policy_rules",
        sql="""
        CREATE TABLE IF NOT EXISTS policy_rules (
            rule_id TEXT PRIMARY KEY,
            definition JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0005_audit_tool_calls",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_tool_calls (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            caller TEXT NOT NULL,
            tool TEXT NOT NULL,
            arguments JSONB NOT NULL,
            effect TEXT NOT NULL,
            rule_id TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0006_audit_human_decisions",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_human_decisions (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            tool_call_id TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            approved BOOLEAN NOT NULL,
            note TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0007_audit_media",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_media (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            media_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            direction TEXT NOT NULL,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        id="0008_audit_turn_costs",
        sql="""
        CREATE TABLE IF NOT EXISTS audit_turn_costs (
            id BIGSERIAL PRIMARY KEY,
            turn_id UUID NOT NULL,
            input_tokens BIGINT NOT NULL,
            output_tokens BIGINT NOT NULL,
            cached BOOLEAN NOT NULL,
            cost_usd NUMERIC NOT NULL,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
)

_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _ensure_database_sync(admin_conninfo: str, database: str) -> None:
    """Create one logical database if it does not already exist. Idempotent.

    Runs autocommit: CREATE DATABASE cannot run inside a transaction block.
    """
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if exists is None:
            conn.execute(f'CREATE DATABASE "{database}"')


# The suffix DBOS appends to the APP database name to get its system database, verified
# against the installed dbos 2.31.1: `_schemas/system_database.py::SystemSchema
# .sysdb_suffix`, applied in `_dbos_config.py` to whatever `database_url` it is handed.
#
# It is a literal here rather than an import because `dbos` is confined to
# adapters/driving/workflow/ (CLAUDE.md). A literal copy of somebody else's constant is a
# thing that can drift, so it is not left to a comment: test_schema_truths.py asks DBOS
# ITSELF what it will open - through `composition.dbos_config`, the config production
# hands the runtime - and fails if this string stops agreeing with it.
DBOS_SYSTEM_DATABASE_SUFFIX = "_dbos_sys"


def dbos_system_database(app_database: str) -> str:
    """The database DBOS opens, derived the way DBOS derives it. t-f11-20."""
    return f"{app_database}{DBOS_SYSTEM_DATABASE_SUFFIX}"


async def ensure_databases(
    admin_conninfo: str,
    *,
    app_database: str,
    dbos_database: str | None = None,
) -> None:
    """Create the app database and the one DBOS opens - two separate calls, never one
    statement naming both.

    WHICH SIDE MOVED, AND WHY (t-f11-20)
        THE BOOTSTRAP MOVED. This used to create `<app>_dbos`, a name chosen here by
        repo-wide convention. DBOS never opens it: it derives its system database from
        the app URL `composition.dbos_config` hands it, by appending `_dbos_sys`, and
        opens `<app>_dbos_sys`. So the bootstrap made an empty database nobody would ever
        connect to, while the database the runtime actually uses was created by nobody
        who meant to.

        The runtime's name won, and not by seniority. The name is DBOS's: `dbos migrate`,
        Conductor and the recovery path all derive it from the same app URL, so choosing
        our own would have meant every dbos-native tool pointing at a different database
        from the one the process uses. Our convention had exactly one consumer - this
        function - and no authority at all.

    WHY THIS CREATES IT AT ALL, GIVEN DBOS CREATES IT ITSELF
        DBOS's own creation is best effort and runs as the APPLICATION role: it connects
        to `postgres` with the app credentials and, on any failure, logs a warning and
        carries on to fail later in migration (`_sys_db_postgres.py::run_migrations`,
        verified on 2.31.1). An app role that may not CREATE DATABASE - which is the
        deployment `composition.start_container` is written for - therefore gets a
        warning and a broken start. This is the opt-in admin path, and creating it here
        is idempotent and harmless when DBOS got there first.

    `dbos_database` IS ACCEPTED AND IGNORED, and it exists only so the callers written
    against the old convention keep working until they are updated to stop passing it;
    the name is derived, because a caller that can choose it is a caller that can choose
    one nothing opens - which is the defect this closes. Delete the parameter with its
    last caller.

    dbos owns the database's contents; this only makes sure it exists, it never migrates
    its schema. Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_ensure_database_sync, admin_conninfo, app_database)
    await asyncio.to_thread(
        _ensure_database_sync, admin_conninfo, dbos_system_database(app_database)
    )


async def run_migrations(app_conninfo: str) -> None:
    """DEPRECATED NAME for `apply_all_migrations`. Same database, same whole schema.

    WHAT THIS USED TO DO, AND WHY IT COULD NOT STAY (t-f11-22)
        It applied `APP_MIGRATIONS` and nothing else - the F1 set, eight of the twenty-odd
        `Migration` objects this package defines. A database built with it therefore had
        `policy_rules` without `0012`'s `tenant_id`, so EVERY policy SELECT raised
        `UndefinedColumn` and the adapter reported the store unreachable; it had
        `audit_tool_calls` without `0022`'s `reason`, so the sink's only INSERT failed the
        same way. Both are the real production statements against a database a test called
        migrated.

        A SECOND, PARTIAL APPLIER THAT LOOKS LIKE THE REAL ONE IS A TRAP. It was not
        load-bearing anywhere - production has always started through
        `apply_all_migrations` - but twenty test fixtures reach for it by name, and "it is
        only used by tests" is what the trap says right before a fixture is one query away
        from asserting against a broken schema, which `test_policy_tenant_sql.py` already
        was.

        So the partial applier is GONE and the name is not: deleting the name would have
        been a twenty-file edit whose only effect is the word, while leaving two appliers
        that differ is the defect itself. There is one applier now, and every caller of
        either name gets the whole schema.

    Safe to call repeatedly, and safe to call before a module's own `apply_*_migration`:
    an already-applied id is skipped and every migration's SQL is idempotent besides.
    """
    await apply_all_migrations(app_conninfo)


class DuplicateMigrationIdError(RuntimeError):
    """Two different migrations were allocated the same NUMBER.

    A silent double-allocation is the failure docs/TASKS.md's pre-allocated id table
    exists to prevent, and it is unrecoverable once one of the two has run anywhere:
    `schema_migrations` records the id, so the other one is skipped on every database that
    already saw the first and never runs again. Loud at startup, before either applies.

    THE NUMBER IS THE ALLOCATION; THE SUFFIX IS A COMMENT - t-f11-30. This check compared
    whole ids, one character narrower than the thing it guards, so `0023_audit_tool_calls_tenant`
    and `0023_anything_else` coexisted silently - which is exactly what happened:
    `audit_tenant_migration.py` took `0023` outside the table at the same moment the table
    allocated it, and nothing said so.

    Whole-id comparison also makes the failure WORSE than not checking, because it fails
    per-database rather than everywhere. `_apply_all_migrations_sync` skips an id already
    in `schema_migrations`, so both run on a fresh database and look fine; on a database
    that recorded one of them before the other existed, the second never runs and its
    table never appears. The bug is then a schema that differs by deployment age.
    """


def _allocation_number(migration_id: str) -> str:
    """The leading digits of an id - `0023` from `0023_audit_tool_calls_tenant`.

    The whole id when there are no leading digits, so a non-numeric id collides only with
    itself rather than with every other non-numeric one. Nothing in the tree is shaped
    that way today; the fallback exists so that the day something is, this check reports a
    real duplicate instead of inventing one.
    """
    digits = len(migration_id) - len(migration_id.lstrip("0123456789"))
    return migration_id[:digits] if digits else migration_id


def _iter_package_modules() -> list[tuple[str, ModuleType]]:
    """Every module of this package, imported, in a platform-independent order.

    `__name__.rpartition` rather than a literal package path: the package moves with the
    file, and a hard-coded dotted name would keep resolving to the old location for
    exactly as long as it took someone to notice the schema had shrunk.
    """
    package_name = __name__.rpartition(".")[0]
    package = importlib.import_module(package_name)
    names = sorted(
        info.name for info in pkgutil.iter_modules(list(package.__path__)) if not info.ispkg
    )
    return [(name, importlib.import_module(f"{package_name}.{name}")) for name in names]


def _migrations_in(module: ModuleType) -> list[Migration]:
    """Every `Migration` reachable from a module's top level, bare or inside a sequence.

    The sequence case is `APP_MIGRATIONS`; the bare case is every sibling module. Both
    shapes are collected so the convention does not have to be restated by whoever writes
    the next one.
    """
    found: list[Migration] = []
    for value in vars(module).values():
        if isinstance(value, Migration):
            found.append(value)
        elif isinstance(value, (tuple, list)):
            found.extend(item for item in value if isinstance(item, Migration))
    return found


def discover_app_migrations() -> tuple[Migration, ...]:
    """Every migration this package defines, in id order. No list to keep up to date.

    Importing is what makes it forget-proof: a `Migration` written in a new sibling module
    is applied by production the moment the module exists, with no registration step to
    skip. That is the opposite of `adapters/driven/tools/provider.py`'s explicit registry
    and deliberately so - there the list IS the security property, because discovery would
    let a dropped-in file hand an agent a tool. A migration cannot widen what an agent may
    do; the risk here runs the other way, and it is a table that never gets created.

    A module that imports another's `Migration` object is not a second allocation - the
    same object is deduplicated. Two DIFFERENT migrations sharing an allocation NUMBER is
    `DuplicateMigrationIdError`, raised here rather than discovered as a table that
    silently never appeared. Keyed on the number and not the whole id, for the reason on
    that exception (t-f11-30).
    """
    by_number: dict[str, tuple[str, Migration]] = {}
    for module_name, module in _iter_package_modules():
        for migration in _migrations_in(module):
            number = _allocation_number(migration.id)
            previous = by_number.get(number)
            if previous is None:
                by_number[number] = (module_name, migration)
                continue
            if previous[1] != migration:
                raise DuplicateMigrationIdError(
                    f"migration number {number!r} is allocated twice: "
                    f"{previous[1].id!r} in {previous[0]}.py and {migration.id!r} in "
                    f"{module_name}.py. The number is the allocation and the suffix is a "
                    "comment, so these are the same slot. Numbers are pre-allocated in "
                    "docs/TASKS.md; take a new one rather than reusing this."
                )
    return tuple(
        migration for _, migration in sorted(by_number.values(), key=lambda pair: pair[1].id)
    )


def _apply_all_migrations_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        conn.execute(_TRACKING_TABLE_SQL)
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}
        for migration in discover_app_migrations():
            if migration.id in applied:
                continue
            conn.execute(migration.sql)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (migration.id,))


async def apply_all_migrations(app_conninfo: str) -> None:
    """Bring the app database up to the whole schema - this is what production calls.

    Every `Migration` in this package, `APP_MIGRATIONS` and sibling modules alike, applied
    in id order and tracked in `schema_migrations`. Safe on a fresh database and safe on a
    running one: an already-applied id is skipped and every migration's SQL is idempotent
    besides, so a restart applies nothing.

    Applied through the `Migration` objects rather than by calling each module's own
    `apply_*_migration`, because ordering is a property of the SET and the per-module
    functions each know only their own prerequisites. One ordered pass over one connection
    cannot get the order wrong; nine independent calls in whatever order someone typed
    them can, and the failure is a `CREATE VIEW` against a table that does not exist yet.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_all_migrations_sync, app_conninfo)
