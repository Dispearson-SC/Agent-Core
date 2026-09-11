"""Driving adapter: what is not ready, answered ONCE.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-04
Tests:   Core/tests/integration/test_preflight.py
Used by: agent_core/main.py - the `preflight` subcommand, and the gate `serve` and
         `console` run before they start

WHY THIS EXISTS, AND WHY IT IS WORTH MORE THAN ANY ONE OF THE FIXES IT REPORTS
    Every failure in F11's list surfaced ONE AT A TIME, each three commands after the last:
    the database refused, then the credential was missing, then the model id did not
    resolve, then the policy table was empty, then the toolset was unregistered. Each fix
    revealed the next, and a human burned an afternoon walking that ladder.

    Nothing in that ladder was a hard problem. What cost the afternoon was the SHAPE: a
    process that raises on the first thing it finds teaches the operator nothing about the
    second. So this module answers the whole question in one pass and prints a LIST, and it
    is the only thing in the tree that deliberately keeps going after it finds a failure.

A REPORT WITHOUT A REMEDY IS THE SAME AFTERNOON IN ONE LINE
    Every failing check carries what to do about it - the command to run, the variable to
    set, the file to edit. `connection failed: FATAL: database "agent_core_app" does not
    exist` names a problem and no remedy, which is exactly how the ladder starts.

NEVER PRINT A CREDENTIAL VALUE. "MINIMAX_API_KEY: set" AND "GEMINI_API_KEY: missing" IS
THE WHOLE VOCABULARY.
    A readiness report is exactly the kind of output someone pastes into a chat window or
    an issue tracker. Nothing here formats a value, and the one check that has to READ a
    secret to answer reports only whether the name resolves. `models.py`'s refusals are
    quoted verbatim because that module makes the same promise in its own docstring - it
    names the variable to set and never carries the credential.

IT CONNECTS, WHICH IS THE ONE THING `composition.build_container` PROMISES NOT TO DO
    `composition.py` says NOTHING HERE CONNECTS, so that a wiring mistake surfaces on a
    laptop with nothing running. This module is the other half of that bargain: the
    questions a container cannot answer without touching the world are asked here, in one
    place, by something an operator runs on purpose.

WHAT IT DELIBERATELY DOES NOT DO
    It does not repair anything. It does not create a database, apply a migration, write a
    policy rule or export a variable - a tool that fixes what it finds is a tool nobody can
    run to find out what is wrong. `start_container` owns the bootstrap; this owns the
    diagnosis.

    It does not call a model or a channel. Resolving an endpoint and a credential is a
    local question litellm answers from its own registry (see
    `adapters/driven/llm_litellm/models.py`); spending money to find out whether a
    deployment is configured is not a preflight.

    It DOES compose each profile's toolsets, because that is the question `StartTurn` step
    3 asks and the only honest way to answer "is this profile servable". Today that reaches
    nothing; the day MCP composition lands (t-f6-05) it will open those transports, which
    is what a readiness report should say about a declared server - and is why the
    composition is bounded by a timeout.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_core.adapters.driven.llm_litellm.models import (
    ModelEndpointUnavailableError,
    model_for,
)
from agent_core.adapters.driven.tools.provider import (
    MCPNotComposedYetError,
    ToolsetBuilder,
    UnknownToolsetError,
    build_tool_provider,
)
from agent_core.composition import TOOL_PACKAGES, Settings, load_profiles
from agent_core.domain.profile import AgentProfile

__all__ = [
    "Check",
    "PreflightReport",
    "Severity",
    "preflight",
]

Severity = Literal["ok", "warn", "fail"]

# The categories a check is filed under. Strings rather than an enum because they are
# printed and matched by tests; what matters is that a report can be asked "did the
# DATABASE half pass" without parsing a sentence.
_DATABASE = "database"
_POLICY = "policy"
_PROFILE = "profile"
_CREDENTIAL = "credential"
_MODEL = "model"

# Connecting to diagnose, not to work. Short, because a preflight that hangs for the
# driver's default is a preflight nobody waits for.
_CONNECT_TIMEOUT_SECONDS = 5

# The second logical database `composition.ensure_databases` creates, by the convention the
# whole repository uses. Restated rather than imported for the reason composition.py gives
# about every other private constant it restates: reaching into another module's private
# name from a reporting tool couples the two.
_DBOS_DATABASE_SUFFIX = "_dbos"

# How long one profile gets to compose its toolsets. Local packages are pure construction
# and take none of it; an MCP transport is what this bound is for - see `_profile_checks`.
_COMPOSE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Check:
    """One question, asked and answered. Never a credential value. See the module docstring.

    `remedy` is REQUIRED in spirit for anything that is not `ok`: the whole value of this
    report over the failure ladder is that it says what to do next. `None` is allowed only
    where there is genuinely nothing to do - a passing check.
    """

    category: str
    name: str
    severity: Severity
    detail: str
    remedy: str | None = None

    @property
    def ready(self) -> bool:
        return self.severity != "fail"


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Everything that was asked, in the order it was asked. Including what passed.

    THE PASSING CHECKS ARE PART OF THE ANSWER. A report that printed only failures would
    leave an operator unable to tell "this deployment has no model configured" from "the
    preflight does not look at models", and the second is how a gap survives a phase.
    """

    checks: tuple[Check, ...]

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.severity == "fail")

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.severity == "warn")

    @property
    def ready(self) -> bool:
        """No hard failure. A warning is a deployment choice, not a refusal to start."""
        return not self.failures

    def render(self) -> str:
        """The list. One block per check, remedy indented under whatever is not ready."""
        headline = (
            f"preflight: {len(self.checks)} checks, "
            f"{len(self.failures)} not ready, {len(self.warnings)} to look at"
        )
        lines = [headline, ""]
        for check in self.checks:
            marker = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}[check.severity]
            lines.append(f"  {marker} {check.name}")
            lines.append(f"         {check.detail}")
            if check.remedy and check.severity != "ok":
                for remedy_line in check.remedy.splitlines():
                    lines.append(f"         -> {remedy_line}")
        return "\n".join(lines)


def _database_name(conninfo: str) -> str:
    dbname = conninfo_to_dict(conninfo).get("dbname")
    return dbname if isinstance(dbname, str) and dbname else "<unnamed>"


def _sibling_conninfo(conninfo: str, dbname: str) -> str:
    """The same server, same credentials, a different database.

    Parsed by psycopg rather than by a pattern, because a conninfo is legitimately either
    a URL or a keyword string - the reasoning `composition._database_name` writes down.
    """
    keywords: dict[str, Any] = dict(conninfo_to_dict(conninfo))
    keywords["dbname"] = dbname
    return make_conninfo(**keywords)


@dataclass(frozen=True, slots=True)
class _DatabaseFacts:
    """What one connection attempt could establish. Nothing here is a verdict yet."""

    reachable: bool
    server_reachable: bool
    migrations: int | None
    policy_rules: int | None
    error: str


def _inspect_database(conninfo: str) -> _DatabaseFacts:
    """Reachable? Migrated? Any rules? Asked on ONE connection, in one pass.

    A REFUSED SERVER AND A MISSING DATABASE ARRIVE IDENTICALLY, which is why the second
    question is asked at all: psycopg reports both as `OperationalError` with no SQLSTATE,
    because the failure happens while the connection is being established. Reaching the
    same host on the maintenance database is the only way to tell them apart without
    matching on a localised driver message - and they have different remedies, so telling
    them apart is the whole job (`composition._server_is_reachable` says the same).

    Autocommit, because a missing table aborts the transaction and the next question in
    this function would then fail for a reason that has nothing to do with its subject.
    """
    try:
        with psycopg.connect(
            conninfo, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_SECONDS
        ) as connection:
            migrations = _count(connection, "schema_migrations")
            policy_rules = _count(connection, "policy_rules")
        return _DatabaseFacts(
            reachable=True,
            server_reachable=True,
            migrations=migrations,
            policy_rules=policy_rules,
            error="",
        )
    except psycopg.OperationalError as refused:
        return _DatabaseFacts(
            reachable=False,
            server_reachable=_server_reachable(conninfo),
            migrations=None,
            policy_rules=None,
            # The driver's own sentence, kept: it names the host and the database and it
            # carries no credential (psycopg does not echo the password).
            error=str(refused).strip().splitlines()[0] if str(refused).strip() else "",
        )


def _count(connection: Any, table: str) -> int | None:
    """How many rows, or `None` when the table is not there at all.

    `None` and `0` are different answers and the report treats them differently: no table
    is a schema that was never applied, and no rows in an existing table is a deployment
    that will deny every tool call with "no matching rule".
    """
    try:
        row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    except psycopg.Error:
        return None
    return None if row is None else int(row[0])


def _server_reachable(conninfo: str) -> bool:
    try:
        with psycopg.connect(
            _sibling_conninfo(conninfo, "postgres"),
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        ):
            return True
    except psycopg.Error:
        return False


def _database_exists(conninfo: str, dbname: str) -> bool | None:
    """`None` when the question could not be asked - the server itself is unreachable."""
    try:
        with psycopg.connect(
            _sibling_conninfo(conninfo, "postgres"),
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        ) as maintenance:
            return (
                maintenance.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
                ).fetchone()
                is not None
            )
    except psycopg.Error:
        return None


def _database_checks(settings: Settings, facts: _DatabaseFacts) -> list[Check]:
    """The app database, its schema, and the second logical database beside it."""
    database = _database_name(settings.app_conninfo)
    checks: list[Check] = []

    if not facts.reachable and not facts.server_reachable:
        return [
            Check(
                category=_DATABASE,
                name=f"database {database}",
                severity="fail",
                detail=f"the server is not answering: {facts.error}",
                remedy=(
                    "Start PostgreSQL, or point AGENT_CORE_DATABASE_URL at a server that "
                    "is running. Nothing below this line was checked."
                ),
            )
        ]

    if not facts.reachable:
        return [
            Check(
                category=_DATABASE,
                name=f"database {database}",
                severity="fail",
                detail=(
                    f'the server is up and the database "{database}" is not there: '
                    f"{facts.error}"
                ),
                remedy=(
                    f'CREATE DATABASE "{database}";\n'
                    f'CREATE DATABASE "{database}{_DBOS_DATABASE_SUFFIX}";\n'
                    "Or set AGENT_CORE_ADMIN_DATABASE_URL to an administrative connection "
                    "string and start once: startup creates both and migrates them, "
                    "idempotently. It is optional on purpose - the app never needs "
                    "superuser to RUN, only to bootstrap."
                ),
            )
        ]

    checks.append(
        Check(
            category=_DATABASE,
            name=f"database {database}",
            severity="ok",
            detail="reachable",
        )
    )

    if facts.migrations is None:
        checks.append(
            Check(
                category=_DATABASE,
                name=f"schema {database}",
                severity="fail",
                detail="there is no schema_migrations table, so no migration has run",
                remedy=(
                    "Start the process once - `start_container` applies every migration "
                    "in the tree, discovered rather than listed, and is idempotent."
                ),
            )
        )
    else:
        checks.append(
            Check(
                category=_DATABASE,
                name=f"schema {database}",
                severity="ok",
                detail=f"{facts.migrations} migrations applied",
            )
        )

    audit_database = _database_name(settings.resolved_audit_conninfo)
    if audit_database != database:
        audit = _inspect_database(settings.resolved_audit_conninfo)
        checks.append(
            Check(
                category=_DATABASE,
                name=f"audit database {audit_database}",
                severity="ok" if audit.reachable else "fail",
                detail="reachable" if audit.reachable else audit.error,
                remedy=(
                    None
                    if audit.reachable
                    else (
                        "AGENT_CORE_AUDIT_DATABASE_URL names a database that cannot be "
                        "reached. The audit sink writes on its own connection so a "
                        "rolled-back turn still leaves a trace; unreachable, it leaves "
                        "none."
                    )
                ),
            )
        )

    dbos_database = f"{database}{_DBOS_DATABASE_SUFFIX}"
    present = _database_exists(settings.app_conninfo, dbos_database)
    if present is False:
        checks.append(
            Check(
                category=_DATABASE,
                name=f"database {dbos_database}",
                # A WARNING and not a failure. The durable engine derives its own system
                # database from the app URL and creates it at launch, so a deployment
                # missing this one can still take a turn - see docs/TASKS.md#t-f11-20,
                # which owns the question of which of the two is the real one.
                severity="warn",
                detail="the second logical database the bootstrap creates is not there",
                remedy=(
                    "Set AGENT_CORE_ADMIN_DATABASE_URL for one start, or "
                    f'CREATE DATABASE "{dbos_database}";'
                ),
            )
        )

    return checks


def _policy_checks(settings: Settings, facts: _DatabaseFacts) -> list[Check]:
    """An empty `policy_rules` denies every tool call, and says so as if the store were down.

    That pairing is why this is a hard failure rather than a note: a fresh deployment with
    no rules refuses every tool with "no matching rule", and until t-f11-06 it reported
    that refusal as an unreachable store - so the operator went looking for a database
    problem that did not exist.
    """
    checks: list[Check] = []

    if not settings.policy_dir.is_dir():
        checks.append(
            Check(
                category=_POLICY,
                name="policy directory",
                severity="warn",
                detail=f"{settings.policy_dir} is not there, so startup manages no rules",
                remedy=(
                    "Absence is meaningful and is not the same as empty: whatever is in "
                    "policy_rules stays in force. Create the directory and put reviewed "
                    "rules in it to manage policy from files (t-f11-03)."
                ),
            )
        )

    if not facts.reachable or facts.policy_rules is None:
        return checks

    if facts.policy_rules == 0:
        checks.append(
            Check(
                category=_POLICY,
                name="policy_rules",
                severity="fail",
                detail="the table is there and empty, so every tool call is denied",
                remedy=(
                    f"Put reviewed rules in {settings.policy_dir} - they are reconciled "
                    "into the table at every startup, so a rule added there is granted "
                    "and a rule deleted there stops applying. Never INSERT by hand."
                ),
            )
        )
    else:
        checks.append(
            Check(
                category=_POLICY,
                name="policy_rules",
                severity="ok",
                detail=f"{facts.policy_rules} rules in force",
            )
        )
    return checks


async def _profile_checks(
    profiles: Mapping[str, AgentProfile], packages: Mapping[str, ToolsetBuilder]
) -> list[Check]:
    """Can every loaded profile actually take a turn? Asked of the PROVIDER, not of a copy.

    THE QUESTION IS ASKED THE WAY A TURN ASKS IT, AND THAT IS THE WHOLE DESIGN
        `StartTurn` step 3 calls `ToolProvider.toolset_for(profile)`, and that is where an
        unregistered toolset and an uncomposable MCP server are both refused. This calls the
        same method on a provider built from the same registry, so a profile that passes
        here is servable by definition rather than by a second opinion that agrees today.

        `composition._refuse_unservable_profiles` deliberately checks toolsets and NOT MCP
        servers, for exactly this reason - "widening this check to cover it would put that
        decision in two places, and the two would drift". A report that reimplemented
        either rule would be the third place. It reimplements neither.

    IT RAISES ON A PROFILE AND KEEPS GOING, WHICH IS WHY THIS IS NOT THAT FUNCTION. The
    startup check refuses the whole deployment on the first unservable profile, correctly;
    a report names every one of them.

    THE TIMEOUT IS NOT DEFENSIVE PADDING. Composing a local toolset is pure construction and
    cannot block. Composing an MCP toolset (t-f6-05) opens a transport, and the day that
    lands, this check becomes a REACHABILITY check for every declared server - which is what
    a preflight should say about one, and also the only way this can hang. Bounded, a server
    that does not answer is one line in the report instead of a process that never starts.
    """
    provider = build_tool_provider(packages)
    checks: list[Check] = []

    for profile_id, profile in sorted(profiles.items()):
        named = ", ".join(profile.toolsets) or "(none)"
        try:
            await asyncio.wait_for(
                provider.toolset_for(profile), timeout=_COMPOSE_TIMEOUT_SECONDS
            )
        except UnknownToolsetError as unknown:
            checks.append(
                Check(
                    category=_PROFILE,
                    name=f"profile {profile_id}",
                    severity="fail",
                    detail=str(unknown),
                    remedy=(
                        "Register the package in composition.TOOL_PACKAGES, or stop "
                        "shipping the profile. Unregistered, every turn under it dies at "
                        "StartTurn step 3 - after the model has been paid for. Registered "
                        f"here: {', '.join(sorted(packages))}."
                    ),
                )
            )
        except MCPNotComposedYetError:
            servers = ", ".join(repr(server.name) for server in profile.mcp_servers)
            checks.append(
                Check(
                    category=_PROFILE,
                    name=f"profile {profile_id}",
                    severity="fail",
                    detail=f"declares MCP servers this process cannot compose: {servers}",
                    remedy=(
                        "The tool provider this container wires is the local-only one, so "
                        "every turn under this profile raises MCPNotComposedYetError. Wire "
                        "the MCP toolset (docs/TASKS.md#t-f6-05) or remove the server."
                    ),
                )
            )
        except TimeoutError:
            checks.append(
                Check(
                    category=_PROFILE,
                    name=f"profile {profile_id}",
                    severity="fail",
                    detail=(
                        f"its toolsets did not compose within {_COMPOSE_TIMEOUT_SECONDS}s"
                    ),
                    remedy=(
                        "Something this profile names is waiting on a transport that is "
                        "not answering - an MCP server, most likely. A turn would wait the "
                        "same way, with a customer on the other end of it."
                    ),
                )
            )
        except Exception as refused:  # noqa: BLE001 - a report must survive any provider
            checks.append(
                Check(
                    category=_PROFILE,
                    name=f"profile {profile_id}",
                    severity="fail",
                    detail=f"{type(refused).__name__}: {refused}",
                    remedy=(
                        "The tool provider refused this profile, so every turn under it "
                        "dies at StartTurn step 3 with the same message."
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    category=_PROFILE,
                    name=f"profile {profile_id}",
                    severity="ok",
                    detail=f"servable: toolsets {named}",
                )
            )
    return checks


def _credential_variable(model_id: str) -> str | None:
    """The environment variable a provider's credential arrives in, BY NAME.

    litellm's own convention: the token before the first `/` is the provider, and its
    credential is `<PROVIDER>_API_KEY` - the same name `models.py` puts in its refusal.
    A model id with no provider prefix answers `None` rather than a guess; the endpoint
    check below still asks litellm the real question and reports whatever it says.
    """
    provider, separator, _model = model_id.partition("/")
    if not separator or not provider:
        return None
    return f"{provider.upper()}_API_KEY"


def _credential_checks(
    settings: Settings, profiles: Mapping[str, AgentProfile], environ: Mapping[str, str]
) -> list[Check]:
    """Resolvable BY NAME. Nothing here reads, compares, logs or renders a VALUE.

    THE PROCESS ENVIRONMENT IS WHAT IS CHECKED, AND THAT IS NOT AN OVERSIGHT. litellm
    resolves a provider credential from `os.environ` when it builds the client, and
    `Settings.from_env` layers the repository's `.env` UNDER the environment for its OWN
    fields only - it does not export anything. So a key that lives only in the credentials
    file is invisible to the model call, and this check answers the question the turn will
    actually ask.
    """
    if settings.proxy_base_url:
        return [
            Check(
                category=_CREDENTIAL,
                name="AGENT_CORE_LITELLM_BASE_URL",
                severity="ok",
                detail=(
                    "proxy mode: the provider credentials are the proxy's, and this "
                    "process holds none"
                ),
            )
        ]

    checks: list[Check] = []
    for variable in sorted(
        {
            name
            for profile in profiles.values()
            if (name := _credential_variable(profile.model)) is not None
        }
    ):
        present = bool(environ.get(variable))
        checks.append(
            Check(
                category=_CREDENTIAL,
                name=variable,
                severity="ok" if present else "fail",
                # "set" and "missing" is the whole vocabulary. See the module docstring.
                detail="set" if present else "missing",
                remedy=(
                    None
                    if present
                    else (
                        f"Export {variable} in the environment this process runs in. A "
                        "value that lives only in the credentials file is not exported, "
                        "and the model client reads the environment - so `.env` alone "
                        "leaves this missing."
                    )
                ),
            )
        )
    return checks


def _model_check(model_id: str, base_url: str | None) -> Check:
    """Does a client exist for this model at all - endpoint AND credential?

    Asked through `models.model_for`, the same function the runner's default factory calls,
    so this reports the answer production will get rather than a second opinion about it.
    It builds a client object and makes no request: resolution is local.
    """
    try:
        model_for(model_id, base_url)
    except ModelEndpointUnavailableError as unavailable:
        return Check(
            category=_MODEL,
            name=f"model {model_id}",
            severity="fail",
            detail="no endpoint and credential resolve for it",
            # Quoted verbatim: that module promises to name the variable to set and never
            # to carry a credential, which is this module's promise too.
            remedy=str(unavailable),
        )
    except Exception as unexpected:  # noqa: BLE001 - a report must survive any resolver
        return Check(
            category=_MODEL,
            name=f"model {model_id}",
            severity="fail",
            detail=f"resolving it raised {type(unexpected).__name__}: {unexpected}",
            remedy=(
                "A profile's `model:` must name a provider litellm knows, as "
                "`provider/model` - see docs/FIELD-NOTES.md for the ids verified against "
                "this version."
            ),
        )
    return Check(
        category=_MODEL,
        name=f"model {model_id}",
        severity="ok",
        detail="endpoint and credential resolve" if base_url is None else "proxy mode",
    )


def _model_checks(settings: Settings, profiles: Mapping[str, AgentProfile]) -> list[Check]:
    """One check per DISTINCT model, not per profile. Two profiles on one model is one fact."""
    return [
        _model_check(model_id, settings.proxy_base_url)
        for model_id in sorted({profile.model for profile in profiles.values()})
    ]


async def preflight(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    tool_packages: Mapping[str, ToolsetBuilder] | None = None,
) -> PreflightReport:
    """Ask everything, answer once. NOTHING here raises on a finding. t-f11-04.

    D13: every blocking call - psycopg, and litellm's registry - runs in a thread, exactly
    as the driven adapters do.

    ORDER IS THE ORDER AN OPERATOR READS IN: the database, its schema, the rules in force,
    the profiles, the credentials they need, and the models they name. A failure in one
    does not stop the next - that is the whole point - except where the answer would be
    meaningless: a database that is not there has no `policy_rules` to count, and a profile
    directory that will not load has no models to resolve.

    `environ` defaults to the real environment and `tool_packages` to the registry this
    process actually serves. Both are seats so the report can be asserted without one.
    """
    resolved_environ = os.environ if environ is None else environ
    packages = TOOL_PACKAGES if tool_packages is None else tool_packages

    facts = await asyncio.to_thread(_inspect_database, settings.app_conninfo)
    checks: list[Check] = []
    checks.extend(_database_checks(settings, facts))
    checks.extend(_policy_checks(settings, facts))

    try:
        profiles: Mapping[str, AgentProfile] = await asyncio.to_thread(
            load_profiles, settings.profiles_dir
        )
    except Exception as unloadable:  # noqa: BLE001 - any parse error is one finding here
        checks.append(
            Check(
                category=_PROFILE,
                name=f"profiles {settings.profiles_dir}",
                severity="fail",
                detail=f"{type(unloadable).__name__}: {unloadable}",
                remedy=(
                    "Fix the profile file named above. Nothing that depends on a loaded "
                    "profile - its toolsets, its credential, its model - could be checked."
                ),
            )
        )
        return PreflightReport(checks=tuple(checks))

    if not profiles:
        checks.append(
            Check(
                category=_PROFILE,
                name=f"profiles {settings.profiles_dir}",
                severity="fail",
                detail="no profile files, so this deployment serves no agent",
                remedy=(
                    "Put at least one profile in the directory, or point "
                    "AGENT_CORE_PROFILES_DIR at the one that has them."
                ),
            )
        )
        return PreflightReport(checks=tuple(checks))

    checks.extend(await _profile_checks(profiles, packages))
    checks.extend(_credential_checks(settings, profiles, resolved_environ))
    checks.extend(
        await asyncio.to_thread(_model_checks, settings, profiles)
    )
    return PreflightReport(checks=tuple(checks))
