"""Driving adapter: which turns did an application-version bump strand? (D30)

Tests:   Core/tests/unit/test_stranded_turns.py, Core/tests/integration/test_stranded_turns.py
Used by: agent_core/main.py - the `stranded-turns` subcommand
Runbook: docs/OPERATIONS.md, "Bumping the DBOS application version"

WHY THIS EXISTS
    `composition.PINNED_DBOS_APPLICATION_VERSION` is bumped whenever a change makes in-flight
    turns incompatible with the new code (D28 took it from .1 to .2). DBOS recovers a PENDING
    workflow only when its row names the version the recovering process claims, so a turn
    suspended on the old version - waiting for a person, or for a peer - is never resumed. It
    stays PENDING for ever, and because `agent-core-turns` is partitioned with one slot per
    session, it also blocks every later turn of that conversation. Nothing raises.

    Nothing reported it either. This command is the report: it lists the open workflows whose
    version is NOT the one this build claims, and exits non-zero when there is any, so a deploy
    script can gate on it BEFORE the bump (drain) and verify AFTER it (cancel what is left).

IT READS, IT NEVER WRITES, AND IT DOES NOT LAUNCH DBOS
    Launching the engine would start recovery and dequeueing in this process. The check reads
    the DBOS system database's `workflow_status` table with one SELECT. Cancelling is a
    different act with different consequences and stays a documented operator step
    (`dbos workflow cancel`), not a flag here: writing DBOS's tables from outside it would
    be a second opinion about workflow state.

EXIT CODE: 0 nothing stranded, 1 stranded turns listed, 2 the check could not run. A check
that cannot run is never reported as a clean one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_core.adapters.driven.persistence_pg.migrations import dbos_system_database
from agent_core.composition import Settings

__all__ = [
    "OPEN_STATUSES",
    "StrandedCheckUnavailableError",
    "StrandedWorkflow",
    "find_stranded_workflows",
    "render_report",
    "run_stranded_check",
    "system_conninfo",
]

# Statuses DBOS still intends to run. SUCCESS, ERROR, CANCELLED and the like are history.
OPEN_STATUSES: Final[tuple[str, ...]] = ("PENDING", "ENQUEUED", "DELAYED")

_CONNECT_TIMEOUT_SECONDS: Final[int] = 5

_SELECT_STRANDED = """
    SELECT workflow_uuid, name, status, application_version, queue_name, created_at
    FROM dbos.workflow_status
    WHERE status = ANY(%s)
      AND application_version IS DISTINCT FROM %s
    ORDER BY created_at, workflow_uuid
"""


class StrandedCheckUnavailableError(RuntimeError):
    """The system database could not be read. Distinct from 'nothing stranded'."""


@dataclass(frozen=True, slots=True)
class StrandedWorkflow:
    workflow_id: str
    name: str
    status: str
    application_version: str
    queue_name: str | None
    created_at: datetime


def system_conninfo(app_conninfo: str) -> str:
    """The conninfo of the database DBOS opens: the app database plus `_dbos_sys`.

    Parsed by psycopg because a conninfo is legitimately a URL or a keyword string.
    """
    keywords: dict[str, Any] = dict(conninfo_to_dict(app_conninfo))
    app_database = str(keywords.get("dbname") or "")
    if not app_database:
        raise StrandedCheckUnavailableError("the application conninfo names no database")
    keywords["dbname"] = dbos_system_database(app_database)
    return make_conninfo(**keywords)


def find_stranded_workflows(settings: Settings) -> tuple[StrandedWorkflow, ...]:
    """Open workflows whose application version is not the one this build claims."""
    try:
        with psycopg.connect(
            system_conninfo(settings.app_conninfo), connect_timeout=_CONNECT_TIMEOUT_SECONDS
        ) as connection:
            rows = connection.execute(
                _SELECT_STRANDED, (list(OPEN_STATUSES), settings.dbos_application_version)
            ).fetchall()
    except psycopg.errors.UndefinedTable:
        return ()  # DBOS has never run against this database: nothing can be stranded
    except psycopg.Error as unreachable:
        # str(error) carries no password (psycopg does not echo it), but name the class.
        raise StrandedCheckUnavailableError(
            f"could not read the DBOS system database ({type(unreachable).__name__}): "
            f"{str(unreachable).strip().splitlines()[0] if str(unreachable).strip() else ''}"
        ) from unreachable
    return tuple(
        StrandedWorkflow(
            workflow_id=str(row[0]),
            name=str(row[1]),
            status=str(row[2]),
            application_version=str(row[3]),
            queue_name=None if row[4] is None else str(row[4]),
            created_at=datetime.fromtimestamp(int(row[5]) / 1000, tz=UTC),
        )
        for row in rows
    )


def render_report(current_version: str, stranded: tuple[StrandedWorkflow, ...]) -> str:
    if not stranded:
        return (
            "[stranded-turns] no stranded turns: nothing open under a version other than "
            f"{current_version}."
        )
    lines = [
        f"[stranded-turns] {len(stranded)} open workflow(s) will NOT be recovered by a build "
        f"claiming {current_version}:"
    ]
    for item in stranded:
        queue = item.queue_name or "-"
        lines.append(
            f"  {item.workflow_id}  {item.status:<8} version={item.application_version}  "
            f"queue={queue}  since {item.created_at:%Y-%m-%d %H:%M}Z"
        )
    lines.append(
        "  Each one holds its session's queue slot. Drain them (let the turns finish or expire) "
        "or cancel them: see docs/OPERATIONS.md, 'Bumping the DBOS application version'."
    )
    return "\n".join(lines)


def run_stranded_check(
    settings: Settings,
    *,
    find: Callable[[Settings], tuple[StrandedWorkflow, ...]] = find_stranded_workflows,
    write_line: Callable[[str], None] = print,
) -> int:
    """Print the report and return the exit code (0 clean, 1 stranded, 2 could not check)."""
    try:
        stranded = find(settings)
    except StrandedCheckUnavailableError as unavailable:
        write_line(f"[stranded-turns] cannot check: {unavailable}")
        return 2
    for line in render_report(settings.dbos_application_version, stranded).splitlines():
        write_line(line)
    return 1 if stranded else 0
