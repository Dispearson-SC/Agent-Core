"""Install state: versioned migrations, `post_init` and the recorded version (Phase 2, A7).

Phase:   Phase 2 (addon system), roadmap 5.4 step 6
Tests:   Core/tests/unit/test_addon_install_plan.py, Core/tests/integration/test_addon_install.py
Implements: no port - startup wiring (D80).

WHAT RUNS, AND WHEN
    After Core's own schema is applied (`addons` and `addon_migrations` exist, migration 0044),
    `install_addons` brings every ENABLED addon's state up to its manifest, in activation order
    (dependencies first):

      1. every `<migrations dir>/*.sql` file not yet recorded for the addon runs, in file-name
         order, EACH IN ITS OWN TRANSACTION together with its record. A failure stops startup
         naming addon and file; the failed file is rolled back, the ones before it stay done, and
         the next start resumes at the failed one.
      2. then, only when the recorded version differs from the manifest's (the addon is new, or
         its version went up), `hooks.post_init` runs and the version is recorded in ONE
         transaction: the hook's database writes (through `context.connection`) and the new
         version commit together or not at all. A failing hook leaves the version unrecorded, so
         the next start runs it again: a hook must therefore be safe to repeat.

    The hook is `def post_init(context: AddonInstallContext) -> None`, synchronous, run once per
    new or upgraded addon. `context.previous_version` is `None` for a first install and the old
    version for an upgrade; that is the whole upgrade hook (D80).

WHO MAY RUN IT: ONE PROCESS AT A TIME
    The pass holds a session advisory lock on its connection and reads the recorded state only
    after taking it, so two processes starting together do each step once: the second waits, then
    finds everything recorded. Core's own migrations are idempotent SQL and take no lock; an
    addon's files need not be, which is why this does.

REFUSALS BEFORE ANYTHING RUNS
    A recorded version NEWER than the manifest's (a downgrade) stops startup for the whole pass
    before any file or hook runs: running old code over a newer schema is not something to try.
    A hook that cannot be found or imported fails while the plans are made, also before any
    database work.

WHAT IT NEVER DOES
    It never uninstalls. An addon removed from `AGENT_CORE_ADDONS` keeps its `addons` and
    `addon_migrations` rows and every table its migrations made (a returning addon continues
    where it stopped). Uninstalling is explicit and out of scope; `addons list` shows the rows.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

import psycopg
from packaging.version import InvalidVersion, Version

from agent_core.adapters.driven.addons_fs.errors import AddonInstallError
from agent_core.adapters.driven.addons_fs.loader import ResolvedAddon

__all__ = [
    "AddonInstallContext",
    "AddonInstallPlan",
    "InstallResult",
    "InstalledAddon",
    "MigrationFile",
    "install_addons",
    "list_installed",
    "plan_installs",
]

_LOG = logging.getLogger(__name__)
_MODULE_PREFIX: Final = "agent_core_addons"
_HOOK_FIELD: Final = "hooks.post_init"
# One key for the whole pass: addons install one process at a time, in activation order.
_LOCK_SQL: Final = "SELECT pg_advisory_lock(hashtextextended('agent_core.addons.install', 0))"


@dataclass(frozen=True)
class AddonInstallContext:
    """What `post_init` is handed. `connection` is inside the transaction that records the
    version: do not commit, close or begin transactions on it."""

    name: str
    version: str
    previous_version: str | None
    settings: Mapping[str, Any]
    connection: psycopg.Connection[Any]


PostInit = Callable[[AddonInstallContext], Any]


@dataclass(frozen=True)
class MigrationFile:
    name: str
    sql: str


@dataclass(frozen=True)
class AddonInstallPlan:
    """One addon's install steps, read from disk: nothing here has touched the database."""

    name: str
    version: str
    migrations: tuple[MigrationFile, ...]
    post_init: PostInit | None
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InstallResult:
    """What one addon needed on this start. `action` is what happened to its recorded version."""

    name: str
    action: Literal["installed", "upgraded", "unchanged"]
    previous_version: str | None
    applied: tuple[str, ...]


@dataclass(frozen=True)
class InstalledAddon:
    """One row of the `addons` table."""

    name: str
    version: str
    installed_at: datetime
    upgraded_at: datetime | None


def _fail(addon: str, field_name: str, reason: str) -> AddonInstallError:
    return AddonInstallError(addon, field_name, reason)


def _migration_files(addon: ResolvedAddon) -> tuple[MigrationFile, ...]:
    relative = addon.manifest.migrations
    if relative is None:
        return ()
    owner = addon.manifest.name
    files: list[MigrationFile] = []
    for path in sorted((addon.root / relative).glob("*.sql"), key=lambda item: item.name):
        try:
            files.append(MigrationFile(path.name, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as error:
            raise _fail(owner, "migrations", f"{path.name} cannot be read: {error}") from error
    return tuple(files)


def _import_hook(addon: ResolvedAddon, reference: str) -> PostInit:
    """`module.path:function` -> the function, from `<addon>/module/path.py`."""
    owner = addon.manifest.name
    module_path, _, function_name = reference.partition(":")
    relative = module_path.replace(".", "/") + ".py"
    path = addon.root / relative
    if not path.is_file():
        raise _fail(owner, _HOOK_FIELD, f"{reference!r} points at {relative}, which does not exist")
    module_name = f"{_MODULE_PREFIX}.{owner}.hook_modules.{module_path}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a .py file always has a loader
        raise _fail(owner, _HOOK_FIELD, f"{relative} cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        del sys.modules[module_name]
        raise _fail(
            owner, _HOOK_FIELD, f"{relative} failed to import: {type(error).__name__}: {error}"
        ) from error
    function = getattr(module, function_name, None)
    if not callable(function):
        raise _fail(owner, _HOOK_FIELD, f"{relative} has no function {function_name!r}")
    return function  # type: ignore[no-any-return]


def plan_installs(addons: Sequence[ResolvedAddon]) -> tuple[AddonInstallPlan, ...]:
    """Each addon's migration files and hook, in the order given (activation order).

    Raises `AddonInstallError` for a hook that is missing or does not import and for an
    unreadable file. Imports the hooks' modules (addon code) and reads the files; no database.
    """
    plans: list[AddonInstallPlan] = []
    for addon in addons:
        reference = addon.manifest.hooks.post_init
        plans.append(
            AddonInstallPlan(
                name=addon.manifest.name,
                version=addon.manifest.version,
                migrations=_migration_files(addon),
                post_init=_import_hook(addon, reference) if reference else None,
                settings=addon.settings,
            )
        )
    return tuple(plans)


def _version(addon: str, text: str, what: str) -> Version:
    try:
        return Version(text)
    except InvalidVersion:
        raise _fail(addon, "version", f"{what} {text!r} is not a version") from None


def _refuse_downgrades(plans: Sequence[AddonInstallPlan], recorded: Mapping[str, str]) -> None:
    for plan in plans:
        previous = recorded.get(plan.name)
        if previous is None:
            continue
        if _version(plan.name, previous, "the recorded version") > _version(
            plan.name, plan.version, "the manifest version"
        ):
            raise _fail(
                plan.name,
                "version",
                f"the database holds version {previous} but the addon on disk is {plan.version}: "
                "a downgrade is not supported; deploy the addon version that was installed, or "
                "remove the addon from the list and restore a backup taken before the upgrade",
            )


def _run_migrations(connection: psycopg.Connection[Any], plan: AddonInstallPlan) -> tuple[str, ...]:
    done = {
        row[0]
        for row in connection.execute(
            "SELECT file FROM addon_migrations WHERE addon = %s", (plan.name,)
        ).fetchall()
    }
    applied: list[str] = []
    for migration in plan.migrations:
        if migration.name in done:
            continue
        try:
            with connection.transaction():
                connection.execute(migration.sql)
                connection.execute(
                    "INSERT INTO addon_migrations (addon, file, version) VALUES (%s, %s, %s)",
                    (plan.name, migration.name, plan.version),
                )
        except psycopg.Error as error:
            raise _fail(
                plan.name,
                "migrations",
                f"{migration.name} failed and was rolled back ({type(error).__name__}: "
                f"{str(error).strip()}); {len(applied)} file(s) before it in this start "
                "stay applied. Fix the file and start again",
            ) from error
        applied.append(migration.name)
    return tuple(applied)


def _record(
    connection: psycopg.Connection[Any], plan: AddonInstallPlan, previous: str | None
) -> None:
    if previous is None:
        connection.execute(
            "INSERT INTO addons (name, version) VALUES (%s, %s)", (plan.name, plan.version)
        )
    else:
        connection.execute(
            "UPDATE addons SET version = %s, upgraded_at = now() WHERE name = %s",
            (plan.version, plan.name),
        )


def _install_one(
    connection: psycopg.Connection[Any], plan: AddonInstallPlan, previous: str | None
) -> InstallResult:
    applied = _run_migrations(connection, plan)
    if previous == plan.version:
        return InstallResult(plan.name, "unchanged", previous, applied)
    try:
        with connection.transaction():
            if plan.post_init is not None:
                plan.post_init(
                    AddonInstallContext(
                        plan.name, plan.version, previous, plan.settings, connection
                    )
                )
            _record(connection, plan, previous)
    except Exception as error:
        raise _fail(
            plan.name,
            _HOOK_FIELD,
            f"post_init for version {plan.version} failed and was rolled back "
            f"({type(error).__name__}: {error}); the version is not recorded, so it runs again "
            "on the next start",
        ) from error
    action: Literal["installed", "upgraded"] = "installed" if previous is None else "upgraded"
    return InstallResult(plan.name, action, previous, applied)


def _install_sync(conninfo: str, plans: Sequence[AddonInstallPlan]) -> tuple[InstallResult, ...]:
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(_LOCK_SQL)  # released when the connection closes
        recorded = {
            name: version
            for name, version in connection.execute("SELECT name, version FROM addons").fetchall()
        }
        _refuse_downgrades(plans, recorded)
        results = [_install_one(connection, plan, recorded.get(plan.name)) for plan in plans]
    for result in results:
        if result.action != "unchanged" or result.applied:
            _LOG.info(
                "addon_install: addon=%s action=%s previous=%s migrations=%s",
                result.name,
                result.action,
                result.previous_version,
                ",".join(result.applied) or "-",
            )
    return tuple(results)


async def install_addons(
    conninfo: str, addons: Sequence[ResolvedAddon]
) -> tuple[InstallResult, ...]:
    """Bring the install state of `addons` up to their manifests. See the module docstring.

    With no addons nothing is read or written, not even a connection opened: a deployment that
    enables none starts byte-identically to before. Postgres access is synchronous (D13); the
    blocking work runs in a thread.
    """
    if not addons:
        return ()
    plans = plan_installs(addons)
    return await asyncio.to_thread(_install_sync, conninfo, plans)


def _list_sync(conninfo: str) -> tuple[InstalledAddon, ...]:
    with psycopg.connect(conninfo) as connection:
        try:
            rows = connection.execute(
                "SELECT name, version, installed_at, upgraded_at FROM addons ORDER BY name"
            ).fetchall()
        except psycopg.errors.UndefinedTable:
            return ()  # the schema was never applied: nothing is installed
    return tuple(InstalledAddon(*row) for row in rows)


async def list_installed(conninfo: str) -> tuple[InstalledAddon, ...]:
    """The `addons` table, by name. Read-only; empty when the table does not exist yet."""
    return await asyncio.to_thread(_list_sync, conninfo)
