"""Composition root - the ONE place adapters are chosen and wired.

Phase:   F0 (minimal) / grows every phase
Tasks:   docs/TASKS.md#t-f0-02, docs/TASKS.md#t-f3-15, docs/TASKS.md#t-f1-23,
         docs/TASKS.md#t-f3-17, docs/TASKS.md#t-f2-12, docs/TASKS.md#t-f11-01,
         docs/TASKS.md#t-f11-02, docs/TASKS.md#t-f11-03, docs/TASKS.md#t-f11-05
Status:  WIRES EVERY SEAT (t-f0-02, t-f1-21) - `start_turn` IS BUILT, SEE THE SEATS NOTE.
         t-f3-15 additionally constructs `DecideApproval`, binding D25's `requester` seat
         to a real lookup (`_pg_requester_lookup`, reading `audit_tool_calls`). Its
         `signal` seat was bound to a function that RAISED until t-f11-19; it is now
         `turn_workflow.signal_decision`, so a recorded approval actually wakes the turn
         it approved - see THE SEAT THAT RECORDED A DECISION AND WOKE NOBODY below.
         t-f5-10: `LadderContextEngine` is now built with a real `ModelSummariser`, not
         `None` - see THE SEAT THAT WAS WIRED BUT DEGRADED FOR FOUR WAVES, NOW FILLED.
         t-f1-23: `start_container` is the production startup path - it applies EVERY
         migration in the tree (discovered, never listed) and then builds. `build_container`
         still connects to nothing; see that function's docstring for why the schema step is
         beside it rather than inside it.
         t-f3-17: `bind_turn_workflow` now passes `human_gateway`, so a turn that suspends
         on a human no longer dies in `_step_publish`.
         t-f0-06: `Container.turn_lookup` now exists (`_pg_turn_lookup`, reading the same
         `turns` row `store` writes) and `main.build_app` binds it, together with the
         `decide_approval` this file already built, into `routes.create_app`'s
         `lookup_turn` / `decide` seats. Until this, `composition.py` built a working
         `decide_approval` and no `lookup_turn` at all, `main.py` handed NEITHER to
         `create_app`, and `GET /turns/{turn_id}` / `POST /decisions/{corr_id}` answered
         503 in every process `main.serve()` ever started - the fourth instance of "a test
         supplies the collaborator production never binds" (docs/STATE.md).

         t-f7-08/t-f7-11: `Container` now carries the three seats `POST /evidence/{corr_id}`
         needs - `ingest_media`, `resolve_evidence` and `signal_evidence` - and `main.py`
         hands all three to `routes.create_app`. Until this, the route answered 503 in
         every process `serve()` started while its own test injected the collaborators:
         the SIXTH instance of the shape docs/STATE.md records. `_pg_evidence_correlate`
         below is also where an approval handle is stopped from being spent as an evidence
         handle - read the SQL's comment before touching the `kind` predicate.

         t-f2-12: `dbos_config` now exists, and it is the first thing in `Core/src` ever to
         construct a DBOS configuration. It pins `application_version`, which is what stops
         a worker killed mid-turn from silencing its session forever - the partitioned
         dequeue holds the slot whatever version wrote the PENDING row, while recovery only
         claims rows of the version this process runs. Read that function before changing
         the pin: it carries the one rule a pinned version puts on whoever deploys.

         F11 - THE COLD START. Four of the things a human had to do by hand the first time
         anyone used this system were this file's (docs/ROADMAP.md, F11), and all four were
         invisible because a test supplied the missing collaborator:
           t-f11-01 the process never read `.env`, so every credential had to be exported
                    at a terminal. `Settings.from_env` now layers the credentials file
                    UNDER the real environment - see `_read_env_file`. No value is logged.
           t-f11-02 `AGENT_CORE_ADMIN_DATABASE_URL` is now an OPTIONAL seat. Present,
                    `start_container` creates the two logical databases and migrates;
                    absent, it migrates only and a missing database raises
                    `MissingDatabaseError` naming the exact command. The app never REQUIRES
                    superuser - see `start_container` for why that reasoning survived.
           t-f11-03 nothing in production wrote `policy_rules`, so a fresh deployment
                    denied every tool and raw SQL was the only fix. `Core/policy/*.yaml` is
                    now RECONCILED at startup by `adapters/driven/policy_fs/loader.py`.
           t-f11-05 `fraud_analyst` shipped naming a toolset nothing registered; it parsed,
                    got a version, and died at `StartTurn` step 3 on every turn. `fraud` is
                    registered in `TOOL_PACKAGES` and `_refuse_unservable_profiles` moves
                    that class of failure from turn three to load.

WHY A SINGLE FILE
    Every `import` of a concrete adapter lives here and nowhere else. That is what makes
    the layer rule checkable by reading one file instead of grepping the tree.

    If a use case ever imports an adapter directly, the hexagon has a hole. The ruff
    banned-api config in pyproject.toml catches the common cases; this file catches the
    rest by being the only legitimate importer.

    `tests/unit/test_composition.py` states that rule from both sides: no module under
    `domain/`, `application/` or `ports/` may name `agent_core.adapters`, and this file
    must. Only the pair is a rule - the first half alone is satisfied by wiring nothing.

PSEUDO-CODE - F0, extended every phase. `build_container` below is the real thing; this
sketch is the shape, and where the two disagree the code is right.
    def build_container(settings) -> Container:
        model    = LiteLLMGateway(settings)                   # F0
        tools    = build_tool_provider()                      # F1 local, F6 mcp
        policy   = PgToolPolicy(pool)                         # F1
        store    = PgConversationStore(pool)                  # F1
        audit    = PgAuditSink(separate_pool)                 # F1 - SEPARATE POOL, see below
        context  = LadderContextEngine(summariser)            # F5
        skills   = FilesystemSkillRegistry(root)              # F6
        media    = FsMediaStore(root)                         # F7
        human    = ChannelHumanGateway(...)                   # F3
        runner   = PydanticAgentRunner(model, policy, audit, tools)  # F1
        profiles = load_profiles(Path("Core/profiles"))       # F1
        return Container(start_turn=StartTurn(...), ...)

    AuditSink gets its OWN connection pool on purpose. It must write outside the domain
    transaction so a rolled-back turn still leaves a trace. CLAUDE.md non-negotiable #6.
    Sharing the pool is the easy mistake and it silently erases the evidence of exactly
    the turns you most need to explain.

THE SEATS - ALL EIGHT ARE FILLED, AND THIS NOTE IS WHAT KEEPS THAT HONEST
    `StartTurn` takes eight collaborators, and for four waves this note said three had no
    constructible adapter. Two of those three were WRONG by the time anyone read it:
    `adapters/driven/context/engine.py` and `adapters/driven/skills_fs/registry.py` were
    both implemented and this file still called them docstring-only. An inaccurate note is
    how a seat stays empty without anyone noticing, so the note is now maintained as
    carefully as the wiring - it is the only place that claims the set is complete.

    The last genuinely empty seat was `ToolProvider`: docs/TASKS.md#t-f1-07 froze the port,
    no anchor claimed the adapter, and the sole implementation in the repository lived in
    `tests/integration/test_f0_end_to_end.py` labelled "a TEST double, not an adapter".
    `adapters/driven/tools/provider.py` (docs/TASKS.md#t-f1-21) fills it, resolving the
    profile's named toolsets against an explicit registry - never by discovery.

    THE NOTE IS NO LONGER THE ONLY THING KEEPING THAT HONEST. `tests/unit/test_composition.py`
    now asserts the positive: `start_turn` exists, holds THESE adapter objects, and none of
    the methods a first turn reaches is a `raise NotImplementedError`. The note explains
    why; the test is what fails when a seat empties out again.

    THE RULE THAT PRODUCED THE REFUSAL STILL STANDS. Wiring a PLACEHOLDER for a seat gives
    a container that builds and then dies on the first turn, which is the shape of bug this
    file exists to prevent. So if a seat is ever emptied again - a port frozen ahead of its
    adapter, an adapter deleted - `build_container` goes back to refusing to construct
    `StartTurn` and this note names the seat, by name, rather than leaving the absence to be
    discovered on the first turn.

    THE SEAT THAT WAS WIRED BUT DEGRADED FOR FOUR WAVES, NOW FILLED
    `LadderContextEngine` takes an optional `Summariser`, and until t-f5-10 nothing in
    the tree built one, so this file wired it with none: rungs L1 and L2 worked, L3 and
    L4 froze nothing and said so through `made_progress` on every real turn - the
    silent-bug table's "compaction strategy - only on the bill", verified in production
    wiring rather than only in `adapters/driven/context/summariser.py`'s own tests.
    `context = LadderContextEngine(ModelSummariser(model))` now passes the SAME
    `ModelGateway` every other model call in this container uses, so a summarisation
    request never ends up on a different day-1/day-2 route from the turn it is
    compacting. `tests/unit/test_summariser.py` asserts this from the container's own
    seat, not just that `ModelSummariser` works when constructed directly - an optional
    seat left at `None` is exactly what that assertion is there to catch if it recurs.

    THE DAY-1 MODEL ENDPOINT, WHICH WAS A REAL GAP AND IS NOW WIRED
        `Settings.proxy_base_url` is None on day 1 (library mode). The runner's default
        model factory used to REFUSE that case, because Pydantic AI's `LiteLLMProvider` is
        an OpenAI-compatible HTTP client and one built with no endpoint silently targets
        api.openai.com - a MiniMax profile's traffic, with a MiniMax key, arriving at
        OpenAI. Safe, and it left production unable to call a model at all.

        `adapters/driven/llm_litellm/models.py` now serves both modes: day 1 resolves the
        provider's own endpoint and credential through litellm, day 2 points at
        `AGENT_CORE_LITELLM_BASE_URL`. Setting that variable is a day-2 choice rather than
        the only way to get a working deployment. A model that genuinely cannot be served
        still raises `models.ModelEndpointUnavailableError`, before any client exists.

NOTHING HERE CONNECTS
    `build_container` is callable with no database running. Pools are constructed
    unopened and adapters take a connection SOURCE, not a connection, so the whole
    wiring is inspectable on a laptop with nothing up. A wiring mistake that only
    surfaces once Postgres is reachable is a wiring mistake nobody finds until deploy.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from agent_core.adapters.driven.agent_pydantic.runner import PydanticAgentRunner
from agent_core.adapters.driven.context.engine import LadderContextEngine
from agent_core.adapters.driven.context.summariser import ModelSummariser
from agent_core.adapters.driven.human.gateway import ChannelHumanGateway
from agent_core.adapters.driven.knowledge_pg.admin import PgKnowledgeAdmin
from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway
from agent_core.adapters.driven.media_fs.store import FilesystemMediaStore
from agent_core.adapters.driven.peers.mailbox import PgAgentMailbox
from agent_core.adapters.driven.persistence_pg.audit_read_repository import (
    PgAuditReadRepository,
)
from agent_core.adapters.driven.persistence_pg.audit_repository import PgAuditSink
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
)
from agent_core.adapters.driven.persistence_pg.migrations import (
    apply_all_migrations,
    ensure_databases,
)
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.persistence_pg.transcript_repository import (
    PgTranscriptReader,
)
from agent_core.adapters.driven.policy_fs.loader import apply_policy_rules, load_policy_rules
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.adapters.driven.skills_fs.registry import FilesystemSkillRegistry
from agent_core.adapters.driven.tools import peers as peer_tools
from agent_core.adapters.driven.tools.fraud import tools as fraud_tools
from agent_core.adapters.driven.tools.provider import (
    DEFAULT_TOOL_PACKAGES,
    LocalToolProvider,
    ToolsetBuilder,
    build_tool_provider,
)
from agent_core.adapters.driving.channels.registry import (
    Channel,
    ChannelRegistry,
    OutboundMessage,
)
from agent_core.adapters.driving.channels.telegram import TELEGRAM_CHANNEL_ID, TelegramChannel
from agent_core.adapters.driving.channels.whatsapp import WhatsAppChannel
from agent_core.adapters.driving.http.routes import (
    EvidenceCorrelate,
    EvidenceSignal,
    TurnLookup,
    TurnStatus,
    TurnView,
)
from agent_core.adapters.driving.workflow.turn_workflow import (
    TurnWorkflowDependencies,
    bind_dependencies,
)
from agent_core.adapters.driving.workflow.turn_workflow import (
    signal_decision as durable_signal_decision,
)
from agent_core.adapters.driving.workflow.turn_workflow import (
    signal_evidence as durable_signal_evidence,
)
from agent_core.application.decide_approval import DecideApproval, RequesterLookup
from agent_core.application.ingest_media import IngestMedia
from agent_core.application.resume_turn import ResumeTurn
from agent_core.application.start_turn import StartTurn
from agent_core.domain.media import MediaRef
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    TenantId,
    ToolCallId,
    TurnId,
)
from agent_core.ports.agent_mailbox import AgentMailbox
from agent_core.ports.knowledge_admin import KnowledgeAdmin
from agent_core.ports.transcript_reader import TranscriptReader

__all__ = [
    "PINNED_DBOS_APPLICATION_VERSION",
    "TOOL_PACKAGES",
    "Container",
    "DbosConfig",
    "MissingDatabaseError",
    "PoolConnections",
    "PoolFactory",
    "PullModeChannel",
    "Settings",
    "UnservableProfileError",
    "bind_turn_workflow",
    "build_channel_registry",
    "build_container",
    "dbos_config",
    "load_profiles",
    "start_container",
]

# whatsapp.py deliberately does not export its channel id - it is a `_CHANNEL_ID` module
# constant, private on purpose (see that module's docstring on leaf isolation). Reaching
# into another module's private name from here would be the same coupling the leaf
# adapters exist to avoid, so the literal is restated once, at the one place D23 always
# meant it to live: the wiring.
_WHATSAPP_CHANNEL_ID = "whatsapp"

# The channel id `adapters/driving/http/routes.py` stamps on every turn that arrives over
# HTTP - its `_DEFAULT_CHANNEL`, restated here for the same reason `_WHATSAPP_CHANNEL_ID`
# is: the name is private to that module and reaching into it from the wiring would couple
# the two. `tests/unit/test_composition.py` reads the id FROM routes.py and asserts it is
# registered here, so the restatement cannot drift without a red test.
_HTTP_CHANNEL_ID = "http"

# The repository layout is `Core/src/agent_core/composition.py`, so `Core/` is three
# parents up and `Core/profiles/` is the directory the profile files live in. Overridable
# by environment because an installed wheel has no `Core/` above it.
_CORE_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_APP_CONNINFO = "postgresql://localhost/agent_core_app"

# t-f11-01. The credentials file a fresh clone gets, at the REPOSITORY root - one above
# `Core/`, beside `README.md` and the `.gitignore` entry that keeps it out of the history.
# Read as a FALLBACK under the real environment; see `_read_env_file` and `Settings.from_env`.
_DEFAULT_ENV_FILE = _CORE_ROOT.parent / ".env"

# t-f11-34. THE MECHANISM TOOLSET, AND IT IS NOT A VERTICAL.
#
# `delivery` and `fraud` below are verticals - one package per business domain, named by
# the profile that wants it, exactly as CLAUDE.md's contract describes. `peers` is the
# other kind: a MECHANISM a profile switches on in its `peers:` block
# (`adapters/driven/tools/peers.py`, WHY THIS IS NOT A VERTICAL'S TOOL), the same way
# `media.allow_evidence_requests` switches on `request_evidence`.
#
# It is registered as a package anyway, rather than composed onto the toolset behind the
# provider's back, because `LocalToolProvider` resolves a profile's NAMED toolsets and
# nothing else - NO AUTO-DISCOVERY, which is the security property and not an
# implementation detail of it. So the grant is made where every other grant is made: by
# putting this name in `profile.toolsets` at load (`_grant_peer_mechanism`), where
# `:profile`, `:tools`, `preflight` and the audit record all see it. A provider wrapper
# would have given the same tool and shown it nowhere.
PEER_TOOLSET: Final[str] = "peers"

# t-f11-05. THE TOOL REGISTRY THIS PROCESS SERVES.
#
# `adapters/driven/tools/provider.py` owns the registry and its NO AUTO-DISCOVERY rule;
# this is that registry plus the second vertical, which was implemented, tested and shipped
# with a profile naming it - and registered nowhere. `fraud_analyst` therefore parsed,
# received a version, and died at `StartTurn` step 3 with `UnknownToolsetError` on EVERY
# turn, while `tests/unit/test_fraud_vertical.py` stayed green by building its own provider.
# The sixth instance of a collaborator only the tests supplied (docs/STATE.md).
#
# STILL A HAND-WRITTEN LIST, AND THAT IS STILL THE SECURITY PROPERTY. Composing the two
# mappings here rather than editing `DEFAULT_TOOL_PACKAGES` keeps one line per vertical and
# keeps discovery out; what it costs is that a reader has to look in two places, which is
# the price of the wave rule that a task writes only the files it was given. If both ever
# live in one place again, it is `provider.py`'s registry that should hold them.
TOOL_PACKAGES: Mapping[str, ToolsetBuilder] = {
    **DEFAULT_TOOL_PACKAGES,
    "fraud": fraud_tools.build_toolset,
    PEER_TOOLSET: peer_tools.build_toolset,
}


class UnservableProfileError(RuntimeError):
    """A profile loaded and names a toolset this process cannot resolve. t-f11-05.

    RAISED AT LOAD, WHICH IS THE WHOLE POINT. The same profile used to build a container,
    take a turn, run the compaction ladder and reach `StartTurn` step 3 before anything
    complained - past the point where the model had been paid for, in a message that named
    a toolset and left the operator to work out which profile had asked for it.

    A profile that parses is not a profile that works. This is the difference.
    """


class MissingDatabaseError(RuntimeError):
    """The app database is not there, and no admin URL was configured to create it.

    t-f11-02. Carries the exact command instead of the driver's `connection failed` three
    layers down, because the operator reading it is the one who has to type it.
    """


def _read_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines from a credentials file. A missing file is an empty mapping.

    t-f11-01. THE PROCESS NEVER READ THIS FILE. Only tests did, which is exactly why every
    credential had to be exported by hand at a terminal before anything would start, and
    exactly why nobody noticed: the suite read `.env` for itself, so the gap only existed
    for a human.

    NOTHING HERE IS LOGGED, PRINTED, OR PUT IN AN EXCEPTION MESSAGE. Every value this
    returns is a secret by assumption. A malformed line is SKIPPED rather than raised on,
    because the only honest way to report one would be to quote it.

    Deliberately a few lines rather than a dependency: this is `KEY=value`, comments, blank
    lines and optional surrounding quotes. `export KEY=value` is accepted because the file
    is also what an operator sources by hand. What is NOT supported is interpolation,
    multi-line values or command substitution - a credentials file that can compute is a
    credentials file that can be made to do something.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Missing, unreadable, a directory - none of them is an error. Production sets real
        # environment variables and ships no file at all.
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        name, separator, value = stripped.partition("=")
        name = name.strip()
        if not separator or not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


# t-f11-23. A NAME THIS REPOSITORY'S `.env` USES THAT A LIBRARY OUTSIDE OUR CONTROL READS
# UNDER A DIFFERENT NAME - {name the file carries: name the library actually reads}.
#
# THE DEFECT THIS MAPS AROUND: `from_env` layers `.env` under the real process environment
# for Settings' OWN FIELDS ONLY and exports nothing (see that method's docstring). litellm's
# per-provider config resolves MiniMax's credential by reading `os.environ["MINIMAX_API_KEY"]`
# directly when it builds a client (docs/FIELD-NOTES.md) - a path that never goes through
# `Settings` at all, however faithfully `from_env` merges the file. This repository's `.env`
# carries the same value under `MINIMAX_API`, so a machine with a complete credentials file
# still could not make a model call until a human exported `MINIMAX_API_KEY` by hand at a
# terminal - the second row of docs/ROADMAP.md's F11 table.
#
# ADD A PAIR HERE ONLY WHEN A LIBRARY READS A NAME THE FILE DOES NOT ALREADY SPELL THE SAME
# WAY. `TELEGRAM_BOT` and `GEMINI_API_KEY` need no entry: `Settings.from_env` and the library
# that reads each one already agree on the name, so there is nothing to rename.
_MAPPED_CREDENTIAL_NAMES: Final[Mapping[str, str]] = {"MINIMAX_API": "MINIMAX_API_KEY"}


def _export_mapped_credentials(env: Mapping[str, str]) -> None:
    """Put a mapped credential where the library that actually reads it will look. t-f11-23.

    `env` is `from_env`'s own merged mapping - the credentials file layered under the real
    process environment - so this runs once, in the one place the two layers already come
    together, rather than adding a second opinion about where a value came from.

    A REAL EXPORTED TARGET VARIABLE ALWAYS WINS AND IS NEVER OVERWRITTEN. If
    `MINIMAX_API_KEY` is already in `os.environ`, this does nothing for it: an operator who
    exported it themselves said something, and a file silently filling a name that is
    already set would repoint a process the operator configured on purpose - the same
    ordering rule `from_env` itself keeps between the file and the environment.

    NOTHING HERE PRINTS, LOGS, OR PUTS A VALUE IN AN EXCEPTION MESSAGE. `os.environ` is the
    only thing this ever touches, and only when the target name is genuinely absent from it.
    """
    for file_name, wire_name in _MAPPED_CREDENTIAL_NAMES.items():
        if wire_name in os.environ:
            continue
        value = env.get(file_name)
        if value:
            os.environ[wire_name] = value


# t-f2-12. THE DBOS APPLICATION VERSION, PINNED. READ `dbos_config` BEFORE CHANGING IT.
#
# This is a constant of the SOURCE, not of a deployment, and that is the entire property:
# DBOS recovers a PENDING workflow only when the row's `application_version` equals the one
# the recovering process claims, so the value has to survive a deploy that changed the code.
# Left unset, DBOS computes an md5 of the registered workflow sources - which changes the
# moment `turn_workflow.py` changes - and a worker killed mid-turn then leaves a row nobody
# will ever recover, on a partition nothing else can use.
#
# BUMP IT DELIBERATELY, AND ONLY FOR THE ONE REASON NAMED IN `dbos_config`: a deploy whose
# workflow signature or step sequence is incompatible with the workflows already in flight.
PINNED_DBOS_APPLICATION_VERSION: Final[str] = "agent-core-turns.1"

# The DBOS application name. It is hashed into the default version and, more importantly,
# it is a predicate of the recovery query itself (`_sys_db.get_pending_workflows` filters
# on `application_name`), so two deployments sharing one system database do not recover
# each other's work - and one deployment that renames itself stops recovering its own.
_DBOS_APP_NAME = "agent-core"

# What `dbos_config` returns. Deliberately a plain mapping and NOT `dbos.DBOSConfig`:
# `dbos` is a banned import outside `adapters/driving/workflow/` (Core/pyproject.toml,
# flake8-tidy-imports - the mechanical form of CLAUDE.md non-negotiable #1) and this file is
# not that package. Every key below is one `DBOSConfig` declares, so whoever launches passes
# it straight through; the type name is the only thing that does not cross the ban.
DbosConfig = dict[str, str]


@dataclass(frozen=True)
class Settings:
    """Everything the wiring needs from outside the process.

    Read from the environment by `Settings.from_env()`, never read from the environment
    piecemeal further down. A second `os.environ` lookup somewhere in the tree is how a
    deployment ends up with two different opinions about which database it is using.

    t-f11-01 keeps that true while adding the credentials FILE. `from_env` layers the
    repository's `.env` UNDER the real environment and reads both through one mapping, so
    there is still exactly one place in this tree that looks at where the process is
    running - and a real environment variable still always wins.
    """

    app_conninfo: str = _DEFAULT_APP_CONNINFO
    # t-f11-02. OPTIONAL, AND THE REASON IT IS OPTIONAL IS THE POINT.
    #
    # `start_container` deliberately did not create databases, because a deployment that
    # hands its application superuser credentials has a bigger problem than a missing
    # table. That reasoning survives: present, startup runs `ensure_databases` and then
    # migrates; absent, startup migrates only and a missing database fails naming the exact
    # command (`MissingDatabaseError`). Development leaves it in `.env` and gets one
    # command; production sets it for one bootstrap run and removes it. THE APP NEVER
    # REQUIRES SUPERUSER TO RUN.
    admin_conninfo: str | None = None
    # `None` means "the app database". The audit tables live alongside the domain tables
    # on day 1; what must be separate is the POOL, not the database - see `build_pools`.
    audit_conninfo: str | None = None
    profiles_dir: Path = field(default_factory=lambda: _CORE_ROOT / "profiles")
    # `Core/skills/<name>/SKILL.md`, the root `FilesystemSkillRegistry` proves containment
    # against. Overridable for the same reason `profiles_dir` is: an installed wheel has no
    # `Core/` above it.
    skills_dir: Path = field(default_factory=lambda: _CORE_ROOT / "skills")
    # t-f3-20. Where `FilesystemMediaStore` keeps its content-addressed files. It has the
    # same override reasoning as the two directories above, and one of its own: media is
    # the only state in this system that is NOT in Postgres, so a deployment that puts it
    # on a mounted volume needs to say where without editing the source.
    media_dir: Path = field(default_factory=lambda: _CORE_ROOT / "media")
    # t-f11-03. `Core/policy/*.yaml` - the reviewed rules `start_container` reconciles
    # `policy_rules` against on every start. Its ABSENCE is meaningful and is not the same
    # as its being empty: a directory that is not there means this deployment does not
    # manage policy from files and startup touches the table not at all, while an existing
    # but empty directory is a reviewer saying "no rules", which revokes everything. See
    # `start_container`. Overridable for the same reason the three directories above are.
    policy_dir: Path = field(default_factory=lambda: _CORE_ROOT / "policy")
    # Day 1 LiteLLM runs in library mode and this stays None. Day 2 sets the proxy URL
    # and, per ports/model_gateway.py, that is meant to be the entire code change.
    proxy_base_url: str | None = None

    # t-f3-06: the channel registry's raw material. `None` on any of these means that
    # channel is simply absent from the registry - see `build_channel_registry` - never
    # wired with a placeholder that would only fail the first time it tried to send.
    #
    # `telegram_bot_token` reads the same env var name this deployment's bot token
    # already carries (`TELEGRAM_BOT`, unprefixed) rather than an `AGENT_CORE_`-prefixed
    # invention: it names a real external credential, the same way `MINIMAX_API_KEY`
    # does for the model gateway (docs/FIELD-NOTES.md).
    telegram_bot_token: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    # One tenant and one profile per channel adapter instance (see `telegram.py`'s and
    # `whatsapp.py`'s own docstrings on why): day 1 is a single deployment, so both
    # channels share the one binding rather than each inventing its own field.
    channel_tenant_id: TenantId = TenantId("default")
    channel_profile_id: str | None = None

    # t-f3-15. Which registered channel `ChannelHumanGateway` asks approvals on. `None`
    # resolves to `_HTTP_CHANNEL_ID` in `build_container` - the one channel always
    # registered - rather than to a push channel that may not be configured, so an
    # unconfigured deployment still builds a working (if silent-to-nobody) gateway
    # instead of failing to construct one. Approvals actually reaching a human is a
    # deployment choice, made by setting this to a channel id `build_channel_registry`
    # has wired.
    human_gateway_channel_id: str | None = None

    # t-f2-12. What the durable engine is told about itself. See `dbos_config`.
    #
    # `dbos_application_version` defaults to the SOURCE constant on purpose: a pin that
    # lived only in the environment is a pin every deployment can forget to set, and
    # forgetting it silently restores the defect. The environment override exists for the
    # deliberate bump described in `dbos_config`, and for a deployment that runs two
    # incompatible builds side by side on purpose.
    dbos_app_name: str = _DBOS_APP_NAME
    dbos_application_version: str = PINNED_DBOS_APPLICATION_VERSION
    # The OTHER fix, and it is opt-in. `None` means local, version-scoped recovery, which
    # is what the pin above makes safe.
    dbos_conductor_key: str | None = None

    @property
    def resolved_audit_conninfo(self) -> str:
        return self.app_conninfo if self.audit_conninfo is None else self.audit_conninfo

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: Path | None = None,
    ) -> Settings:
        """Everything from outside the process, resolved once. t-f11-01.

        TWO LAYERS, AND THE ORDER IS THE WHOLE CONTRACT
            `environ` (the real environment) wins over `env_file` (the credentials file a
            fresh clone gets), always. A file that could override an exported variable would
            silently repoint a production process at whatever a developer left in a working
            copy, and the symptom would be a deployment reading the wrong database.

            A missing file is not an error - production sets real variables and ships none.

        This stays the ONE place in the tree that looks at where the process is running, so
        the class docstring's rule holds: the file is read here, merged here, and nothing
        below ever sees two opinions about which database it is using. NO VALUE READ HERE IS
        EVER LOGGED OR ECHOED - see `_read_env_file`.

        t-f11-23: THIS IS ALSO WHERE A CREDENTIAL NAMED FOR OUR FILE BECOMES VISIBLE TO A
        LIBRARY NAMED FOR ITS OWN. `MINIMAX_API` (the name `.env` carries) is exported to
        `os.environ` as `MINIMAX_API_KEY` (the name litellm reads) whenever the target name
        is not already set - see `_MAPPED_CREDENTIAL_NAMES` and `_export_mapped_credentials`
        for the full reasoning and the one rule that protects an operator's own export.
        """
        fallback = _read_env_file(_DEFAULT_ENV_FILE if env_file is None else env_file)
        process = os.environ if environ is None else environ
        env: Mapping[str, str] = {**fallback, **process}
        _export_mapped_credentials(env)
        profiles_dir = env.get("AGENT_CORE_PROFILES_DIR")
        skills_dir = env.get("AGENT_CORE_SKILLS_DIR")
        media_dir = env.get("AGENT_CORE_MEDIA_DIR")
        policy_dir = env.get("AGENT_CORE_POLICY_DIR")
        return cls(
            app_conninfo=env.get("AGENT_CORE_DATABASE_URL", _DEFAULT_APP_CONNINFO),
            admin_conninfo=env.get("AGENT_CORE_ADMIN_DATABASE_URL"),
            audit_conninfo=env.get("AGENT_CORE_AUDIT_DATABASE_URL"),
            profiles_dir=Path(profiles_dir) if profiles_dir else _CORE_ROOT / "profiles",
            skills_dir=Path(skills_dir) if skills_dir else _CORE_ROOT / "skills",
            media_dir=Path(media_dir) if media_dir else _CORE_ROOT / "media",
            policy_dir=Path(policy_dir) if policy_dir else _CORE_ROOT / "policy",
            proxy_base_url=env.get("AGENT_CORE_LITELLM_BASE_URL"),
            telegram_bot_token=env.get("TELEGRAM_BOT"),
            whatsapp_access_token=env.get("AGENT_CORE_WHATSAPP_ACCESS_TOKEN"),
            whatsapp_phone_number_id=env.get("AGENT_CORE_WHATSAPP_PHONE_NUMBER_ID"),
            channel_tenant_id=TenantId(env.get("AGENT_CORE_CHANNEL_TENANT_ID", "default")),
            channel_profile_id=env.get("AGENT_CORE_CHANNEL_PROFILE_ID"),
            human_gateway_channel_id=env.get("AGENT_CORE_HUMAN_GATEWAY_CHANNEL_ID"),
            dbos_app_name=env.get("AGENT_CORE_DBOS_APP_NAME", _DBOS_APP_NAME),
            dbos_application_version=env.get(
                "AGENT_CORE_DBOS_APPLICATION_VERSION", PINNED_DBOS_APPLICATION_VERSION
            ),
            # `DBOS_CONDUCTOR_KEY`, unprefixed, because it names a real external
            # credential issued by DBOS - the same reasoning `telegram_bot_token` gives
            # for reading `TELEGRAM_BOT` rather than inventing an `AGENT_CORE_` name.
            dbos_conductor_key=env.get("DBOS_CONDUCTOR_KEY"),
        )


def dbos_config(settings: Settings | None = None) -> DbosConfig:
    """What a process must hand DBOS so a killed worker cannot silence a session forever.

    docs/TASKS.md#t-f2-12. Nothing in `Core/src` constructed a DBOS configuration at all
    before this function, which is not a missing convenience - it is the defect.

    THE DEFECT, BECAUSE THE FIX IS UNREADABLE WITHOUT IT
        `agent-core-turns` is partitioned with `partition_concurrency=1` (t-f2-03). On the
        installed dbos 2.31.1 the partitioned-dequeue mutual-exclusion probe in `_sys_db.py`
        is UNSCOPED BY DESIGN: a PENDING row occupies its partition whatever
        `application_version` wrote it. Startup recovery in `_dbos.py` is the opposite - it
        asks `get_pending_workflows(executor_id, GlobalParams.app_version)`, scoped to the
        version this process claims. Unset, that version is an md5 of the registered
        workflow sources, so EVERY deploy is a new version.

        A worker killed mid-turn therefore leaves a row the next deploy will never recover,
        holding the only slot that session has. Later turns are accepted, enqueued, and
        never dequeued. Nothing raises. It reads as "the turn never started", and the only
        report is a customer saying the assistant stopped answering.

    THE FIX WIRED HERE: A PINNED VERSION
        `application_version` is `PINNED_DBOS_APPLICATION_VERSION`, a constant of this
        source. The row a dying worker leaves then names a version the next process still
        claims, recovery picks it up at launch, the workflow finishes, and the slot is
        released. `executor_id` is left alone deliberately: DBOS defaults it to `"local"`
        (`DBOS__VMID`), which is already stable across restarts, and pinning a version is
        worth nothing if the executor half of the same predicate moves instead.

    THE PRICE, WHICH SOMEBODY HAS TO KNOW
        DBOS uses this version to decide whether a recovered workflow's code is the code
        that wrote it. Pinning it removes that check: an incompatible deploy WILL recover
        an in-flight workflow into changed code, and a workflow replaying against a
        different step sequence is the crash-recovery bug CLAUDE.md's silent-bug table
        warns about - visible only on recovery, never in a green suite.

        So the rule is a human one and there is no way to automate it away: **bump
        `PINNED_DBOS_APPLICATION_VERSION` deliberately whenever a change to
        `run_turn_workflow` is incompatible with the turns already in flight** - a step
        added, removed or reordered, or a changed workflow argument. Bumping it strands the
        old rows exactly as the default does, which is the correct outcome there: those
        workflows must not resume into this code. Editing a step's INSIDES is not a bump;
        changing the body's decision sequence is.

    THE OTHER CONFIGURATION, AND WHEN TO SWITCH TO IT
        A recovery service - `conductor_key`, or DBOS Cloud - recovers pending workflows
        centrally and `_dbos.py` skips the local, version-scoped recovery entirely (it also
        mints a per-process `executor_id`, which is why the two are not additive strategies
        so much as two answers to the same question). That is the better answer the day
        this runs as more than one replica: a dead POD's rows are then recovered by whoever
        is alive, rather than by the same executor id coming back, and the version check
        stops being something a human has to remember. It costs an external dependency and
        a credential, which a single-process day-1 deployment should not take on. Set
        `DBOS_CONDUCTOR_KEY` and it travels below; nothing else has to change.

    Returns a plain mapping, not `dbos.DBOSConfig` - see `DbosConfig` for why the type name
    cannot cross the import ban. The launch itself is not here and cannot be: `dbos` is
    confined to `adapters/driving/workflow/`, and nothing under it exposes a bootstrap yet
    (`main.py` says so in its own docstring). What this file owns is the VALUE, so that
    whoever gains a launch takes it from the composition root instead of assembling a
    second opinion beside it.
    """
    resolved = Settings.from_env() if settings is None else settings

    config: DbosConfig = {
        "name": resolved.dbos_app_name,
        # DBOS derives its system database from this URL by appending `_dbos_sys`, so the
        # durable state lands beside the app database this container was built with rather
        # than on a second connection string that could drift from it.
        "database_url": resolved.app_conninfo,
        "application_version": resolved.dbos_application_version,
    }
    if resolved.dbos_conductor_key:
        config["conductor_key"] = resolved.dbos_conductor_key
    return config


@dataclass(frozen=True)
class PoolConnections:
    """A `ConnectionFactory` bound to exactly one pool.

    A named object rather than a lambda, because which pool a sink writes through is the
    single most consequential fact in this file (CLAUDE.md non-negotiable #6) and a
    closure hides it. This way the binding is a value a reader - and a test - can look at.
    """

    pool: Any

    def __call__(self) -> AbstractContextManager[Any]:
        return self.pool.connection()  # type: ignore[no-any-return]


# Injectable so the wiring can be exercised without a database. Production passes
# `_unopened_pool`; a test passes a recording stand-in and can then assert WHICH pool an
# adapter checked out, which a comparison of two pool objects cannot prove.
PoolFactory = Callable[[str], Any]


def _unopened_pool(conninfo: str) -> ConnectionPool[Any]:
    """A pool that has not connected to anything yet.

    `open=False` keeps `build_container` free of I/O: the container is assembled, then
    whoever runs it opens the pools. Connecting during construction would make an
    unreachable database a construction error rather than a runtime one, and the process
    could no longer start to report why it cannot work.
    """
    return ConnectionPool(conninfo, open=False)


def load_profiles(
    profiles_dir: Path, registry: ProfileVersionRegistry | None = None
) -> dict[str, AgentProfile]:
    """Every `*.yaml` under `profiles_dir`, keyed by profile id and version-assigned.

    Versions are assigned HERE, at load, because `AgentProfile.version` is 0 until a
    registry says otherwise (D20) and `PgConversationStore.append_request` refuses to
    persist an unversioned profile. Loading in file-name order keeps the numbering
    reproducible across processes; directory iteration order is not.

    A duplicate `id` across two files raises rather than letting the later file win: the
    winner would depend on file names, and it decides what an agent is permitted to do.

    The reading and the YAML parsing belong to `adapters/driven/profiles_fs/`, never to
    `AgentProfile` - see that module's docstring. `load_profile_sync` rather than the async
    `load_profile` because this runs at startup, before there is an event loop to protect.
    """
    assign = ProfileVersionRegistry() if registry is None else registry
    profiles: dict[str, AgentProfile] = {}

    for path in sorted(profiles_dir.glob("*.yaml")):
        profile = assign.assign(_grant_peer_mechanism(load_profile_sync(path)))
        if profile.id in profiles:
            raise ValueError(
                f"Two profile files declare id {profile.id!r}; {path.name} is the second. "
                "Refusing to pick one: the loser's permissions would vanish silently."
            )
        profiles[profile.id] = profile

    return profiles



def _grant_peer_mechanism(profile: AgentProfile) -> AgentProfile:
    """`peers.enabled` resolves to the `ask_peer` tool. t-f11-34.

    THE GAP THIS CLOSES, AND WHY IT SURVIVED SIX F9 ANCHORS
        `support_triage.yaml` names `billing_specialist` with a two-sided allowlist and a
        hop limit, the loader reads all of it (t-f11-14), and the agent resolved to no
        `ask_peer` tool at all - so the model was never offered a way to ask, and every
        A2A mechanism in the tree (t-f9-03 .. t-f9-09, t-f11-15) was reachable only from a
        unit test that built its own provider. "One agent orchestrating another is a YAML
        change, not a code change" was the claim; nothing in the shipped process made it
        true.

    THE DECLARATION IS THE `peers:` BLOCK, NOT A TOOLSET NAME, AND THAT IS DELIBERATE
        A profile that wrote `toolsets: [peers]` and left `peers.enabled` false would hold
        a tool whose every call `hop_limit.authorise_hop` refuses, and a profile that
        declared a peer and forgot the toolset line is exactly the state this anchor found.
        One switch, in the block that already carries the allowlist, the hop limit and the
        visibility - so the two cannot disagree.

    IT GRANTS A NAME, NOT A TOOLSET OBJECT, AND THAT IS THE OTHER HALF
        `PEER_TOOLSET` is registered in `TOOL_PACKAGES`, so what comes back from
        `ToolProvider.tool_names_for` is resolved by the same registry lookup every other
        tool goes through - visible in `:profile`, in `:tools`, in `preflight`'s servability
        check and in the audit record. Composing the toolset onto the provider's answer
        instead would have granted the same capability and shown it in none of them.

    Appended rather than prepended: `toolsets` order is meaningful (domain/profile.py) and
    it decides the order of the flat name list a human reads in the trail, so the profile's
    own packages keep coming first. Already-named is left alone - a second entry would
    build `ask_peer` twice and `LocalToolProvider._resolve` would refuse the profile for a
    collision with itself.
    """
    if not profile.peers.enabled or PEER_TOOLSET in profile.toolsets:
        return profile
    return replace(profile, toolsets=(*profile.toolsets, PEER_TOOLSET))


def _refuse_unservable_profiles(
    profiles: Mapping[str, AgentProfile], packages: Mapping[str, ToolsetBuilder]
) -> None:
    """Every loaded profile must resolve every toolset it names. t-f11-05.

    THIS IS A LOAD-TIME CHECK OF A RUN-TIME FAILURE, ON PURPOSE
        `LocalToolProvider._resolve` already refuses an unregistered name, loudly and with
        a good message - but it refuses on the turn, inside `StartTurn` step 3, which is
        after the container built, after the turn was accepted and enqueued, and after the
        model was paid for. `fraud_analyst` shipped in exactly that state: it parsed, it
        was assigned a version, it appeared in the console's profile list, and it could not
        take a single turn.

        So the same question is asked here, where the answer costs nothing: at startup,
        over every profile at once, naming all of them rather than whichever one a user
        happened to pick first.

    IT CHECKS TOOLSETS AND NOT MCP SERVERS, AND THAT IS A DIFFERENT ANSWER SINCE t-f11-28.
    An MCP server is not a registration this process either has or lacks - it is a remote
    thing that may be down for ten seconds. `build_tool_provider` now composes them
    (adapters/driven/tools/provider.py), and an unreachable one is a DEGRADED start: the
    profile loses that server's tools, keeps its local ones, and the process comes up. So
    there is nothing for a load-time check to refuse. Reachability is a question for
    `adapters/driving/cli/preflight.py`, which asks it without stopping anything.

    Resolved against the registry KEYS rather than by building the toolsets, because
    building them is `toolset_for`'s async job and `build_container` performs no I/O
    (NOTHING HERE CONNECTS). A name that is registered always builds; a name that is not is
    the whole of this failure.
    """
    known = set(packages)
    unservable = {
        profile_id: sorted(set(profile.toolsets) - known)
        for profile_id, profile in sorted(profiles.items())
        if set(profile.toolsets) - known
    }
    if not unservable:
        return

    registered = ", ".join(sorted(known)) or "<nothing registered>"
    detail = "; ".join(
        f"{profile_id!r} names {', '.join(repr(name) for name in missing)}"
        for profile_id, missing in unservable.items()
    )
    raise UnservableProfileError(
        f"these profiles name toolsets nothing registered: {detail}. Registered: "
        f"{registered}. A profile that parses is not a profile that works - unregistered, "
        "every turn under it dies at StartTurn step 3 with UnknownToolsetError "
        "(docs/TASKS.md#t-f11-05). Register the package in composition.TOOL_PACKAGES, or "
        "stop shipping the profile."
    )


@dataclass(frozen=True)
class Container:
    """The wired application. Built once at startup, read from everywhere after.

    `domain_pool` and `audit_pool` are both exposed on purpose. They are not an
    implementation detail of the adapters that hold them: that they are two objects is
    the invariant non-negotiable #6 asks for, and an invariant nobody can see is an
    invariant nobody checks.
    """

    settings: Settings
    domain_pool: Any
    audit_pool: Any
    model: LiteLLMGateway
    policy: PgToolPolicy
    store: PgConversationStore
    audit: PgAuditSink
    # One runner for the process. `AgentRunner.run` takes the `turn_id` as its first
    # parameter (docs/TASKS.md#t-f1-05), so whoever runs a turn passes the id it generated
    # inside its DBOS step and nothing here is per-turn. A container-level binding would
    # have been the same id for every turn in the process; there is no longer one to make.
    runner: PydanticAgentRunner
    tools: LocalToolProvider
    context: LadderContextEngine
    skills: FilesystemSkillRegistry
    # t-f3-20. `ResumeTurn` takes a `MediaStore` because an evidence request resolves to a
    # `MediaRef` on the same code path an approval takes (D9), so the container could not
    # build the use case without choosing one. Exposed alongside it for the reason every
    # other adapter here is: a second store built beside this one would address a
    # different directory, and a turn resumed on evidence nobody can read fails nowhere
    # near the mistake.
    media: FilesystemMediaStore
    profiles: dict[str, AgentProfile]
    profile_versions: ProfileVersionRegistry
    # The use case, wired from the eight adapters above. Exposed alongside them rather than
    # instead of them: a driving adapter runs turns through `start_turn`, while a test or
    # an operator tool still needs to reach one collaborator without rebuilding the world.
    start_turn: StartTurn
    # t-f3-20. The other half of a turn's life, and until this field existed there was no
    # half at all: `_step_resume` resolves `ResumeTurn` off the workflow's bound
    # dependencies, nothing built one, and every answered suspension died on
    # `TurnWorkflowNotWiredError` naming this exact seat. It shares `runner`, `store`,
    # `audit` and `profiles` with `start_turn` above - one wiring, so the continuation is
    # policed by the rules the turn started under and audited through the same sink.
    resume_turn: ResumeTurn
    # t-f3-06 (D23 Shape A): one lookup table, shared by `ChannelHumanGateway`'s mid-turn
    # prompt and the workflow's end-of-turn `_step_deliver` (t-f3-07). `ChannelRegistry`,
    # never a plain `dict` or `Mapping` - see `build_channel_registry`'s docstring for
    # why the type itself, not just its contents, is the point.
    channels: ChannelRegistry
    # t-f3-15. The same `ChannelHumanGateway` instance `decide_approval` holds - exposed
    # alongside it, not instead of it, for the reason every other seat is: whoever wires
    # `POST /decisions/{corr_id}` (t-f3-11) or `_step_publish` needs a `HumanGateway` too,
    # and building a second one beside this container's would let the two disagree about
    # which channel a human is asked on.
    human_gateway: ChannelHumanGateway
    # t-f3-15 / D25. Nothing constructed this before: the requester seat had no binder,
    # so the four-eyes rule (D25) could never fire and the approval route had no use case
    # to call. See `_pg_requester_lookup` below for the `requester` seat, and t-f11-19's
    # note above `build_container` for `signal` - neither needed a port change.
    decide_approval: DecideApproval
    # t-f0-06. `routes.py`'s `lookup_turn` seat, bound to `_pg_turn_lookup` - see that
    # function's docstring. Exposed alongside `decide_approval` for the identical reason:
    # `main.py` hands both to `routes.create_app`, and nothing else in the process needs
    # to reach it.
    turn_lookup: TurnLookup
    # t-f7-08 / t-f7-11. The three seats `POST /evidence/{corr_id}` needs, and all three
    # were empty: the route answered 503 in every process `serve()` started while
    # `tests/integration/test_evidence_upload.py` stayed green by injecting its own. That
    # is the SIXTH time docs/STATE.md has recorded this shape, which is why
    # `tests/unit/test_composition.py` now derives the seat list from the declarations
    # instead of anyone remembering to add a field here.
    #
    # `ingest_media` holds the container's OWN `media` store and `audit` sink - a second
    # store built beside this one would address a different directory, and a turn resumed
    # on evidence nobody can read fails nowhere near the mistake.
    ingest_media: IngestMedia
    resolve_evidence: EvidenceCorrelate
    signal_evidence: EvidenceSignal
    # t-f11-18. The read half of the trail, on the AUDIT pool - see `build_container`.
    # Until this seat existed `main.py` hand-built a driven adapter of its own
    # (`_PgToolCallLog`) with its own SQL over the same table, because the console needed
    # one question answered and the container could not answer it. That made `main.py` the
    # only module outside this file that chose a driven adapter, which is the hole the
    # single-composition-root rule exists to prevent.
    audit_reader: PgAuditReadRepository
    # t-f11-33. The read side of the conversation, and the seat `main.py` did not have.
    # `build_app` built a `PgTranscriptReader` of its own from this container's domain
    # pool and said so in a comment marked "this is the debt": it made `main.py` the only
    # module outside this file that chose a driven adapter. Worse, `build_console` could
    # not do the same trick usefully - it simply passed nothing - so `:sessions` and
    # `:trace` answered "this console has no transcript reader wired" in every process
    # `console` ever started, while `tests/unit/test_console.py` stayed green by
    # constructing one itself.
    #
    # Typed on the PORT rather than on `PgTranscriptReader`, like `turn_lookup` and the two
    # evidence seats above: both the router and the console take `TranscriptReader`, and a
    # container field that named the adapter would be the one place a swap had to be
    # remembered.
    transcripts: TranscriptReader
    # t-f11-33. The other adapter `main.py` hand-built, retired by the same seat. The
    # admin router is the only consumer, and non-negotiable #8 is why it is a separate
    # field and not something an agent can reach: `KnowledgeAdmin` is never injected into
    # anything a turn touches, and there is no field here that would carry it there.
    knowledge_admin: KnowledgeAdmin
    # t-f11-34. The durable queue a peer ask leaves on (migration 0014). Typed on the port
    # because t-d2-04's A2A adapter is an adapter SWAP - `A2AAgentMailbox` implements the
    # same protocol over HTTP - and a container field naming `PgAgentMailbox` would put
    # that swap out of reach of the one file that is supposed to make it.
    #
    # WHAT THIS SEAT DOES NOT YET REACH, NAMED RATHER THAN IMPLIED. The `ask_peer` tool is
    # now on the asking agent's surface and always defers (adapters/driven/tools/peers.py),
    # and `StartTurn` reports the suspension to the user without naming the peer (t-f9-06).
    # Nothing in production yet turns that deferred call INTO `AgentMailbox.ask()`, nor
    # claims the far side's queue: `TurnWorkflowDependencies` has no mailbox field to bind,
    # and that file is not this anchor's to write. So a peer ask suspends and waits. That
    # is one wiring away and it is visible from here, which is the difference between this
    # and the six waves where it was not.
    mailbox: AgentMailbox


def build_pools(
    settings: Settings, pool_factory: PoolFactory
) -> tuple[Any, Any]:
    """`(domain_pool, audit_pool)` - two pool objects, always, even on one database.

    THE SEPARATION IS THE POINT AND IT IS NOT ABOUT DATABASES
        Non-negotiable #6 asks that an audit write survive the rollback of the turn it
        describes. What decides that is which CONNECTION the write runs on, not which
        database it lands in: a second connection is a second transaction, and a second
        transaction commits on its own.

        So the two pools are built separately here even when `audit_conninfo` is None and
        both point at the same database. Collapsing them to one object the moment the
        connection strings match is the change that quietly reintroduces the bug, and it
        would look like a tidy-up in review.
    """
    return pool_factory(settings.app_conninfo), pool_factory(
        settings.resolved_audit_conninfo
    )


@dataclass(frozen=True)
class PullModeChannel:
    """A channel whose `send` does nothing BECAUSE the answer is retrieved, not pushed.

    READ THIS BEFORE "FIXING" THE EMPTY BODY
        This is not a placeholder and not a swallowed error. The HTTP surface is
        202-plus-poll (D23, docs/GAPS.md A4): `POST /turns` returns a turn id immediately
        and the caller comes back with `GET /turns/{turn_id}` for the result. The result is
        already persisted by `ConversationStore.append_outcome` before delivery is even
        reached, so by the time this runs the answer IS where its reader will look for it.
        There is no socket to push it down - the client is not holding one - and inventing
        a webhook to call would be a product decision nobody has made.

    WHY IT IS REGISTERED AT ALL, RATHER THAN LEFT OUT
        `routes.py` puts `_DEFAULT_CHANNEL` on the caller of every HTTP-started turn, and
        `_step_deliver` ends every finished turn by asking the registry for that id. An
        absent entry is `UnknownChannelError` - loud, correct, and fatal to EVERY
        `POST /turns` turn, at the last step, after the model has already been paid for.
        The registry's rule is that a miss means "an answer has nowhere to go"; here the
        answer has somewhere to go, so this is not a miss.

    WHY IT IS NOT A GENERIC "NULL CHANNEL"
        Named for the delivery mode it implements, not for the fact that it does nothing.
        A `NullChannel` would be reusable for exactly the case that must stay loud - a
        channel that genuinely cannot deliver - and the first person to register one for a
        push channel would silence a real failure with a class this file provided.
    """

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        """Deliver nothing, successfully. See the class docstring for why that is correct.

        Async and taking the full `Channel` shape because it IS a channel: the registry
        dispatches to it exactly as it dispatches to Telegram, and a step that had to know
        which of its channels were pull-mode would be D23 leaking back into the workflow.
        """
        return None


def _http_channel() -> tuple[str, Channel]:
    """Always registered - unlike the push channels, it needs no credential to work.

    Telegram and WhatsApp are absent when unconfigured because an unconfigured push
    channel cannot send. Pull mode has nothing to configure: the answer is fetched from
    the store the route already reads.
    """
    return (_HTTP_CHANNEL_ID, PullModeChannel())


def _telegram_channel(settings: Settings) -> tuple[str, Channel] | None:
    """`None` when no bot token or no bound profile is configured.

    A token with no `channel_profile_id` is left unwired rather than guessed at: which
    profile answers a Telegram message is the composition-time decision `telegram.py`'s
    docstring names, and there is no honest default for it.
    """
    if not settings.telegram_bot_token or not settings.channel_profile_id:
        return None
    return (
        TELEGRAM_CHANNEL_ID,
        TelegramChannel(
            bot_token=settings.telegram_bot_token,
            tenant_id=settings.channel_tenant_id,
            profile_id=settings.channel_profile_id,
            # `httpx.AsyncClient()` performs no I/O at construction - same "NOTHING HERE
            # CONNECTS" guarantee `_unopened_pool` gives the two Postgres pools above.
            poster=httpx.AsyncClient(),
        ),
    )


def _whatsapp_channel(settings: Settings) -> tuple[str, Channel] | None:
    """`None` unless both the access token and the phone number id are configured."""
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return None
    return (
        _WHATSAPP_CHANNEL_ID,
        WhatsAppChannel(
            access_token=settings.whatsapp_access_token,
            phone_number_id=settings.whatsapp_phone_number_id,
            client=httpx.AsyncClient(),
        ),
    )


def build_channel_registry(settings: Settings) -> ChannelRegistry:
    """t-f3-06: wire `t-f3-08`'s and `t-f3-09`'s concrete channels into `t-f3-13`'s
    registry - the shared lookup table `ChannelHumanGateway` and turn delivery both read.

    An unconfigured channel is simply ABSENT here, never wired with a placeholder that
    would only fail the first time something actually tried to send through it - the
    same "fail at construction, not at the first real use" reasoning
    `adapters/driving/channels/registry.py` gives for raising on a duplicate id at
    `ChannelRegistry.__init__` rather than at `get`. What this function adds on top of
    that: whatever comes back from here is `ChannelRegistry`, so a channel id nothing
    was configured for still raises `UnknownChannelError` on `get` - loudly, at the
    lookup, never a silent `None` for a caller three layers up to mistake for delivered.

    `http` is always here and the two push channels are conditional. That asymmetry is the
    difference between a channel that needs a credential to reach a platform and one whose
    delivery is the caller coming back for the stored result - see `PullModeChannel`.
    """
    pairs = (
        pair
        for pair in (
            _http_channel(),
            _telegram_channel(settings),
            _whatsapp_channel(settings),
        )
        if pair is not None
    )
    return ChannelRegistry(pairs)


# t-f3-15 / D25. The only place a turn's ORIGINAL caller survives to be read back by
# `turn_id` alone: `PgAuditSink.record_tool_call` (`adapters/driven/persistence_pg/
# audit_repository.py`) writes `caller.subject_id` into `audit_tool_calls` BEFORE the
# runner's `before_tool_execute` can raise (its own docstring's ordering, mirrored for
# the same reason) - and the `caller` it writes is `request.caller`, which is exactly the
# human D25 calls "the requester". A tool call that reached NEEDS_APPROVAL always has at
# least one such row: the row for the very call that suspended the turn.
#
# THIS IS DELIBERATELY NOT A NEW PgAuditSink METHOD.
#     D25 said so explicitly: "no port changes; the invariant holds." Adding a read member
#     to `AuditSink` for one lookup the composition root can already answer from a table
#     that already exists would be exactly the port change D25 decided against. The SQL
#     sits here, at the one seam allowed to hold it, rather than growing a second adapter
#     file for a single `SELECT`.
#
# `ORDER BY at ASC LIMIT 1` picks the EARLIEST row on purpose - the first tool call this
# turn ever attempted - though any row would answer the same name: `self._caller` is bound
# once per run (`PydanticAgentRunner.run`) and every row for one `turn_id` carries it.
_REQUESTER_LOOKUP_SQL = (
    "SELECT caller FROM audit_tool_calls WHERE turn_id = %s ORDER BY at ASC LIMIT 1"
)


def _requester_lookup_sync(conninfo: str, turn_id: TurnId) -> str | None:
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(_REQUESTER_LOOKUP_SQL, (str(turn_id),)).fetchone()
    return None if row is None else str(row[0])


def _pg_requester_lookup(conninfo: str) -> RequesterLookup:
    """Bind `DecideApproval`'s `RequesterLookup` seat to the audit pool's own data.

    D13: the psycopg call is synchronous and runs in a thread, exactly like every other
    adapter here. `None` (unknown requester) is a normal answer, not an error - D25 treats
    it as a refusal, and this function does nothing to soften that; it only reports what
    the table says.
    """

    async def lookup(turn_id: TurnId) -> str | None:
        return await asyncio.to_thread(_requester_lookup_sync, conninfo, turn_id)

    return lookup


# t-f0-06. The three `turns.state` values `PgConversationStore` writes
# (adapters/driven/persistence_pg/conversation_repository.py's `_TURN_STATE_*`
# constants), restated here for the same reason `_WHATSAPP_CHANNEL_ID` and
# `_HTTP_CHANNEL_ID` are: those names are private to that module, and reaching into them
# from the wiring would couple a driven adapter's internals to the composition root. If
# that module ever renames the states on its own, this restatement - not an import - is
# what a reviewer has to notice and update.
_TURN_STATE_STARTED = "started"
_TURN_STATE_SUSPENDED = "suspended"
_TURN_STATE_FINISHED = "finished"

_TURN_LOOKUP_STATUS: dict[str, TurnStatus] = {
    _TURN_STATE_STARTED: "running",
    _TURN_STATE_SUSPENDED: "waiting",
    _TURN_STATE_FINISHED: "finished",
}

# THE TENANT PREDICATE IS INSIDE THE QUERY, NOT APPLIED AFTER IT - `routes.py`'s own
# `TurnLookup` comment states the rule: a turn id names a conversation, ids leak (logs,
# screenshots, a shared support ticket), and unguessable is not private. A row for another
# tenant's turn_id simply does not match this WHERE clause, so it reads exactly like an
# unknown id - the same "404, never a guess" the route already promises.
_TURN_LOOKUP_SQL = """
    SELECT state, result
    FROM turns
    WHERE turn_id = %s AND tenant_id = %s
"""


def _turn_lookup_sync(
    conninfo: str, turn_id: TurnId, tenant_id: TenantId
) -> tuple[str, str | None] | None:
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(_TURN_LOOKUP_SQL, (str(turn_id), str(tenant_id))).fetchone()
    return None if row is None else (row[0], row[1])


def _pg_turn_lookup(conninfo: str) -> TurnLookup:
    """Bind `routes.py`'s `TurnLookup` seat to the row `PgConversationStore` writes.

    docs/TASKS.md#t-f0-06. `append_request` inserts the `turns` row before the model
    runs and `append_outcome` updates `state`/`result` on it when the turn suspends or
    finishes (`conversation_repository.py`) - the SAME row `StartTurn` writes through this
    container's OWN `store`, so a caller polling here reads back exactly what that write
    persisted, on the same conninfo (`resolved.app_conninfo`), never a second connection
    string that could drift from it.

    `text` stays `None` for anything that has not finished, per `TurnView`'s own
    docstring: a caller must see THAT a turn is still going without this route inventing
    an answer for what it is waiting on.

    D13: the psycopg call is synchronous and runs in a thread, exactly like
    `_pg_requester_lookup` beside it.
    """

    async def lookup(turn_id: TurnId, caller: CallerIdentity) -> TurnView | None:
        row = await asyncio.to_thread(
            _turn_lookup_sync, conninfo, turn_id, caller.tenant_id
        )
        if row is None:
            return None
        state, result_json = row
        status = _TURN_LOOKUP_STATUS.get(state)
        if status is None:
            # A `turns.state` value this file does not know about is a schema this
            # composition root has fallen behind, not an unknown turn - raising says so
            # loudly instead of reporting a plausible but wrong status.
            raise ValueError(f"turns.state {state!r} has no known TurnStatus mapping")
        text = (
            json.loads(result_json)["text"]
            if status == "finished" and result_json is not None
            else None
        )
        return TurnView(turn_id=turn_id, status=status, text=text)

    return lookup


# t-f7-08 / t-f7-11. THE EVIDENCE HANDLE, RESOLVED ONCE FOR THREE FACTS AT A TIME.
#
# `routes.py` declares two seats over one handle: `resolve_evidence` needs the turn and the
# profile an upload belongs to, and `signal_evidence` needs the deferred call's
# `ToolCallId`. All three are facts of the same row pair, so one statement answers them and
# the two closures below share it rather than each growing its own opinion of the
# correlation table.
#
# `kind = 'evidence'` IS A PRIVILEGE BOUNDARY, NOT A TIDY PREDICATE.
#     `HumanGateway.correlate` resolves ANY live handle - approval handles included - and
#     `turn_workflow.signal_evidence` sends `approved=True` (that function's docstring says
#     why that is not a decision anybody made). Bound to a bare `correlate`, this seat would
#     let an APPROVAL handle be answered by POSTing a file to `/evidence/{corr_id}`: the
#     deferred call resolves as approved, D25's four-eyes rule never runs, and
#     `AuditSink.record_human_decision` never writes the row that says who decided. Nothing
#     downstream can catch it - `_step_resume` deliberately narrows on `HumanAnswer` and not
#     on the kind the request was published with. This predicate is the only place the two
#     doors can be told apart, so it is where they are.
#
# THIS IS DELIBERATELY NOT A NEW `HumanGateway` MEMBER, for the reason
# `_REQUESTER_LOOKUP_SQL` above gives about `AuditSink`: the port answers one question and
# it answers it already. Widening it for an adapter-shaped join onto `turns` would be a port
# change nothing else needs, and a port answering two questions is cut wrong (CLAUDE.md).
#
# `expires_at > now()` is the DATABASE's clock, restated from `gateway.py`'s own
# `_CORRELATE_SQL` for the reason written there: two processes with drifting clocks must not
# disagree about whether a handle is still good.
_EVIDENCE_HANDLE_SQL = """
    SELECT hr.turn_id, hr.tool_call_id, t.profile_id
    FROM human_requests hr
    JOIN turns t ON t.turn_id = hr.turn_id
    WHERE hr.correlation_id = %s
      AND hr.kind = %s
      AND hr.expires_at > now()
"""


def _evidence_handle_sync(
    conninfo: str, correlation_id: str
) -> tuple[TurnId, ToolCallId, str] | None:
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            _EVIDENCE_HANDLE_SQL, (correlation_id, PendingKind.EVIDENCE.value)
        ).fetchone()
    if row is None:
        return None
    # `turn_id` comes back as a uuid.UUID from a UUID column; the domain type is the string
    # form every use case keys on - the same conversion `gateway.py` makes.
    return TurnId(str(row[0])), ToolCallId(str(row[1])), str(row[2])


async def _evidence_handle(
    conninfo: str, correlation_id: str
) -> tuple[TurnId, ToolCallId, str] | None:
    """D13: the psycopg call is synchronous and runs in a thread, like every other here."""
    return await asyncio.to_thread(_evidence_handle_sync, conninfo, correlation_id)


def _pg_evidence_correlate(
    conninfo: str, profiles: Mapping[str, AgentProfile]
) -> EvidenceCorrelate:
    """Bind `routes.py`'s `resolve_evidence` seat. docs/TASKS.md#t-f7-08.

    `None` for an unknown handle, an expired one, and one that names an approval rather
    than an evidence request - collapsed into a single answer on purpose, exactly as
    `HumanGateway.correlate` collapses unknown and expired. The route turns it into a 404
    and a stranger POSTing a guess learns nothing about which ids exist.

    A PROFILE THIS PROCESS CANNOT RESOLVE RAISES rather than answering `None`. The two are
    not the same fact: `None` says "that handle names nothing", and this says "the handle
    is real and the profile whose `MediaPolicy` would decide what may be uploaded is not
    loaded here". Answering 404 would tell a person their upload link had expired, while
    the actual state is a deployment holding a profile set that no longer covers a turn it
    is still running - and `IngestMedia` would have had no size or kind limit to enforce.
    The profile map is the container's OWN, the same one `store` and `start_turn` hold.
    """

    async def resolve(correlation_id: str) -> tuple[TurnId, AgentProfile] | None:
        resolved = await _evidence_handle(conninfo, correlation_id)
        if resolved is None:
            return None
        turn_id, _tool_call_id, profile_id = resolved
        profile = profiles.get(profile_id)
        if profile is None:
            raise KeyError(
                f"turn {turn_id!r} has a live evidence request and ran under profile "
                f"{profile_id!r}, which this process has not loaded. Refusing rather than "
                "ingesting an upload with no MediaPolicy to validate it against."
            )
        return turn_id, profile

    return resolve


def _pg_evidence_signal(conninfo: str) -> EvidenceSignal:
    """Bind `routes.py`'s `signal_evidence` seat. docs/TASKS.md#t-f7-11.

    THE SEAT TAKES A HANDLE AND THE SEND NEEDS A `ToolCallId`, so this is where the one
    becomes the other. `routes.EvidenceCorrelate` hands the route only `(TurnId, profile)`,
    which is why the send could not be keyed by the route itself and had to be a third
    seat - and it is why this closure reads the correlation row a SECOND time within one
    request. That second read is the cost of the current cut, not a correctness problem:
    the row is immutable once published, so both reads see the same pair.

    `turn_id` IS CHECKED, NOT TRUSTED. The route passes back the turn `resolve_evidence`
    gave it; if the handle now resolves to a different turn, something has rewritten the
    correlation table underneath a live request and waking the turn named by the weaker of
    the two values would resume somebody else's suspension on this person's file.

    IT RAISES WHEN THE HANDLE NO LONGER RESOLVES, and the message says the bytes are safe.
    This runs AFTER `IngestMedia` has stored the file, so there is no answer here that
    costs nothing: silence leaves the turn asleep until it expires three days later with
    nobody able to tell it was ever asked, and a raise is at least a trace an operator can
    act on. `DecideApproval.execute` lets a failing signal propagate for the same
    reason, and over the same row: the decision is on record and the turn has not woken.
    """

    async def signal(correlation_id: str, turn_id: TurnId, media: MediaRef) -> None:
        resolved = await _evidence_handle(conninfo, correlation_id)
        if resolved is None:
            raise LookupError(
                f"the evidence handle for turn {turn_id!r} no longer resolves to a live "
                "evidence request, so the turn waiting for this file cannot be woken. The "
                "upload IS stored - IngestMedia returned "
                f"{media.media_id!r} - and nothing about it has been lost."
            )
        resolved_turn_id, tool_call_id, _profile_id = resolved
        if resolved_turn_id != turn_id:
            raise ValueError(
                f"the evidence handle resolved to turn {resolved_turn_id!r} and the route "
                f"carried {turn_id!r}. Waking either one would resume a suspension on a "
                f"file uploaded against the other. The upload IS stored ({media.media_id!r})."
            )
        await durable_signal_evidence(turn_id, tool_call_id, media)

    return signal


# t-f11-19. THE SEAT THAT RECORDED A DECISION AND WOKE NOBODY, NOW BOUND.
#
# `_decision_signal` used to live here and it RAISED. `DecideApproval.execute` writes the
# audit row BEFORE it signals (its own docstring's ordering), so an approval was recorded,
# the caller was told it had been accepted, and the workflow waiting on
# `DBOS.recv_async(RESUME_TOPIC)` slept until it expired three days later. Raising was the
# right refusal at the time - a silent no-op would have been worse, because nothing about a
# 200 says the wake-up never happened - but it was still a route back that did not exist.
#
# WHY IT COULD NOT BE BOUND BEFORE, AND WHAT CHANGED
#     `DBOS.send_async` is banned in this file (pyproject.toml's TID251, the mechanical form
#     of CLAUDE.md non-negotiable #1), so the send has to be EXPORTED from
#     `adapters/driving/workflow/` for a composition root to bind - the same arrangement
#     `signal_evidence` already uses above. `turn_workflow.signal_decision` is that export
#     (t-f3-11) and it addresses `str(turn_id)`, which only became the workflow's own id
#     when t-f0-06 reconciled the two. Both halves now exist, so the seat is bound to the
#     function rather than to an apology.
#
# BOUND DIRECTLY, WITH NO WRAPPER. A closure here would be a second place that decides what
# an approval sends and how it is keyed; `signal_decision` owns the idempotency key
# (turn_id, tool_call_id) and the topic, and `signal_evidence` is bound the same way for the
# same reason.


def bind_turn_workflow(container: Container) -> None:
    """Hand the DBOS workflow adapter the collaborators its steps resolve through.

    `run_turn_workflow` is a MODULE-LEVEL object - that is how DBOS registers it - so its
    collaborators cannot travel as arguments: workflow arguments are serialised into the
    durable operation log and a use case is not serialisable. They are bound once, at
    startup, and `TurnWorkflowNotWiredError` already names this file as the place that does
    it. Until this call existed, the message was true about where the wiring BELONGED and
    false about where it happened, and every DBOS-started turn died on its first step.

    The workflow gets the container's OWN `start_turn` and `channels`, never a second pair
    built beside them. One process, one wiring: an HTTP turn and a DBOS turn that resolved
    different objects would enforce different policy rules and deliver on different
    channels, and nothing would say so.

    Deliberately a global side effect, deliberately called from exactly one place. A
    workflow that lazily constructed its own dependencies would be a second composition
    root, and the two would drift; this is the price of DBOS's module-level registration
    and it is paid here rather than spread out.

    The gateway travels the same way, and for a sharper reason than the other two
    (t-f3-17). `_step_publish` resolves `HumanGateway` off these dependencies, and the
    field defaults to `None` rather than to a no-op - so until this kwarg existed, every
    turn that suspended on a human died at the last step with `TurnWorkflowNotWiredError`
    naming this exact call. It is the container's OWN gateway, the same instance
    `decide_approval` holds: the turn that ASKS and the route that ANSWERS then share one
    correlation table by construction rather than by both happening to point at the same
    database.

    `resume_turn` IS THE SAME OMISSION ONE WAVE LATER (t-f3-20), AND IT IS THE MIRROR HALF
    OF THE GATEWAY ABOVE. `human_gateway` is how a turn ASKS; `resume_turn` is how the
    answer gets APPLIED. `_step_resume` resolves it off these dependencies and it defaults
    to `None` for the same reason - there is no correct silent behaviour for "a human
    answered and nothing happened" - so until this kwarg existed, every suspension that
    actually received a decision died at the moment of being answered, with
    `TurnWorkflowNotWiredError` naming this call. Every test of the step passed throughout,
    because each one bound the use case itself: a collaborator a test supplies and
    production never binds is the shape docs/STATE.md has now recorded five times.
    """
    bind_dependencies(
        TurnWorkflowDependencies(
            start_turn=container.start_turn,
            channels=container.channels,
            human_gateway=container.human_gateway,
            resume_turn=container.resume_turn,
        )
    )


def build_container(
    settings: Settings | None = None,
    *,
    pool_factory: PoolFactory = _unopened_pool,
) -> Container:
    """Choose the adapters and wire them. The only function that may do either.

    Deliberately does no I/O beyond reading the profile files: see NOTHING HERE CONNECTS
    in the module docstring.
    """
    resolved = Settings.from_env() if settings is None else settings

    domain_pool, audit_pool = build_pools(resolved, pool_factory)

    profile_versions = ProfileVersionRegistry()
    profiles = load_profiles(resolved.profiles_dir, profile_versions)
    # t-f11-05. Before anything else is wired: a profile that cannot resolve its toolsets
    # is refused HERE, naming the profile and the toolset, rather than on its first turn.
    _refuse_unservable_profiles(profiles, TOOL_PACKAGES)

    model = LiteLLMGateway(resolved.proxy_base_url)
    policy = PgToolPolicy(PoolConnections(domain_pool))
    # PoolConnections(audit_pool), never PoolConnections(domain_pool). This one line
    # is the whole of non-negotiable #6 in practice.
    audit = PgAuditSink(PoolConnections(audit_pool))
    # t-f11-18. The READ side of the same trail, through the SAME pool - never
    # `PoolConnections(domain_pool)`. The sink is on its own pool so an audit write
    # survives the rollback of the turn it describes (non-negotiable #6); a reader that
    # borrowed the domain pool would put every inspection tool back on the connection that
    # separation exists to keep it off, and it would read as a tidy-up in review.
    audit_reader = PgAuditReadRepository(PoolConnections(audit_pool))
    # t-f11-33. Both on the DOMAIN pool, and both were built in `main.build_app` until
    # now. A transcript and a knowledge document are domain state, not the append-only
    # trail, so neither takes the audit pool - the separation non-negotiable #6 asks for
    # is about the audit WRITE.
    transcripts = PgTranscriptReader(PoolConnections(domain_pool))
    knowledge_admin = PgKnowledgeAdmin(PoolConnections(domain_pool))
    # t-f11-34. `resolved.app_conninfo` - the same database `store` and `human_gateway`
    # use, because `peer_messages` (migration 0014) is domain state. It opens nothing
    # here: `PgAgentMailbox` takes a conninfo and connects per call, exactly as
    # `PgConversationStore` does (NOTHING HERE CONNECTS).
    mailbox = PgAgentMailbox(resolved.app_conninfo)
    # `provider.py`'s registry plus the second vertical - see `TOOL_PACKAGES` above for why
    # the composition happens here and why the list is still hand-written. t-f11-28: this
    # constructor also supplies the MCP half, so a profile declaring a server (today
    # `delivery_optimizer`) is served rather than refused. It CONNECTS TO NOTHING here -
    # transports are built, and reached on first use or on schema discovery.
    tools = build_tool_provider(TOOL_PACKAGES)
    # t-f5-10 filled this seat: `ModelSummariser` over the SAME `model` gateway every
    # other model call in this container uses, so a summarisation call never ends up on
    # a different day-1/day-2 route from the turn it is compacting. Before this line
    # `LadderContextEngine()` took no summariser and L3/L4 froze free on every real
    # turn - see WHAT IS WIRED BUT DEGRADED in the module docstring, now stale history
    # rather than the current shape.
    context = LadderContextEngine(ModelSummariser(model))
    skills = FilesystemSkillRegistry(resolved.skills_dir)
    # t-f3-20. `delivery` is left at the port's private default (BYTES): handing a model
    # provider a URL it fetches itself is a deployment decision, and `FilesystemMediaStore`
    # refuses to construct for SIGNED_URL without a `base_url` to point at. Constructing it
    # touches the filesystem no more than `FilesystemSkillRegistry` does - see NOTHING HERE
    # CONNECTS; the directory is read and written when a medium actually moves.
    media = FilesystemMediaStore(resolved.media_dir)
    # `PgConversationStore` takes a conninfo rather than a pool today - it opens its own
    # connection per call (t-f1-13). It is built here so that the day it grows a pool
    # parameter, the domain pool is already sitting next to it.
    store = PgConversationStore(resolved.app_conninfo, profiles)
    # The runner takes the SAME policy, audit and tool objects as `StartTurn`, so the
    # enforcement point in `before_tool_execute` reads the rules this container was built
    # with and the model is offered the toolset the policy narrowed.
    runner = PydanticAgentRunner(model=model, policy=policy, audit=audit, tools=tools)

    channels = build_channel_registry(resolved)

    # t-f3-15. `human_gateway_channel_id` falls back to `_HTTP_CHANNEL_ID` - see that
    # field's docstring on `Settings` - never to a push channel that may be `None`.
    human_gateway = ChannelHumanGateway(
        resolved.app_conninfo,
        channels,
        channel_id=resolved.human_gateway_channel_id or _HTTP_CHANNEL_ID,
    )
    # D25's requester seat, finally bound: `_pg_requester_lookup` reads the SAME audit
    # pool `audit` writes through (`resolved_audit_conninfo`), so the row the four-eyes
    # check reads back is the one non-negotiable #6 guarantees survives a rolled-back
    # turn.
    decide_approval = DecideApproval(
        gateway=human_gateway,
        audit=audit,
        signal=durable_signal_decision,
        requester=_pg_requester_lookup(resolved.resolved_audit_conninfo),
    )
    # t-f0-06. The `turns` row lives on `resolved.app_conninfo` - the SAME conninfo
    # `store` above was built with - never the audit conninfo: it is domain state, not an
    # append-only trail, and D25's separation (non-negotiable #6) is about the audit
    # write, not this read.
    turn_lookup = _pg_turn_lookup(resolved.app_conninfo)
    # t-f7-08 / t-f7-11. All three read or write the SAME app database `store`,
    # `human_gateway` and `turn_lookup` were built with: `human_requests` and `turns` are
    # domain state, not the append-only trail, so none of them takes the audit conninfo.
    # `ingest_media` is the one exception and it is not one - it AUDITS, so it takes this
    # container's audit sink, which is already bound to the audit pool (non-negotiable #6).
    ingest_media = IngestMedia(media=media, audit=audit)
    resolve_evidence = _pg_evidence_correlate(resolved.app_conninfo, profiles)
    signal_evidence = _pg_evidence_signal(resolved.app_conninfo)

    container = Container(
        settings=resolved,
        domain_pool=domain_pool,
        audit_pool=audit_pool,
        model=model,
        policy=policy,
        audit=audit,
        audit_reader=audit_reader,
        transcripts=transcripts,
        knowledge_admin=knowledge_admin,
        mailbox=mailbox,
        runner=runner,
        tools=tools,
        context=context,
        skills=skills,
        media=media,
        store=store,
        profiles=profiles,
        profile_versions=profile_versions,
        channels=channels,
        human_gateway=human_gateway,
        decide_approval=decide_approval,
        turn_lookup=turn_lookup,
        ingest_media=ingest_media,
        resolve_evidence=resolve_evidence,
        signal_evidence=signal_evidence,
        start_turn=StartTurn(
            runner=runner,
            tools=tools,
            policy=policy,
            store=store,
            audit=audit,
            context=context,
            skills=skills,
            profiles=profiles,
        ),
        # t-f3-20. The SAME runner, store, audit sink and profile map `start_turn` holds.
        # A second set here would police the continuation with rules loaded from another
        # container and file its tool calls through another sink - and the two would only
        # be seen to disagree in an audit trail nobody reconciles until it matters.
        resume_turn=ResumeTurn(
            runner=runner,
            store=store,
            audit=audit,
            media=media,
            profiles=profiles,
        ),
    )

    # The container is built FIRST and bound second, so the workflow and everything else in
    # the process share one set of objects rather than two equal-looking ones.
    bind_turn_workflow(container)

    return container


# The second logical database (migrations.py: `app` and `dbos`), named by the convention
# every integration test in this repository already uses: the app database plus this
# suffix. DBOS itself appends `_dbos_sys` to whatever `dbos_config` hands it, so it creates
# its own system database; this is the one the deployment owns and backs up.
_DBOS_DATABASE_SUFFIX = "_dbos"


def _database_name(conninfo: str) -> str:
    """The database a connection string names. t-f11-02.

    Parsed by psycopg rather than by a regular expression here, because a conninfo is
    legitimately either a URL or a keyword string (`dbname=... host=...`), and a pattern
    that understood only one of them would create a database called something else on a
    deployment that used the other.
    """
    dbname = conninfo_to_dict(conninfo).get("dbname")
    if not isinstance(dbname, str) or not dbname:
        raise ValueError(
            f"AGENT_CORE_DATABASE_URL names no database, so there is nothing to create or "
            f"migrate: {conninfo_to_dict(conninfo).keys()!r}. Give it a database, as in "
            f"{_DEFAULT_APP_CONNINFO!r}."
        )
    return dbname


def _server_is_reachable(conninfo: str) -> bool:
    """Can this host be connected to AT ALL, on the maintenance database?

    THIS IS A DIAGNOSIS, NOT A CONNECTION TEST, and it exists because psycopg reports both
    failures the same way. A refused server and a missing database both arrive as
    `OperationalError` with `sqlstate` unset - the SQLSTATE never crosses, because the
    failure happens while the connection is being established - so the only way to tell the
    two apart without matching on a localised driver message is to ask a second question:
    reach the same host with the same credentials on `postgres`. If that works, the server
    is up and the DATABASE is what is missing.

    Any failure at all answers False. This runs only on a path that is already failing, so
    a wrong guess costs a slightly less specific message and never a false start.
    """
    maintenance = dict(conninfo_to_dict(conninfo))
    maintenance["dbname"] = "postgres"
    try:
        with psycopg.connect(**maintenance, connect_timeout=5):  # type: ignore[arg-type]
            return True
    except psycopg.Error:
        return False


def _redacted_target(conninfo: str) -> str:
    """host, port and database name - NEVER a credential. t-f11-24.

    Built from the PARSED conninfo, never from the string itself: a URL conninfo carries
    its password inline (`postgresql://user:pass@host/db`) and a keyword conninfo may carry
    one as `password=...`, so only reading out the three keys an operator actually needs -
    never re-assembling or echoing the string - keeps this safe to put in an exception
    message. `host`/`port` fall back to libpq's own defaults so a bare `dbname=...`
    conninfo still names a place instead of printing nothing.
    """
    parsed = conninfo_to_dict(conninfo)
    host = parsed.get("host") or "localhost"
    port = parsed.get("port") or "5432"
    return f'host "{host}" port {port}'


def _refused_to_start(settings: Settings, error: psycopg.OperationalError) -> Exception:
    """Turn a failed connection into something the operator can act on. t-f11-02/t-f11-24.

    The whole of this anchor's value is in the message. Every failure in F11's list was
    discovered one at a time, three commands apart, because each one surfaced as the driver
    saw it rather than as the operator needed it - and `connection failed: FATAL: database
    "agent_core_app" does not exist` names a problem while naming no remedy.

    t-f11-24 ADDS THE SERVER, NOT JUST THE DATABASE. The checked-in `.env` and a fresh
    clone's local Postgres can legitimately both hold a database named `agent_core_app` -
    the shipped credentials point at the REMOTE Coolify instance, where that database was
    never created - so naming only the database left an operator unable to tell which
    server the failure was even about. That was the most confusing failure in this phase
    and it cost an afternoon once already: "the database does not exist" reads the same
    whether it is missing on the host you meant or you are pointed at the wrong host
    entirely. `_redacted_target` names the host and port too, and never a credential.

    A server that is genuinely down gets the original exception back, unchanged and
    un-wrapped in advice: telling someone to `CREATE DATABASE` on a host that is not
    listening would send them to fix the wrong thing.
    """
    if not _server_is_reachable(settings.app_conninfo):
        return error

    database = _database_name(settings.app_conninfo)
    target = _redacted_target(settings.app_conninfo)
    if settings.admin_conninfo:
        return MissingDatabaseError(
            f'the database "{database}" does not exist at {target}, and '
            "AGENT_CORE_ADMIN_DATABASE_URL is set, so startup tried to create it there and "
            "the schema still could not be applied. The admin connection reached a "
            f"different server, or the role it names may not CREATE DATABASE at {target}. "
            f"Original error: {error}"
        )
    return MissingDatabaseError(
        f'the database "{database}" does not exist at {target}.\n'
        "\n"
        f"Create it once at {target}, with a role that may:\n"
        f'    CREATE DATABASE "{database}";\n'
        f'    CREATE DATABASE "{database}{_DBOS_DATABASE_SUFFIX}";\n'
        "\n"
        "Or set AGENT_CORE_ADMIN_DATABASE_URL to an administrative connection string and "
        "start again: startup will create both databases and migrate them, idempotently. "
        "It is OPTIONAL on purpose - the application never needs superuser to RUN, only to "
        "bootstrap - so a production deployment sets it for one start and removes it "
        "(docs/TASKS.md#t-f11-02)."
    )


async def start_container(
    settings: Settings | None = None,
    *,
    pool_factory: PoolFactory = _unopened_pool,
) -> Container:
    """Bring the database up to the schema, THEN wire the adapters. Production starts here.

    t-f1-23. `build_container` deliberately performs no I/O - see NOTHING HERE CONNECTS -
    and that guarantee is worth keeping: it is what lets a wiring mistake surface on a
    laptop with nothing running. So the schema step is not folded into it; it is this
    function, and this function is what a process calls instead.

    WHAT WAS ACTUALLY BROKEN
        Nothing in production applied any migration. Nine `Migration` objects live in the
        sibling module that owns each table - the convention that keeps `migrations.py`
        from being a write two anchors share - and every integration test applied its own
        by hand. So the suite was green and a fresh deployment had no schema at all: the
        first `POST /turns` would fail on `INSERT INTO turns`. A per-test applier is a test
        double for a startup step nobody wrote, and the double is exactly what stopped
        anyone noticing the step was missing.

    WHY THERE IS NO LIST HERE
        `apply_all_migrations` DISCOVERS the migrations by importing this package's
        modules. Naming them here would be the same defect with an extra step: the next
        agent writes the module and forgets the line, and nothing says so until a table is
        missing in production. There is no line to forget.

    CREATING THE DATABASES IS OPT-IN, AND THE ORIGINAL REASONING IS WHY (t-f11-02)
        This function used to create nothing at all, because `ensure_databases` needs an
        ADMIN connection - `CREATE DATABASE` cannot run inside a transaction, and the app
        role need not be allowed to - and *a deployment that hands its application superuser
        credentials has a bigger problem than a missing table*. That sentence still decides
        the shape here, so the bootstrap did not become automatic; it became OPTIONAL.

        `AGENT_CORE_ADMIN_DATABASE_URL` present: the two logical databases are created if
        they are not there, then the schema is applied. Absent: only the schema is applied,
        and a database that does not exist raises `MissingDatabaseError` naming the exact
        command. Development leaves the variable in `.env` and gets one command; production
        sets it for one bootstrap run and removes it. THE APP NEVER REQUIRES SUPERUSER.

    AND THE POLICY IS RECONCILED, WHICH NOTHING IN PRODUCTION USED TO DO (t-f11-03)
        A fresh deployment came up with an empty `policy_rules`, denied every tool with
        "no matching rule", and had no supported way to change that short of raw SQL - the
        rules only tests had ever written. `Core/policy/*.yaml` is now applied here, the way
        a migration is, and it is a RECONCILIATION: a rule deleted from the reviewed file
        stops being in force, because an applier that only ever adds is a permission nobody
        can revoke.

        The policy directory's ABSENCE is meaningful and is not the same as its being
        empty. Missing, this step does nothing at all and whatever is in the table stays -
        a deployment that manages policy some other way is not silently disarmed by
        upgrading. Present and empty, it is a reviewer saying "no rules", and everything is
        revoked.

    ORDER: DATABASES, THEN SCHEMA, THEN POLICY, THEN WIRING. Each step needs the previous
    one to have happened - `policy_rules` is created by migration 0004 and gains its
    `tenant_id` column in 0012 - and the container is built last so a process never holds a
    wired container over a database it could not finish preparing.

    Idempotent end to end, and the second start proves it: `ensure_databases` skips what
    exists, an already-applied migration id is skipped (and every migration's SQL is `IF
    NOT EXISTS` besides), and the policy upsert does not even touch `updated_at` when the
    stored rule already matches the file.
    """
    resolved = Settings.from_env() if settings is None else settings

    if resolved.admin_conninfo:
        app_database = _database_name(resolved.app_conninfo)
        await ensure_databases(
            resolved.admin_conninfo,
            app_database=app_database,
            dbos_database=f"{app_database}{_DBOS_DATABASE_SUFFIX}",
        )

    try:
        await apply_all_migrations(resolved.app_conninfo)
    except psycopg.OperationalError as error:
        raise _refused_to_start(resolved, error) from error

    if resolved.policy_dir.is_dir():
        await apply_policy_rules(
            resolved.app_conninfo, load_policy_rules(resolved.policy_dir)
        )

    return build_container(resolved, pool_factory=pool_factory)
