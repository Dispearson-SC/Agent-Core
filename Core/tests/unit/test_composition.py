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
import dataclasses
import importlib
import inspect
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

import agent_core
from agent_core.adapters.driving.channels.registry import OutboundMessage
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.turn import CallerIdentity, TenantId, TurnId, Usage

PACKAGE_ROOT = Path(agent_core.__file__).resolve().parent
COMPOSITION = PACKAGE_ROOT / "composition.py"
MAIN = PACKAGE_ROOT / "main.py"
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


# The seats `StartTurn` takes, and the method on each one that the FIRST turn actually
# reaches - `StartTurn.execute`, read top to bottom, plus the two the runner resolves
# through the provider. Nothing speculative is listed: a method a first turn never calls
# cannot make a container "build and die on the first turn", which is the property below.
#
# `skills` is the one seat `execute` does not call yet (F6 wires the reading in), so its
# port methods stand in - it is held by the use case today, and a stub there would be a
# stub nobody noticed until the phase that needs it.
#
# DELIBERATELY ABSENT: `ConversationStore.save_checkpoint` and `latest_checkpoint`, which
# still raise for F5 (docs/TASKS.md). They are not a broken seat, they are an unbuilt
# phase: no first turn touches them, `CompactContext` is what will. Listing them would
# make this guard fail for something it is not about; leaving them unnamed would let that
# look like an oversight, so they are named here instead.
_FIRST_TURN_SEATS: dict[str, tuple[str, ...]] = {
    "runner": ("run",),
    "tools": ("toolset_for", "tool_names_for"),
    "policy": ("load_rules", "filter_toolset"),
    "store": ("append_request", "load_history", "append_outcome"),
    "audit": ("record_turn_end",),
    "context": ("update_from_response",),
    "skills": ("index", "read"),
}


def _body_raises_not_implemented(method: Any) -> bool:
    """True when `method`'s own body is a `raise NotImplementedError`.

    Lexical, over the AST of that one function, for the same reason the import rule above
    is: it is a question about what the source says, and `hasattr` cannot ask it. A
    placeholder answers `hasattr` exactly as a real adapter does, which is how a seat gets
    called filled while the container it is in dies on the first turn.

    Only the method's OWN body counts. A helper it calls may legitimately refuse some
    input - `LocalToolProvider._resolve` raises for an MCP profile it cannot serve - and
    that is a refusal on a path, not an unwritten method.
    """
    source = textwrap.dedent(inspect.getsource(method))
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(raised, ast.Name) and raised.id == "NotImplementedError":
            return True
    return False


@pytest.mark.phase("F1")
def test_the_container_carries_the_runner_and_a_start_turn_that_survives_its_first_use() -> None:
    """The SEATS note in `composition.py`, in executable form.

    WHY THIS GUARD EXISTS, WHICH IS NOT THE SAME AS WHAT IT USED TO ASSERT
        It used to assert `not hasattr(container, "start_turn")`, because `ToolProvider`,
        `ContextEngine` and `SkillRegistry` had no constructible adapter and
        `PgConversationStore.append_outcome` still raised. Refusing to construct the use
        case was the honest thing to do then, and the test said so.

        t-f1-21 and t-f1-22 filled the last two of those, so the absence no longer holds -
        but the ABSENCE WAS NEVER THE PROPERTY WORTH PROTECTING. The property is that the
        container never hands out a `start_turn` that dies on first use, and `not hasattr`
        was only the shape that took while a seat was empty. So it is inverted rather than
        deleted, and inverted into something stronger: every collaborator is present AND
        none of the methods a first turn reaches is a `raise NotImplementedError`.

        A deleted guard and a green suite look identical from the outside. That is why
        this file writes the reason down instead of removing the test.

    Also, still: the runner holds NO turn state, because `run` takes the `turn_id` per call
    and a container-level id would be the same id for every turn in the process.
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

    start_turn = getattr(container, "start_turn", None)
    assert start_turn is not None, (
        "the container exposes no start_turn, so nothing can run a turn. If a seat was "
        "genuinely emptied again, `build_container` refusing to construct it is the right "
        "answer - and the SEATS note in composition.py must name the empty seat."
    )
    assert isinstance(
        start_turn, importlib.import_module("agent_core.application.start_turn").StartTurn
    )

    # The use case holds the container's OWN adapters, not a second set built beside them.
    # Identity, not type: two `PgToolPolicy` objects on two different pools both pass an
    # isinstance check while enforcing against different rows.
    held = {id(collaborator) for collaborator in vars(start_turn).values()}
    for seat in _FIRST_TURN_SEATS:
        assert id(getattr(container, seat)) in held, (
            f"start_turn does not hold the container's {seat!r}. The container exposes the "
            "adapters alongside the use case so an operator tool can reach one without "
            "rebuilding the world; if they are not the same object, the two disagree."
        )

    stubs = [
        f"{seat}.{method}"
        for seat, methods in _FIRST_TURN_SEATS.items()
        for method in methods
        if _body_raises_not_implemented(getattr(getattr(container, seat), method))
    ]
    assert stubs == [], (
        f"the container is wired, and these seats still raise NotImplementedError: {stubs}. "
        "That is a container that builds and then dies on the first turn - the exact shape "
        "of bug the composition root exists to prevent. Either the adapter is finished or "
        "build_container refuses to construct StartTurn and names the seat."
    )


@pytest.mark.phase("F3")
def test_every_http_started_turn_has_a_channel_to_leave_on() -> None:
    """`routes.py` stamps a channel on every HTTP turn; `_step_deliver` looks it up.

    `_authenticate` falls back to `_DEFAULT_CHANNEL` whenever no `X-Channel` header is
    sent, so the id is on the caller of every `POST /turns` turn - and `_step_deliver`
    (t-f3-07) ends the turn by asking the registry for exactly that id. A registry holding
    only telegram and whatsapp therefore kills EVERY HTTP turn with `UnknownChannelError`
    at the last step, after the model has already been paid for.

    The id is read from `routes.py` rather than restated here: two spellings of "http"
    would let this pass while production still dies.
    """
    composition = _composition()
    routes = importlib.import_module("agent_core.adapters.driving.http.routes")
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    assert routes._DEFAULT_CHANNEL in container.channels, (
        f"nothing is registered under {routes._DEFAULT_CHANNEL!r}, the channel routes.py "
        f"stamps on every HTTP turn. Registered: {list(container.channels.channel_ids())}. "
        "Every POST /turns turn would finish and then fail at _step_deliver."
    )

    # And it must be a channel that SUCCEEDS while sending nothing: D23 and docs/GAPS.md A4
    # make the HTTP surface 202-plus-poll, so the answer is already stored for a later GET
    # and there is nowhere to push it. Delivery still has to complete, or the turn fails
    # for having been answered the way this surface is designed to answer.
    caller = CallerIdentity(
        subject_id="u-1",
        channel=routes._DEFAULT_CHANNEL,
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )
    asyncio.run(container.channels.deliver(caller, OutboundMessage(text="the answer")))


@pytest.mark.phase("F3")
def test_a_glazed_started_turn_has_a_channel_to_leave_on() -> None:
    """The Glazed backend starts turns on channel `glazed` and polls `GET /turns/{id}`.

    Without a registration the peer-worker's turns died at `_step_deliver` with
    `UnknownChannelError: no channel registered under 'glazed'`.
    """
    composition = _composition()
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    assert "glazed" in container.channels
    caller = CallerIdentity(
        subject_id="m-1",
        channel="glazed",
        tenant_id=TenantId("S030"),
        roles=frozenset({"glazed-manager"}),
    )
    asyncio.run(container.channels.deliver(caller, OutboundMessage(text="la respuesta")))


@pytest.mark.phase("F2")
def test_building_the_container_binds_the_dbos_workflow_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TurnWorkflowNotWiredError` names this file, so this file has to do the wiring.

    The workflow function is a module-level object - that is how DBOS registers it - so its
    collaborators cannot be arguments and are bound once at startup instead. Nothing but
    the composition root may do that: a workflow that lazily built its own `StartTurn`
    would be a second composition root, and the two would drift.

    Until this binding exists, `build_container` produces a container the DBOS path cannot
    use at all: every workflow dies on its first step with `TurnWorkflowNotWiredError`, and
    the only reason no test noticed is that every workflow test binds its own fakes.
    """
    # Snapshot-and-restore: the binding is a process-wide global by design, so building a
    # container from a test would otherwise leak a real one into every test after it.
    monkeypatch.setattr(turn_workflow, "_dependencies", turn_workflow._dependencies)

    composition = _composition()
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    dependencies = turn_workflow._dependencies
    assert dependencies is not None, (
        "build_container never called turn_workflow.bind_dependencies(). The composition "
        "root is where that function's own error message says it is wired."
    )
    assert dependencies.start_turn is container.start_turn, (
        "the workflow was bound to a different StartTurn than the container exposes; the "
        "DBOS path and the HTTP path would then run turns through two different wirings"
    )
    assert dependencies.channels is container.channels, (
        "the workflow was bound to a different ChannelRegistry than the container holds. "
        "D23 Shape A is ONE lookup table shared by the mid-turn prompt and _step_deliver."
    )


# ---------------------------------------------------------------------------
# THE SEAT GUARD - docs/TASKS.md#t-f7-12
#
# docs/STATE.md has now recorded the same shape five times: a driving adapter declares an
# optional collaborator, every test of that adapter injects one, and NOTHING in the
# production path ever binds it. The `ToolProvider` adapter, the day-1 model endpoint,
# every migration, `lookup_turn`/`decide`, and the two evidence seats. Each time, THE
# FIXTURE THAT SUPPLIES THE MISSING COLLABORATOR IS EXACTLY WHAT STOPS ANYONE NOTICING IT
# IS MISSING - so wiring the seat of the day fixes one instance and leaves the mechanism
# intact for the sixth.
#
# THE SEAT LIST IS DERIVED, NEVER WRITTEN DOWN. It comes from
# `inspect.signature(routes.create_app)` and `dataclasses.fields(TurnWorkflowDependencies)`
# - the declarations themselves. A hand-maintained list here would be the same defect with
# an extra step: the next agent adds a seat and forgets the line, and the guard goes quiet
# about exactly the thing it exists to shout about.
#
# WHAT IS AND IS NOT GENERAL ABOUT THIS
#     The SEAT side is fully general and needs no maintenance. `_UNBOUND_BY_DESIGN` below
#     is the one hand-written set, and it is the INVERSE of the defect rather than a repeat
#     of it: an inclusion list fails OPEN (forget an entry, lose the coverage silently) and
#     an exemption list fails CLOSED (forget an entry, the suite goes red). The only way to
#     silence this guard is to add a line that says, in writing, why a seat is deliberately
#     empty - which is the argument the composition root's SEATS note already asks for.
# ---------------------------------------------------------------------------

# Seats a production process deliberately leaves empty, each with the reason. An entry here
# is a claim somebody has to defend in review; an absence is a red suite.
_UNBOUND_BY_DESIGN: dict[str, str] = {
    "pending_inputs": (
        "D19 coalescing is only half-deployed: `main.py` binds `_start_turn_on_the_queue` "
        "(a plain `enqueue_turn`) and not `routes.coalescing_turn_starter`, so nothing "
        "ever APPENDS to the buffer. Binding the drain alone would drain a table no "
        "writer feeds. Wiring the starter is a deployment decision this guard must not "
        "make silently - see the seat's own comment in turn_workflow.py."
    ),
    "compaction": (
        "`CompactionSeat`'s own docstring makes an unbound seat a legitimate deployment "
        "('this deployment does not compact', and `_step_compact` returns rather than "
        "raising). It also needs a `context_window` number that docs/STATE.md records as "
        "nobody's yet, and the seat refuses to be half-stated. Reported, not smuggled in."
    ),
}


def _declared_route_seats() -> tuple[str, ...]:
    """Every collaborator `routes.create_app` accepts, read off the signature itself."""
    routes = importlib.import_module("agent_core.adapters.driving.http.routes")
    return tuple(inspect.signature(routes.create_app).parameters)


def _declared_workflow_seats() -> tuple[str, ...]:
    """Every collaborator the DBOS workflow resolves through, read off the dataclass."""
    return tuple(
        field.name
        for field in dataclasses.fields(turn_workflow.TurnWorkflowDependencies)
    )


def _keywords_of_call(path: Path, callee: str) -> set[str]:
    """The keyword names `path` passes at every call to `callee`.

    An AST walk rather than a runtime capture, for the reason the import rules above give:
    it is a question about what the source SAYS, and it can be asked without constructing
    anything. Positional arguments are refused rather than counted - a seat passed
    positionally would be invisible to this guard, and both construction sites are
    keyword-only today.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    sites = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != callee:
            continue
        sites += 1
        assert not node.args, (
            f"{path.name} calls {callee}() with positional arguments. This guard reads "
            "keyword names to decide which seats were bound; a positional one would be "
            "invisible to it."
        )
        found.update(keyword.arg for keyword in node.keywords if keyword.arg is not None)

    assert sites, (
        f"{path.name} never calls {callee}(). The seat guard has lost its production "
        "construction site, so it is asserting nothing - find where the call moved to."
    )
    return found


@pytest.mark.phase("F7")
@pytest.mark.contract
def test_every_driving_seat_is_named_at_its_production_construction_site() -> None:
    """Every declared seat is passed where the process actually assembles itself.

    Two construction sites, because there are two driving adapters with seats: the HTTP
    surface is assembled in `main.build_app` and the DBOS workflow in
    `composition.bind_turn_workflow`. Neither list of seats is written here - both are
    read from the declaration - so a seat added tomorrow is covered the moment it exists.
    """
    checks = (
        (
            "main.py's create_app(...)",
            _declared_route_seats(),
            _keywords_of_call(MAIN, "create_app"),
        ),
        (
            "composition.py's TurnWorkflowDependencies(...)",
            _declared_workflow_seats(),
            _keywords_of_call(COMPOSITION, "TurnWorkflowDependencies"),
        ),
    )

    # The exemption list is the one hand-written thing here, so it is itself checked. An
    # entry naming a seat that no longer exists - or one that has since been bound - is a
    # standing permission slip for whatever is named next, which is how an exemption list
    # decays into the inclusion list this guard refuses to be.
    all_declared = {seat for _, declared, _ in checks for seat in declared}
    all_named = {seat for _, _, named in checks for seat in named}
    stale = sorted(
        f"{seat!r} (no longer declared)"
        for seat in _UNBOUND_BY_DESIGN
        if seat not in all_declared
    ) + sorted(
        f"{seat!r} (bound now)" for seat in _UNBOUND_BY_DESIGN if seat in all_named
    )
    assert stale == [], (
        f"_UNBOUND_BY_DESIGN carries entries that no longer describe anything: {stale}. "
        "Delete them - an exemption for a seat that is gone or already wired excuses the "
        "next seat to be given that name."
    )

    unbound = [
        f"{site}: {seat!r}"
        for site, declared, named in checks
        for seat in declared
        if seat not in named and seat not in _UNBOUND_BY_DESIGN
    ]

    assert unbound == [], (
        "These seats are declared by a driving adapter and never bound where the process "
        f"assembles itself: {unbound}. The adapter's own tests inject them, so the suite "
        "stays green while the production route refuses or the workflow step dies - the "
        "shape docs/STATE.md has now recorded five times. Either bind the seat at the site "
        "named above, or add it to _UNBOUND_BY_DESIGN with the reason it is empty."
    )


@pytest.mark.phase("F7")
def test_build_app_hands_create_app_a_real_object_for_every_route_seat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: a seat can be NAMED at the call site and still arrive as `None`.

    The test above reads the source and cannot see that. This one runs the real
    `build_app` over a real `build_container` - no database, exactly as every other test
    in this file - and records what `create_app` was actually handed. `routes.create_app`
    treats `None` as "this deployment has no such collaborator" and makes the route refuse,
    which is right for a library and never right for the process `serve()` starts.
    """
    # The workflow binding is a process-wide global by design; snapshot and restore it so
    # building a container here does not leak into the tests that follow.
    monkeypatch.setattr(turn_workflow, "_dependencies", turn_workflow._dependencies)

    main = importlib.import_module("agent_core.main")
    composition = _composition()
    container = composition.build_container(pool_factory=cast(Any, _RecordingPool))

    handed: dict[str, Any] = {}

    def _record(**seats: Any) -> Any:
        handed.update(seats)
        # Just enough of an app for `build_app` to finish: two routers and one attribute.
        return SimpleNamespace(
            include_router=lambda router: None, state=SimpleNamespace()
        )

    monkeypatch.setattr(main, "create_app", _record)
    main.build_app(container_factory=lambda: container)

    unbound = sorted(
        seat
        for seat in _declared_route_seats()
        if seat not in _UNBOUND_BY_DESIGN and handed.get(seat) is None
    )

    assert unbound == [], (
        f"main.build_app() handed create_app nothing for these seats: {unbound}. Each one "
        "makes its route answer 503 (or, for signal_evidence, stores a file and never wakes "
        "the turn that asked for it) in every process serve() starts, while the tests "
        "written against routes.py stay green by injecting the seat themselves."
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
