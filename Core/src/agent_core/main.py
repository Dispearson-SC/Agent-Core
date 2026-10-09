"""The process entry point: one container, one ASGI app or one console, every router on it.

Phase:   F0
Tasks:   docs/TASKS.md#t-f0-05, docs/TASKS.md#t-f0-07, docs/TASKS.md#t-f11-04,
         docs/TASKS.md#t-f11-18, docs/TASKS.md#t-f11-43
Status:  DONE - build_app() wires the container once and mounts chat, transcript and admin.
         t-f0-07: `main()` now dispatches between two ways to run the same container -
         `serve` (the HTTP process) and `console` (the operator REPL). The dispatch is
         HERE rather than in `__main__.py`, which says in its own docstring that it must
         stay three lines and is right: logic there would be a third place that knows how
         the system starts. `__main__.py` delegates to `main()`, so every subcommand -
         `serve`, `console` and now `preflight` - is reachable as `python -m agent_core`.
         t-f11-04: `preflight` is the third subcommand, and `refuse_unless_ready` is the
         gate the other two run before they open a socket or a prompt.
         t-f11-18: the console's tool-call read goes through `Container.audit_reader` and
         the hand-built `_PgToolCallLog` is retired.
         t-f11-43: `peer-worker` is the fourth subcommand - the consumer the durable peer
         mailbox never had. It is four lines of wiring here and a driving adapter in
         `adapters/driving/peers/worker.py`, for the reason every other subcommand is.
         t-f0-06 / t-f3-11: build_app() now binds routes.py's `lookup_turn` and `decide`
         seats to the container's OWN `turn_lookup` / `decide_approval` by default -
         previously neither was passed to create_app at all, so GET /turns/{turn_id} and
         POST /decisions/{corr_id} answered 503 in every process serve() started.
         t-f7-08 / t-f7-11: the same for `ingest_media`, `resolve_evidence` and
         `signal_evidence`, which had the same defect and the same cause - see the seat
         guard in tests/unit/test_composition.py, which now derives the list of seats
         this function must pass from create_app's own signature.
Implements: nothing - it assembles what composition.py wired and what the routers expose

WHY THIS FILE EXISTS AT ALL
    Every other F0 anchor closed and the repository was still a library with tests: no
    `main`, no ASGI app, no process (docs/STATE.md, "what cannot be run"). A phase whose
    done-when is "a POST returns a model response" needs something for the POST to arrive
    at. This is that something, and it is deliberately thin - an entry point that grows
    logic becomes a second composition root, and then there are two places that know how
    the system is built.

THE CONTAINER IS BUILT ONCE, AND THE ONCE IS THE POINT
    `build_container()` constructs two connection pools, an `httpx.AsyncClient` per
    configured channel, and reads and version-assigns every profile file. The idiomatic
    FastAPI spelling for handing a handler its dependencies is `Depends(build_container)`,
    and here that is a bug with three separate faces: a fresh pool set per request that is
    never reused, a fresh `ProfileVersionRegistry` that restarts at version 1 so two turns
    in one process file different versions for the same profile (D20), and a startup that
    can no longer fail on a wiring error because there is no startup left. None of that
    shows up in a single-request test. `tests/integration/test_main.py` counts the builds.

WHY THE FRAMEWORK TYPE IS NOWHERE IN THIS FILE
    `fastapi` is a banned import outside `adapters/driving/http/` (Core/pyproject.toml,
    flake8-tidy-imports), and this module is not an adapter - it is the composition of
    them. So the app travels as `ASGIApplication`, a structural protocol spelled out
    below. Nothing here needs to know what the routers were built with, and the ban is a
    fair statement of that: the day a router is served by something other than FastAPI,
    this file does not change.

WHICH PROCESSES LAUNCH THE DURABLE ENGINE, AND WHY IT IS NOT ALL OF THEM
    `dbos` is banned outside `adapters/driving/workflow/` for the same reason `fastapi` is
    banned here, so this file never imports it: `launch_dbos(container)` is that package's
    bootstrap (t-f2-13) and it takes the CONTAINER, so no launcher can hand DBOS a second
    opinion about the pinned `application_version`.

    `serve` launches it, because a turn is enqueued onto a queue that does not exist until
    it does. `peer-worker` launches it too (t-f11-49), because waking the turn that asked
    a peer is `DBOS.send_async` - see that subcommand for what launching costs and what it
    does not buy.

    `console` LAUNCHES IT ONLY FOR `--durable`, AND THAT IS THE t-f11-51 CHANGE
        It used to decline unconditionally, and the reason it gave was correct for the only
        mode it had: a console that calls `StartTurn` directly has no queue to dequeue and
        no workflow of its own to recover, so launching would start a recovery worker for
        turns this process is not serving. That reason does not survive the durable mode.
        `enqueue_turn` puts the turn on a queue that does not exist until `launch_dbos`
        runs, and it is THIS process that must dequeue it - so without the engine the first
        durable turn fails at the enqueue, from the prompt, with the operator holding
        nothing. What was a lie in one direction would become a lie in the other.

        So the launch follows the flag, one decision with one reason on each side: no
        `--durable`, no engine, and the banner's direct-mode notice is true as it always
        was; `--durable`, and this process is a full executor of the application exactly as
        `peer-worker` is - it dequeues `agent-core-turns` and takes part in version-scoped
        startup recovery. That is acceptable rather than accidental, for the reason
        `run_peer_worker_process` gives: `build_container` has already bound the workflow's
        collaborators through `bind_turn_workflow` before the launch is reached.

    This file also builds two driven adapters that `composition.py` has no seat for yet -
    `PgTranscriptReader` and `PgKnowledgeAdmin` - because a router mounted against nothing
    is a router that is not mounted. They are marked below and they belong in the
    container; this is the only place in the tree outside `composition.py` that chooses a
    DRIVEN adapter, and it should stop being so.

    THE THIRD ONE IS GONE (t-f11-18). `_PgToolCallLog` used to live here with its own
    `SELECT` over `audit_tool_calls`, because the console needed a read of the trail and no
    port answered one. `ports/audit_reader.py` is that port and `Container.audit_reader` is
    its seat, so what is left in this file is a projection between two already-chosen
    types. Two to go.

THE PREFLIGHT IS A GATE, AND THAT IS WHY IT IS ONE CALL (t-f11-04)
    `serve` and `console` both run `refuse_unless_ready` before they start, and
    `python -m agent_core preflight` runs the same report on its own. The report itself
    lives in `adapters/driving/cli/preflight.py`: an entry point that grew the checks would
    be the second composition root this file exists not to become.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_core.adapters.driving.cli.console import Console
from agent_core.adapters.driving.cli.preflight import preflight
from agent_core.adapters.driving.http.admin_routes import (
    AdminAuthenticator,
    create_admin_router,
)
from agent_core.adapters.driving.http.routes import (
    EvidenceCorrelate,
    EvidenceSignal,
    TurnHandle,
    TurnLookup,
    TurnStarter,
    create_app,
)
from agent_core.adapters.driving.http.transcript_routes import create_transcript_router
from agent_core.adapters.driving.peers.worker import (
    DirectTurnRunner,
    PeerQueue,
    answerable_targets,
    run_peer_worker,
)
from agent_core.adapters.driving.workflow.bootstrap import launch_dbos
from agent_core.adapters.driving.workflow.turn_workflow import (
    enqueue_turn,
    signal_peer_answer,
)
from agent_core.application.decide_approval import DecideApproval
from agent_core.application.ingest_media import IngestMedia
from agent_core.composition import (
    Container,
    Settings,
    build_container,
    load_profiles,
    start_container,
)
from agent_core.domain.knowledge import CollectionId
from agent_core.domain.peers import AgentId
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
)
from agent_core.ports.knowledge_admin import (
    AdminIdentity,
    AdminSubjectId,
    KnowledgeAdmin,
    TenantAdminScope,
)
from agent_core.ports.transcript_reader import TranscriptReader

__all__ = [
    "ASGIApplication",
    "build_app",
    "build_console",
    "main",
    "refuse_unless_ready",
    "run_console",
    "run_peer_worker_process",
    "serve",
]

# The ASGI calling convention, restated rather than imported. See WHY THE FRAMEWORK TYPE
# IS NOWHERE IN THIS FILE above; these three aliases are the whole of the contract a
# server and an application share.
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class ASGIApplication(Protocol):
    """What a server needs from an app, and nothing else."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None: ...


_ADMIN_TOKEN_ENV = "AGENT_CORE_ADMIN_TOKEN"
_ADMIN_TENANT_ENV = "AGENT_CORE_ADMIN_TENANT_ID"
_ADMIN_SUBJECT_ENV = "AGENT_CORE_ADMIN_SUBJECT_ID"
_ADMIN_COLLECTIONS_ENV = "AGENT_CORE_ADMIN_COLLECTIONS"
_HOST_ENV = "AGENT_CORE_HTTP_HOST"
_PORT_ENV = "AGENT_CORE_HTTP_PORT"


@dataclass(frozen=True, slots=True)
class _QueuedTurn:
    """The id half of a DBOS handle, with everything a route may not touch left behind.

    `routes.py` narrows what it holds to `TurnHandle` on purpose - a route that can reach
    a result is a route that can wait for one. This is where the wide durable handle
    becomes the narrow one.
    """

    turn_id: TurnId


async def _start_turn_on_the_queue(request: TurnRequest) -> TurnHandle:
    """The production `TurnStarter`: enqueue, take the id, let go.

    THE ID HERE IS THE WORKFLOW'S, NOT THE AUDIT TRAIL'S, AND THAT IS A KNOWN SEAM.
    `run_turn_workflow` mints its own `TurnId` inside `_step_new_turn_id`, so the id a
    caller can poll and the id every audit row is filed under are two different values
    with nothing joining them. That reconciliation is `docs/TASKS.md#t-f0-06`, which owns
    `GET /turns/{turn_id}` - the half of the 202 that has to read one of them. Returning
    the workflow id is the honest answer until then: it is the id this process can
    actually resolve later, and pretending otherwise would put a fabricated join in the
    entry point.
    """
    handle = await enqueue_turn(request)
    return _QueuedTurn(turn_id=TurnId(handle.workflow_id))


async def _enqueue_console_turn(request: TurnRequest) -> TurnId:
    """The console's durable starter: enqueue, take the id, let go. t-f11-51.

    THE SAME `enqueue_turn` `POST /turns` USES, AND NOT A SECOND WAY IN. Starting a turn
    any other way - `DBOS.start_workflow_async`, or calling the workflow function - skips
    the per-session partition and lets two messages of one conversation run at once, which
    is the interleaving D19 exists to prevent and which fails only under real load.

    THE ID IS THE TURN'S, NOT A SECOND IDENTIFIER. `_start_turn_on_the_queue` above records
    a seam here; it is closed on this path by `turn_workflow._step_new_turn_id`, which reads
    the durable workflow id back instead of minting one. So the id handed to the console is
    the id the audit rows are filed under and the id `GET /turns/{turn_id}` resolves - one
    value, joined by construction rather than by a query somebody has to remember to write.

    NO WINDOW, deliberately. `enqueue_turn_window` is what opens the per-session coalescing
    window, and it belongs to `POST /turns`: an operator typing one sentence at a prompt has
    nothing to coalesce with, and a console that waited out a window before starting would
    be slower than the direct path for no property gained. The console's durable-mode notice
    says coalescing is still not exercised, and this is why.
    """
    handle = await enqueue_turn(request)
    return TurnId(handle.workflow_id)


def _env_admin_authenticator(environ: Mapping[str, str] | None = None) -> AdminAuthenticator:
    """The deployment's admin credential verifier, read from the environment.

    THIS IS THE SEAM `admin_routes.py` REFUSED TO FILL, FILLED WHERE IT BELONGS
        That module says an admin surface must not implement its own credential store,
        because the wall it defends is "an administrator is established by the admin
        route" and a route that mints its own administrators is not a wall. The seam is
        meant to be filled where the deployment is assembled. That is here.

    NON-NEGOTIABLE #9 IS STRUCTURAL, NOT A RULE ANYONE HAS TO REMEMBER
        The verifier takes a bare token and returns a `TenantAdminScope`. There is no
        parameter a `CallerIdentity` fits in, `AdminSubjectId` is a `NewType` a plain
        `str` from a chat header cannot satisfy without an explicit mint, and the tenant
        comes from the environment that issued the credential - never from a request.

    UNCONFIGURED MEANS EVERY ADMIN REQUEST IS 401, AND THE ROUTER IS STILL MOUNTED
        An absent token is not a deployment choice to be guessed at. The routes exist,
        answer 401, and the write port is never reached - which is a surface an operator
        can see and enable, rather than a 404 they have to go read the source to explain.

    `AGENT_CORE_ADMIN_COLLECTIONS` narrows the grant to a comma-separated list. Left
    unset, the scope is superuser WITHIN THAT ONE TENANT, which is what
    `TenantAdminScope` means by superuser and has never meant across tenants.
    """
    env = os.environ if environ is None else environ
    configured = env.get(_ADMIN_TOKEN_ENV)
    tenant = env.get(_ADMIN_TENANT_ENV)
    subject = env.get(_ADMIN_SUBJECT_ENV, "operator")
    named = tuple(
        CollectionId(part.strip())
        for part in env.get(_ADMIN_COLLECTIONS_ENV, "").split(",")
        if part.strip()
    )

    def authenticate(token: str) -> TenantAdminScope | None:
        if not configured or not tenant:
            return None
        # Constant time, because a token comparison that returns early leaks the token one
        # character at a time to anyone willing to measure.
        if not hmac.compare_digest(token, configured):
            return None
        return TenantAdminScope(
            admin=AdminIdentity(
                subject_id=AdminSubjectId(subject),
                collections=named,
                is_superuser=not named,
            ),
            tenant_id=TenantId(tenant),
        )

    return authenticate


def build_app(
    *,
    container_factory: Callable[[], Container] = build_container,
    start_turn: TurnStarter | None = None,
    lookup_turn: TurnLookup | None = None,
    decide: DecideApproval | None = None,
    ingest_media: IngestMedia | None = None,
    resolve_evidence: EvidenceCorrelate | None = None,
    signal_evidence: EvidenceSignal | None = None,
    transcript_reader: TranscriptReader | None = None,
    knowledge_admin: KnowledgeAdmin | None = None,
    admin_authenticator: AdminAuthenticator | None = None,
) -> ASGIApplication:
    """The whole HTTP surface of the process, wired to one container.

    Every parameter is a seam with a production default. They exist so the assembly can be
    asserted without infrastructure - `container_factory` is what makes "built once" a
    countable fact rather than a claim - and NOT so a deployment can assemble a different
    system: called with no arguments, this is exactly what `serve()` runs.

    EVERY ROUTE SEAT DEFAULTS TO THE CONTAINER'S OWN, NEVER TO NONE
        `routes.create_app` treats an unbound seat as a legitimate, if regrettable,
        deployment - unwired means a route refuses loudly rather than answering
        something plausible (that module's own docstring). It is never legitimate HERE:
        `composition.py` built both `turn_lookup` and `decide_approval` before this
        function was ever called, so a `None` reaching `create_app` from THIS file would
        not be an unconfigured deployment, it would be this file forgetting to pass along
        a seat the container was already holding. That is exactly how `GET
        /turns/{turn_id}` and `POST /decisions/{corr_id}` answered 503 in every process
        `serve()` started - docs/TASKS.md#t-f0-06, docs/TASKS.md#t-f3-11 - while the tests
        written directly against `routes.py` stayed green by injecting the seats
        themselves. The parameters stay overridable, the same way `start_turn` is, so
        this function can still be asserted with a recording stand-in and no database.

        `ingest_media`, `resolve_evidence` and `signal_evidence` HAD EXACTLY THE SAME
        DEFECT, one wave later and with the same fixture hiding it
        (docs/TASKS.md#t-f7-08, docs/TASKS.md#t-f7-11). Wiring them is half the fix; the
        other half is `tests/unit/test_composition.py`'s seat guard, which DERIVES the
        list of seats this call must pass from `create_app`'s own signature - so the
        seventh instance fails here instead of surviving another five waves.

    The chat app is the base and the other two arrive as routers, so there is one
    application object rather than a mount tree. That matters for the admin surface: two
    apps mounted side by side would each carry their own middleware and their own
    exception handlers, and "the admin surface is separate" would quietly come to mean
    "the admin surface is configured somewhere else".
    """
    container = container_factory()

    app = create_app(
        start_turn=_start_turn_on_the_queue if start_turn is None else start_turn,
        lookup_turn=container.turn_lookup if lookup_turn is None else lookup_turn,
        decide=container.decide_approval if decide is None else decide,
        ingest_media=(
            container.ingest_media if ingest_media is None else ingest_media
        ),
        resolve_evidence=(
            container.resolve_evidence if resolve_evidence is None else resolve_evidence
        ),
        signal_evidence=(
            container.signal_evidence if signal_evidence is None else signal_evidence
        ),
    )

    # t-f11-33. THE DEBT THIS COMMENT USED TO RECORD IS PAID. Both of these were built
    # HERE, from the container's own domain pool, because `Container` had no seat for
    # either - which made this file the only module outside `composition.py` that chose a
    # driven adapter. It now has both seats, so the choice is back in the one place that
    # is allowed to make it and the two adapter imports are gone from the top of this file.
    reader = container.transcripts if transcript_reader is None else transcript_reader
    admin = container.knowledge_admin if knowledge_admin is None else knowledge_admin

    app.include_router(create_transcript_router(reader=reader))
    app.include_router(
        create_admin_router(
            knowledge_admin=admin,
            authenticate=(
                _env_admin_authenticator()
                if admin_authenticator is None
                else admin_authenticator
            ),
        )
    )

    # For whoever runs the process: the pools to open, the profiles that were loaded, the
    # channels that were configured. Read-only in practice - nothing here reaches back
    # into it, so an app is still just an app.
    app.state.container = container

    return app


# t-f11-18. THE AUDIT READ THE CONSOLE RENDERS A TURN FROM - NOW A PORT, NOT SQL IN HERE.
#
# This file used to carry a `SELECT` over `audit_tool_calls` and a `_PgToolCallLog` around
# it, because `AuditSink` is write-only by design and no port answered "what did that turn
# actually call". A consumer forced to declare the protocol it consumes is the definition
# of a missing port (`ports/audit_reader.py`), and the next consumer declares a second one.
#
# `AuditReader` is that port (t-f11-08) and `Container.audit_reader` is its seat
# (t-f11-18), and nothing at all is left here: the console takes the port directly. A
# projection lived here briefly, narrowing `AuditedToolCall` onto a shape the console had
# declared before the port existed - and it dropped migration 0022's `reason`, the sentence
# the winning rule actually said, because that older shape had no field for one. Deleting
# the duplicate shape is what let the sentence through. No connection string, no statement,
# no second opinion about which pool the trail is read through.

# The channel a console turn is attributed to. NOT `http`: policy rules can be scoped by
# channel, and letting the console borrow the HTTP channel's rules would mean the verdicts
# an operator reads here are not the verdicts this caller would get. A deployment that has
# written no `cli` row still gets every rule with no channel constraint, which is what an
# empty `channels` set means (domain/policy.py).
_CONSOLE_CHANNEL_ID = "cli"


def _console_audit_admin(environ: Mapping[str, str] | None = None) -> AdminIdentity:
    """The identity the OPERATOR PROCESS reads the trail under. t-f11-18.

    NON-NEGOTIABLE #9, AND WHY THIS IS NOT A WIDENING
        `AuditReader` takes an `AdminIdentity` because tool names, arguments and policy
        verdicts are admin-audience facts (`ports/transcript_reader.py`'s own list). This
        function mints one from the ENVIRONMENT - the same route `_env_admin_authenticator`
        above uses, and the same route a deployment issues every other administrative
        credential through.

        It never touches the console's `--subject`, and there is no parameter it could
        arrive through: a `CallerIdentity` cannot be widened into an `AdminIdentity` by any
        code path, which is the whole reason the two are different types. The console's
        caller decides what the AGENT may do; this decides what the PROCESS may read, and
        the process is already holding the database credentials.

    `is_superuser` within the deployment, because `audit_tool_calls` carries no tenant to
    scope by (t-f11-21) and `collections` narrows knowledge documents, which this port does
    not read. Claiming a narrower scope than the table can enforce would be decoration.
    """
    env = os.environ if environ is None else environ
    return AdminIdentity(
        subject_id=AdminSubjectId(env.get(_ADMIN_SUBJECT_ENV, "operator")),
        collections=(),
        is_superuser=True,
    )


def _stdin_reader() -> Callable[[str], str | None]:
    """`input()`, with both ways out of a REPL reported as end of input.

    EOFError is ctrl-D or a piped script running out. KeyboardInterrupt is ctrl-C, and
    treating it as the end rather than letting it unwind means the pools still close
    through `run_console`'s `finally`.
    """

    def read_line(prompt: str) -> str | None:
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    return read_line


def build_console(
    container: Container,
    *,
    profile_id: str | None = None,
    subject_id: str = "operator",
    tenant_id: TenantId | None = None,
    channel: str = _CONSOLE_CHANNEL_ID,
    roles: frozenset[str] = frozenset(),
    session_id: SessionId | None = None,
    durable: bool = False,
    write_line: Callable[[str], None] = print,
    read_line: Callable[[str], str | None] | None = None,
) -> Console:
    """The operator REPL, wired to the container this process already built.

    EVERY SEAT COMES FROM THE CONTAINER, WHICH IS THE WHOLE POINT
        `start_turn`, `tools` and `policy` are the objects `composition.py` chose. A
        console that built its own would be inspecting a system nobody deploys - and the
        one subsystem it exists to make visible, the policy engine, is the one CLAUDE.md's
        silent-bug table says never fails a test.

    NON-NEGOTIABLE #9: THERE IS NO ADMIN SEAT HERE AND THERE CANNOT BE ONE
        The console gets a `CallerIdentity`, built from flags. `AdminIdentity` is a
        different type reached by a different route (`_env_admin_authenticator`, from a
        credential in the environment), and nothing in this function could widen one into
        the other - there is no parameter it would arrive through.

    The tenant defaults to `settings.channel_tenant_id` rather than to a literal, so the
    console inspects the tenant this deployment actually serves. The session id is fresh
    per run unless one is named: reusing a real conversation's id would append console
    experiments to a customer's history.

    SIX OF THE THIRTEEN COMMANDS WERE INERT IN THE SHIPPED PROCESS UNTIL t-f11-33
        `profiles_dir`, `load_profiles`, `approvals` and `transcripts` were passed for
        none of them, so `:new`, `:reload`, `:approve`, `:refuse`, `:sessions` and
        `:trace` - every command for MAKING an agent and every command for JUDGING one -
        answered "this console has no ... wired" from `python -m agent_core console`. The
        surface behind them (t-f11-09 .. t-f11-13) was finished, tested and green, because
        `tests/unit/test_console.py` constructs the console itself and passes every one of
        those seats. A fixture that supplies the missing piece is exactly what stops
        anyone noticing it is missing; the acceptance run
        (`tests/integration/test_cli_acceptance.py`) drives this function instead, and
        found all six on its first execution.
    """
    return Console(
        profiles=container.profiles,
        tools=container.tools,
        policy=container.policy,
        start_turn=container.start_turn,
        # t-f11-33. `:new` writes here and `:reload` re-reads here - the SAME directory
        # this container loaded from, never a second opinion about where profiles live.
        profiles_dir=container.settings.profiles_dir,
        # t-f11-33. `:reload`, bound to the composition root's own loader and to this
        # container's version REGISTRY (D20). The registry is what makes the reload
        # meaningful: a fresh one would restart numbering at 1, so an edited profile would
        # be filed under a version that already means something else in the audit trail.
        # The console never learns that a profile is a YAML file on a disk - `yaml` is
        # banned outside `adapters/`, and this callable is the whole of the seam.
        load_profiles=lambda: load_profiles(
            container.settings.profiles_dir, container.profile_versions
        ),
        # t-f11-33 / D25. The container's OWN `DecideApproval`, so the console answers a
        # pending request through the same use case `POST /decisions/{corr_id}` does - one
        # correlation table, one audit sink, one four-eyes rule.
        #
        # NON-NEGOTIABLE #9 IS NOT BENT BY THIS SEAT. `DecideApproval.execute` takes a
        # `subject_id` string and decides with it; it is handed no `AdminIdentity` and
        # mints none. D25's four-eyes rule will usually REFUSE a one-operator console -
        # the operator asking is the operator approving - and that refusal is CORRECT and
        # says so by name (`FourEyesError`, caught and rendered by the console). Nothing
        # here works around it; a console that could approve its own requests would be the
        # rule deleted rather than enforced.
        approvals=container.decide_approval,
        # t-f11-33. `:sessions` and `:trace`, on the container's reader - the same one the
        # transcript router is mounted against (`build_app`), so what an operator reads
        # here is what an HTTP caller would read, in whichever `Audience` they ask for
        # (non-negotiable #11).
        transcripts=container.transcripts,
        # t-f11-18. The container's OWN reader, on the audit pool it was built with -
        # never a second one assembled here from a connection string.
        audit=container.audit_reader,
        # t-f11-51. THE DURABLE PATH, AND BOTH HALVES OF IT COME FROM HERE.
        #
        # The console gains a second way to run a turn and does not become a second
        # composition root to get it: `_enqueue_console_turn` is the same `enqueue_turn`
        # `POST /turns` starts a turn with, and `container.turn_lookup` is the same seat
        # `GET /turns/{turn_id}` reads it back through (`build_app` passes that very
        # object). So a durable console turn is started and read exactly as an HTTP one is,
        # by the objects this deployment was built with - a console that assembled its own
        # queue or its own SELECT would be inspecting a system nobody deploys, which is the
        # whole reason every other seat here is the container's.
        #
        # BOUND ONLY WHEN THE ENGINE IS. `--durable` is one decision with two halves:
        # `run_console` launches DBOS, and these seats are filled. Filling them in a
        # process that declined to launch would hand the console a queue that does not
        # exist, and `:mode durable` would succeed and then fail at the first turn - so the
        # absence IS the signal, and the console's refusal names the command to restart
        # with. Inside a `--durable` process both modes stay reachable through `:mode`,
        # because the direct path is an addition this task deliberately did not remove.
        durable_enqueue=_enqueue_console_turn if durable else None,
        durable_poll=container.turn_lookup if durable else None,
        durable=durable,
        admin=_console_audit_admin(),
        caller=CallerIdentity(
            subject_id=subject_id,
            channel=channel,
            tenant_id=(
                container.settings.channel_tenant_id if tenant_id is None else tenant_id
            ),
            roles=roles,
        ),
        session=SessionRef(
            session_id=(
                SessionId(f"console-{uuid.uuid4().hex[:12]}")
                if session_id is None
                else session_id
            ),
            tenant_id=(
                container.settings.channel_tenant_id if tenant_id is None else tenant_id
            ),
        ),
        write_line=write_line,
        read_line=_stdin_reader() if read_line is None else read_line,
        profile_id=profile_id,
    )


def refuse_unless_ready(
    settings: Settings, *, write_line: Callable[[str], None] = print
) -> None:
    """Print what is not ready, and do not start if any of it is hard. t-f11-04.

    ONE CALL, BECAUSE THIS FILE MUST STAY THIN. The checks live in
    `adapters/driving/cli/preflight.py`; what belongs here is the DECISION that a process
    does not start on a deployment that cannot take a turn.

    IT PRINTS EVERYTHING, INCLUDING WHEN IT IS ABOUT TO REFUSE. The whole value of the
    preflight over the failure ladder is the full list, so the report is written out first
    and the refusal comes after it - never instead of it.

    A WARNING IS NOT A REFUSAL. `PreflightReport.ready` is "no hard failure": a deployment
    that manages policy outside this repository, or that has not created the second logical
    database, gets a line to read and a process that starts.

    `SystemExit` rather than a custom exception: the caller is an entry point and the
    honest outcome of "this cannot work" is a non-zero exit code, not a traceback.
    """
    report = asyncio.run(preflight(settings))
    for line in report.render().splitlines():
        write_line(line)
    if not report.ready:
        raise SystemExit(
            f"preflight: refusing to start - {len(report.failures)} of "
            f"{len(report.checks)} checks are not ready. Each one above carries what to "
            "do about it; fix them and start again."
        )


def run_preflight(_arguments: argparse.Namespace) -> None:
    """`python -m agent_core preflight` - the report, and nothing else. t-f11-04.

    It does NOT bootstrap. `start_container` creates databases and applies migrations; this
    reports on the deployment as it stands, which is the only way to be told that the
    database is missing rather than have it quietly created underneath the question. That
    is also why it is worth running before the first start, on a machine where nothing has
    been set up yet.

    The exit code is the answer: 0 ready, non-zero not. A preflight nobody can put in a
    deploy script is a preflight that runs once, by hand, the first time.
    """
    refuse_unless_ready(Settings.from_env())


def run_console(arguments: argparse.Namespace) -> None:
    """Start the container, run the REPL, close the pools. The console's `serve()`.

    It calls `start_container` for the same reason `serve` does: a process that builds the
    container without migrating is the t-f1-23 gap reopened at the one place nobody writes
    a test for.

    IT LAUNCHES `launch_dbos` FOR `--durable` AND FOR NOTHING ELSE (t-f11-51)
        Without the flag it does not, and the reason is the one it always gave: the console
        calls `StartTurn` directly, so there is no queue to dequeue from and no workflow to
        recover, and launching would start a recovery worker for turns this process is not
        serving. That reason is exactly as true as it ever was - for that mode.

        With the flag the console enqueues through `turn_workflow.enqueue_turn`, onto a
        queue that does not exist until the engine is launched, and it is THIS process that
        has to dequeue and run it. Declining here would make the durable mode fail at the
        very first turn, from the prompt, with no queue and no explanation - the banner a
        lie in the other direction. See WHICH PROCESSES LAUNCH THE DURABLE ENGINE above for
        what launching costs.
    """
    container = asyncio.run(start_container())
    # t-f11-04. AFTER `start_container`, deliberately: that is what creates the databases
    # and applies the schema when it is allowed to, so a preflight run before it would
    # refuse a deployment that was one bootstrap away from working.
    refuse_unless_ready(container.settings)

    container.domain_pool.open()
    container.audit_pool.open()
    if arguments.durable:
        # Before the console and after the pools, which is the order `serve()` and
        # `run_peer_worker_process` both use: recovery begins inside `launch`, and a
        # recovered workflow's first step reaches for a pool.
        launch_dbos(container)
    try:
        console = build_console(
            container,
            profile_id=arguments.profile,
            subject_id=arguments.subject,
            tenant_id=None if arguments.tenant is None else TenantId(arguments.tenant),
            channel=arguments.channel,
            roles=frozenset(arguments.roles or ()),
            session_id=(
                None if arguments.session is None else SessionId(arguments.session)
            ),
            durable=arguments.durable,
        )
        asyncio.run(console.run())
    finally:
        container.audit_pool.close()
        container.domain_pool.close()


def run_peer_worker_process(arguments: argparse.Namespace) -> None:
    """`python -m agent_core peer-worker` - the answering side of the mailbox. t-f11-43.

    THIN ON PURPOSE, LIKE EVERY OTHER SUBCOMMAND HERE. The claim, the identity, the fresh
    session and the answer all live in `adapters/driving/peers/worker.py`; what belongs in
    the entry point is which container the worker runs against and which agents it answers
    for. An entry point that grew the loop would be the second composition root this file
    exists not to become.

    WHY THE QUEUE IS CHECKED RATHER THAN BUILT. `Container.mailbox` is typed on the PORT
    (`composition.py` says why: D2's A2A adapter is a swap), and the ANSWERING half -
    `claim_next` - is deliberately not on that port. So the bound adapter either can be
    claimed from or it cannot, and this asks at startup instead of a minute later on the
    first ask. Building a `PgAgentMailbox` here instead would make this file choose a
    driven adapter again, which is exactly the debt t-f11-33 just paid off.

    IT DOES `launch_dbos`, AND THAT IS THE COMPOSITION DECISION THIS SUBCOMMAND MAKES
    (t-f11-49)
        It used to decline, quoting `run_console`'s reasoning. That reasoning does not
        survive the move: the console never wakes anybody, and this process must. Waking
        the turn that asked is `turn_workflow.signal_peer_answer`, which is
        `DBOS.send_async`, and send needs a launched DBOS in THIS process - so a worker
        that declines to launch is a worker that records an answer and wakes nobody. The
        asking turn then sleeps until its `reply_timeout_seconds` and reports that nobody
        replied, to a question that was answered minutes ago and is sitting on the row. A
        durable queue whose consumer cannot wake anything is a queue that drains into
        nothing, which is worse than having no consumer at all: the ask is consumed.

        WHAT LAUNCHING COSTS, NAMED RATHER THAN DISCOVERED. This process becomes a full
        executor of the same application: it dequeues `agent-core-turns` alongside
        `serve`, and it takes part in version-scoped startup recovery. That is acceptable
        rather than accidental, because `build_container` has already bound the workflow's
        collaborators through `bind_turn_workflow` before this line is reached
        (`adapters/driving/workflow/bootstrap.py` says so), and the pinned
        `application_version` is the same one `serve` claims - so any turn this process
        picks up is served by a fully-composed process, not a half-wired one. An operator
        who wants the two roles on separate capacity runs them on separate deployments,
        which is a deployment decision and not one this file can make for them.

        WHAT IT DOES NOT BUY, so the banner below stays honest: the PEER turn is still not
        durable. `DirectTurnRunner` calls `StartTurn` directly because the worker needs the
        OUTCOME in order to answer, and `enqueue_turn` returns a handle whose result would
        never come back here. Kill this process while B is thinking and that turn is gone -
        the ask stays `delivered` with a null answer and nobody recovers it
        (`adapters/driving/peers/worker.py`, WHAT HAPPENS WHEN THE WORKER DIES MID-TURN).
    """
    container = asyncio.run(start_container())
    refuse_unless_ready(container.settings)

    queue = container.mailbox
    if not isinstance(queue, PeerQueue):
        raise SystemExit(
            "peer-worker: the configured mailbox cannot be claimed from - it answers "
            "`ask` and `read_answer` but not `claim_next`, so there is no queue to drain. "
            "A deployment that talks to peers over A2A is answered by the far side, not "
            "by this process."
        )

    named = tuple(AgentId(agent) for agent in (arguments.agents or ()))
    targets = named if named else answerable_targets(container.profiles)
    if not targets:
        raise SystemExit(
            "peer-worker: no loaded profile sets `peers.enabled: true`, so there is no "
            "queue to drain. Name an agent with --agent, or enable peers on the profile "
            "that is meant to answer."
        )

    print(
        "[peer-worker] answering for: "
        + ", ".join(targets)
        + " - peer turns run through StartTurn directly, so one killed halfway is not "
        "recovered. This process launches the durable engine (it has to, to wake the turn "
        "that asked), so it also dequeues ordinary turns and takes part in recovery."
    )

    container.domain_pool.open()
    container.audit_pool.open()

    # t-f11-49. The durable engine, because the wake below is `DBOS.send_async` and send
    # needs one in this process. See IT DOES `launch_dbos` above for what that costs.
    # Before the loop and after the pools, which is the order `serve()` uses: recovery
    # begins inside `launch`, and a recovered workflow's first step reaches for a pool.
    launch_dbos(container)
    try:
        # t-f11-49. `wake` BOUND, and the loop is only a loop once it is. The worker
        # records the answer on the row; this is what tells the turn that asked. It is
        # `turn_workflow.signal_peer_answer` and never a send assembled here, for the
        # reason `dbos` is banned outside that package (pyproject.toml's TID251, the
        # mechanical form of CLAUDE.md non-negotiable #1): the topic, the payload type and
        # the idempotency key are one decision, and a second spelling of them would be a
        # second opinion about which turn an answer belongs to.
        asyncio.run(
            run_peer_worker(
                queue,
                DirectTurnRunner(container.start_turn),
                targets=targets,
                wake=signal_peer_answer,
                idle_seconds=arguments.poll,
            )
        )
    except KeyboardInterrupt:
        # The ordinary way an operator stops a polling process. The `finally` below still
        # closes the pools, which is the whole reason this is caught rather than unwound.
        print("[peer-worker] stopped.")
    finally:
        container.audit_pool.close()
        container.domain_pool.close()


def _argument_parser() -> argparse.ArgumentParser:
    """Four ways to run one container. No subcommand means `serve`, as it always did."""
    parser = argparse.ArgumentParser(
        prog="agent-core",
        description=(
            "Run the agent core as an HTTP process, an operator console or a peer worker."
        ),
    )
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="the HTTP process (the default)")
    subcommands.add_parser(
        "preflight",
        help=(
            "say what is not ready - database, credentials, models, profiles, policy - "
            "in one pass, and exit non-zero if anything is"
        ),
    )

    console = subcommands.add_parser(
        "console",
        help="an operator REPL: inspect an agent, talk to it, watch its tool calls",
    )
    console.add_argument(
        "--profile",
        default=None,
        help="which agent to start on; defaults to the first loaded profile by id",
    )
    console.add_argument(
        "--subject",
        default="operator",
        help="the subject_id every turn is attributed to (policy may branch on it)",
    )
    console.add_argument(
        "--tenant",
        default=None,
        help="the tenant to inspect; defaults to AGENT_CORE_CHANNEL_TENANT_ID",
    )
    console.add_argument(
        "--channel",
        default=_CONSOLE_CHANNEL_ID,
        help=(
            "the channel a turn is attributed to. Policy rules can be channel-scoped, so "
            "this changes what is allowed"
        ),
    )
    console.add_argument(
        "--role",
        action="append",
        dest="roles",
        default=None,
        help="a role for the caller; repeatable. Rules with no role constraint apply to any",
    )
    console.add_argument(
        "--session",
        default=None,
        help="continue a named session instead of a fresh one",
    )
    # t-f11-51. The durable path, as a flag rather than as a separate subcommand: it is the
    # SAME console, on the same container, with the same commands - what changes is which
    # route a typed sentence takes, and `:mode` moves between them once the process is up.
    console.add_argument(
        "--durable",
        action="store_true",
        help=(
            "start in durable mode: launch the DBOS engine in this process, enqueue every "
            "turn onto the durable queue and poll for it, exercising durability, "
            "publication and peer delegation. `:mode direct` switches back to the "
            "answer-now path at any time"
        ),
    )

    # t-f11-43. The answering side of a peer ask. Hyphenated rather than `peerworker`
    # because the other three read as words, and this is two.
    worker = subcommands.add_parser(
        "peer-worker",
        help=(
            "answer asks other agents left on the durable mailbox: claim one, run the "
            "turn as the TARGET agent under its own profile and policy, answer it"
        ),
    )
    worker.add_argument(
        "--agent",
        action="append",
        dest="agents",
        default=None,
        help=(
            "an agent whose queue to drain; repeatable. Defaults to every loaded profile "
            "with peers enabled"
        ),
    )
    worker.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="seconds to wait after a pass that found nothing; a busy queue never waits",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """The subcommand dispatch, and the only thing in this file that reads a command line.

    It lives HERE and not in `__main__.py` because that file says in its own docstring that
    it must stay three lines, and it is right: every decision about what the process IS
    belongs to this module. See WHICH PROCESSES LAUNCH THE DURABLE ENGINE in the module
    docstring for what each of the four actually starts.
    """
    arguments = _argument_parser().parse_args(argv)
    if arguments.command == "console":
        run_console(arguments)
        return
    if arguments.command == "preflight":
        run_preflight(arguments)
        return
    if arguments.command == "peer-worker":
        run_peer_worker_process(arguments)
        return
    serve()


def serve() -> None:
    """Run the process. Deliberately the only function here that performs I/O.

    The container is built ONCE, before the app, and handed in through the same seam a
    test uses - so the production path and the asserted path are the same path.

    Opening the pools is this function's job and not `build_app`'s. `composition.py`
    guarantees that building the container connects to nothing, so that a wiring mistake
    surfaces on a laptop with nothing running; the counterpart to that guarantee is that
    somebody has to open the pools afterwards, and it is whoever starts the process.

    It calls `start_container`, NOT `build_container`, and the difference is the schema.
    Nothing in production applied any migration until t-f1-23; every integration test
    applied its own by hand, so the suite was green and a fresh deployment had no tables.
    A process that builds the container without migrating is that gap reopened at the one
    place it is invisible - the entry point nobody writes a test for.
    """
    # Imported here rather than at module scope so that building the app - which every
    # test does - never requires a server to be installed.
    import uvicorn

    container = asyncio.run(start_container())
    # t-f11-04. Before the socket, after the bootstrap - see `run_console` for why that
    # order. A process that binds a port and then fails every turn on a missing credential
    # is the failure ladder with a listening socket in front of it.
    refuse_unless_ready(container.settings)
    app = build_app(container_factory=lambda: container)

    container.domain_pool.open()
    container.audit_pool.open()

    # The queue a turn is enqueued onto does not exist until DBOS is launched, and
    # `launch_dbos` is the only thing that reads the pinned `application_version` this
    # container carries. A launch site that built its own config would reopen t-f2-12 -
    # a worker killed mid-turn silencing that session forever - and the symptom of that
    # defect is silence, so nothing here would say so.
    launch_dbos(container)
    try:
        uvicorn.run(
            app,
            # Loopback by default. A process that binds every interface the moment it is
            # started is a deployment decision, and it is made by setting the variable.
            host=os.environ.get(_HOST_ENV, "127.0.0.1"),
            port=int(os.environ.get(_PORT_ENV, "8000")),
        )
    finally:
        container.audit_pool.close()
        container.domain_pool.close()


if __name__ == "__main__":  # pragma: no cover - `python -m agent_core.main [console]`
    main()
