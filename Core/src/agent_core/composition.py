"""Composition root - the ONE place adapters are chosen and wired.

Phase:   F0 (minimal) / grows every phase
Tasks:   docs/TASKS.md#t-f0-02
Status:  WIRES WHAT EXISTS (t-f0-02) - `start_turn` PENDING, SEE THE MISSING SEATS NOTE

WHY A SINGLE FILE
    Every `import` of a concrete adapter lives here and nowhere else. That is what makes
    the layer rule checkable by reading one file instead of grepping the tree.

    If a use case ever imports an adapter directly, the hexagon has a hole. The ruff
    banned-api config in pyproject.toml catches the common cases; this file catches the
    rest by being the only legitimate importer.

    `tests/unit/test_composition.py` states that rule from both sides: no module under
    `domain/`, `application/` or `ports/` may name `agent_core.adapters`, and this file
    must. Only the pair is a rule - the first half alone is satisfied by wiring nothing.

PSEUDO-CODE - F0, extended every phase
    def build_container(settings) -> Container:
        model    = LiteLLMGateway(settings)                  # F0
        tools    = CompositeToolProvider(local=..., mcp=...)  # F0 local, F6 mcp
        policy   = PgToolPolicy(pool)                         # F1
        store    = PgConversationStore(pool)                  # F1
        audit    = PgAuditSink(separate_pool)                 # F1 - SEPARATE POOL, see below
        context  = LadderContextEngine(aux_model)             # F5
        skills   = FsSkillRegistry(root)                      # F6
        media    = FsMediaStore(root)                         # F7
        human    = ChannelHumanGateway(...)                   # F3
        runner   = PydanticAgentRunner(model, policy, audit)  # F1
        profiles = load_profiles(Path("Core/profiles"))       # F1
        return Container(start_turn=StartTurn(...), ...)

    AuditSink gets its OWN connection pool on purpose. It must write outside the domain
    transaction so a rolled-back turn still leaves a trace. CLAUDE.md non-negotiable #6.
    Sharing the pool is the easy mistake and it silently erases the evidence of exactly
    the turns you most need to explain.

THE MISSING SEATS - WHAT THIS FILE CANNOT WIRE YET, AND WHY IT SAYS SO OUT LOUD
    `StartTurn` takes eight collaborators. `AgentRunner` used to be the fourth missing one;
    t-f1-12 landed and `PydanticAgentRunner` is constructed below like any other adapter.
    THREE seats are still empty, and each belongs to a later phase:

      - `ToolProvider`   - `adapters/driven/tools/` is docstring-only. The local provider
                           is docs/TASKS.md#t-f1-07 and the first real toolset is the F4
                           vertical (t-f4-01).
      - `ContextEngine`  - `adapters/driven/context/engine.py` is docstring-only. F5,
                           docs/TASKS.md#t-f5-04.
      - `SkillRegistry`  - `adapters/driven/skills_fs/registry.py` is docstring-only. F6,
                           docs/TASKS.md#t-f6-02.

    One more gap is not a seat but would bite on the first turn anyway:
    `PgConversationStore.append_outcome` still raises (docs/TASKS.md#t-f1-13), and
    `StartTurn` step 7 awaits it on every turn, finished or suspended.

    Wiring a placeholder for any of these would produce a container that builds and then
    fails on the first turn, which is the shape of bug this file exists to prevent. So the
    container carries the adapters that exist and NOT a half-built use case. The absence
    stays one named gap here rather than three silent ones spread across the tree.

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

import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from psycopg_pool import ConnectionPool

from agent_core.adapters.driven.agent_pydantic.runner import PydanticAgentRunner
from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway
from agent_core.adapters.driven.persistence_pg.audit_repository import PgAuditSink
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
)
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.adapters.driving.channels.registry import Channel, ChannelRegistry
from agent_core.adapters.driving.channels.telegram import TELEGRAM_CHANNEL_ID, TelegramChannel
from agent_core.adapters.driving.channels.whatsapp import WhatsAppChannel
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry
from agent_core.domain.turn import TenantId

__all__ = [
    "Container",
    "PoolConnections",
    "PoolFactory",
    "Settings",
    "build_channel_registry",
    "build_container",
    "load_profiles",
]

# whatsapp.py deliberately does not export its channel id - it is a `_CHANNEL_ID` module
# constant, private on purpose (see that module's docstring on leaf isolation). Reaching
# into another module's private name from here would be the same coupling the leaf
# adapters exist to avoid, so the literal is restated once, at the one place D23 always
# meant it to live: the wiring.
_WHATSAPP_CHANNEL_ID = "whatsapp"

# The repository layout is `Core/src/agent_core/composition.py`, so `Core/` is three
# parents up and `Core/profiles/` is the directory the profile files live in. Overridable
# by environment because an installed wheel has no `Core/` above it.
_CORE_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_APP_CONNINFO = "postgresql://localhost/agent_core_app"


@dataclass(frozen=True)
class Settings:
    """Everything the wiring needs from outside the process.

    Read from the environment by `Settings.from_env()`, never read from the environment
    piecemeal further down. A second `os.environ` lookup somewhere in the tree is how a
    deployment ends up with two different opinions about which database it is using.
    """

    app_conninfo: str = _DEFAULT_APP_CONNINFO
    # `None` means "the app database". The audit tables live alongside the domain tables
    # on day 1; what must be separate is the POOL, not the database - see `build_pools`.
    audit_conninfo: str | None = None
    profiles_dir: Path = field(default_factory=lambda: _CORE_ROOT / "profiles")
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

    @property
    def resolved_audit_conninfo(self) -> str:
        return self.app_conninfo if self.audit_conninfo is None else self.audit_conninfo

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        profiles_dir = env.get("AGENT_CORE_PROFILES_DIR")
        return cls(
            app_conninfo=env.get("AGENT_CORE_DATABASE_URL", _DEFAULT_APP_CONNINFO),
            audit_conninfo=env.get("AGENT_CORE_AUDIT_DATABASE_URL"),
            profiles_dir=Path(profiles_dir) if profiles_dir else _CORE_ROOT / "profiles",
            proxy_base_url=env.get("AGENT_CORE_LITELLM_BASE_URL"),
            telegram_bot_token=env.get("TELEGRAM_BOT"),
            whatsapp_access_token=env.get("AGENT_CORE_WHATSAPP_ACCESS_TOKEN"),
            whatsapp_phone_number_id=env.get("AGENT_CORE_WHATSAPP_PHONE_NUMBER_ID"),
            channel_tenant_id=TenantId(env.get("AGENT_CORE_CHANNEL_TENANT_ID", "default")),
            channel_profile_id=env.get("AGENT_CORE_CHANNEL_PROFILE_ID"),
        )


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
        profile = assign.assign(load_profile_sync(path))
        if profile.id in profiles:
            raise ValueError(
                f"Two profile files declare id {profile.id!r}; {path.name} is the second. "
                "Refusing to pick one: the loser's permissions would vanish silently."
            )
        profiles[profile.id] = profile

    return profiles


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
    profiles: dict[str, AgentProfile]
    profile_versions: ProfileVersionRegistry
    # t-f3-06 (D23 Shape A): one lookup table, shared by `ChannelHumanGateway`'s mid-turn
    # prompt and the workflow's end-of-turn `_step_deliver` (t-f3-07). `ChannelRegistry`,
    # never a plain `dict` or `Mapping` - see `build_channel_registry`'s docstring for
    # why the type itself, not just its contents, is the point.
    channels: ChannelRegistry


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
    """
    pairs = (
        pair
        for pair in (_telegram_channel(settings), _whatsapp_channel(settings))
        if pair is not None
    )
    return ChannelRegistry(pairs)


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

    model = LiteLLMGateway(resolved.proxy_base_url)
    policy = PgToolPolicy(PoolConnections(domain_pool))
    # PoolConnections(audit_pool), never PoolConnections(domain_pool). This one line
    # is the whole of non-negotiable #6 in practice.
    audit = PgAuditSink(PoolConnections(audit_pool))

    return Container(
        settings=resolved,
        domain_pool=domain_pool,
        audit_pool=audit_pool,
        model=model,
        policy=policy,
        audit=audit,
        # `tools=None` is the empty `ToolProvider` seat named in THE MISSING SEATS, not a
        # default: an agent with no tools is honest, a fake toolset is not. The runner
        # takes the SAME policy and audit objects as everything else, so the enforcement
        # point in `before_tool_execute` reads the rules this container was built with.
        runner=PydanticAgentRunner(model=model, policy=policy, audit=audit, tools=None),
        # `PgConversationStore` takes a conninfo rather than a pool today - it opens its
        # own connection per call (t-f1-13). It is listed here so that the day it grows a
        # pool parameter, the domain pool is already sitting next to it.
        store=PgConversationStore(resolved.app_conninfo, profiles),
        profiles=profiles,
        profile_versions=profile_versions,
        channels=build_channel_registry(resolved),
    )
