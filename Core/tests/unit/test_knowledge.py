"""Knowledge access rules.

Phase:   F8 - Knowledge retrieval
Tasks:   docs/TASKS.md#t-f8-01, docs/TASKS.md#t-f8-07, docs/TASKS.md#t-f8-08
Status:  t-f8-01 PINNED (KnowledgePolicy.can_read, and the tenant narrowing).
         t-f8-07 PINNED (no write path is reachable from the object a chat turn holds).
         t-f8-08 PINNED (cross-collection retrieval returns empty, and never raises).
         The three sections below are kept apart on purpose: t-f8-01 is about a domain
         rule, t-f8-07 about an absence, t-f8-08 about a behaviour.

WHY THIS FILE EXISTS AT ALL
    `collections` is a permission boundary, not an organising convenience, so the empty
    case is the case that matters: an agent that was never granted a collection must
    retrieve NOTHING, not everything. The asymmetry against `PolicyRule.subject_roles`,
    where empty means "any subject", is deliberate - a permissive default is acceptable
    for a convenience feature and unacceptable for data access - and an asymmetry that is
    only written down in a docstring is one refactor away from being read the other way.

    The domain is sync and imports nothing external (CLAUDE.md, "Layer rules"), so these
    cases construct a policy and call a method. No database, no fixtures, no I/O.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any

import pytest

# Imported as MODULES so a missing name fails inside the test that needs it rather than
# at collection, taking the whole file down with it.
import agent_core.adapters.driven.knowledge_pg.repository as repository_module
import agent_core.domain.knowledge as knowledge_module
import agent_core.ports.knowledge_admin as admin_module
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import CallerIdentity, TenantId

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"
PACKAGE_DIR = SRC_DIR / "agent_core"

# Names an agent might plausibly be handed, including the ones a caller would guess.
CANDIDATE_COLLECTIONS = (
    "delivery",
    "fraud",
    "pricing",
    "public",
    "",
    "*",
    "default",
)


def _collection(name: str) -> knowledge_module.CollectionId:
    return knowledge_module.CollectionId(name)


def test_can_read_denies_every_collection_when_none_are_granted() -> None:
    """Empty `collections` means NOTHING, not everything.

    The default-constructed policy is the shape an `AgentProfile` gets when its file says
    nothing about knowledge, which is the shape most profiles will have.
    """
    policy = knowledge_module.KnowledgePolicy()

    assert policy.collections == ()

    for name in CANDIDATE_COLLECTIONS:
        assert policy.can_read(_collection(name)) is False, (
            f"empty `collections` granted read on {name!r}; "
            "empty must mean nothing, not everything"
        )


def test_can_read_denies_every_collection_when_enabled_but_none_are_granted() -> None:
    """`enabled=True` is not a grant.

    Turning the feature on says the agent may retrieve; `collections` says from where.
    Collapsing the two would make `enabled=True` a wildcard, which is exactly the
    permissive default this port refuses.
    """
    policy = knowledge_module.KnowledgePolicy(enabled=True)

    for name in CANDIDATE_COLLECTIONS:
        assert policy.can_read(_collection(name)) is False, (
            f"`enabled=True` alone granted read on {name!r}; "
            "enabling retrieval is not granting a collection"
        )


def test_can_read_grants_only_the_named_collections() -> None:
    """The counterpart: without this, `can_read` returning False always would pass above.

    A deny-everything implementation satisfies the two cases before it, so the grant case
    has to be pinned in the same file or the guard proves nothing.
    """
    granted = _collection("delivery")
    policy = knowledge_module.KnowledgePolicy(enabled=True, collections=(granted,))

    assert policy.can_read(granted) is True
    assert policy.can_read(_collection("fraud")) is False


def test_knowledge_policy_is_frozen() -> None:
    """A permission boundary that can be reassigned at runtime is not a boundary."""
    policy = knowledge_module.KnowledgePolicy()

    with pytest.raises(FrozenInstanceError):
        policy.collections = (_collection("fraud"),)  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# The tenant narrowing. See the t-f8-01 correction note in docs/TASKS.md.
# --------------------------------------------------------------------------------------
#
# `ports/knowledge_base.py` instructs its adapter to "filter by tenant IN THE QUERY", and
# for as long as the whole contract was `(policy, query, collections)` no adapter could
# obey it: there was no tenant anywhere to put in the predicate. The fix is the one
# `RuleSet` already uses for roles and channel - a value narrowed for exactly one tenant,
# so the predicate has a source and no call site can supply a different one.


def _caller(tenant: str) -> CallerIdentity:
    return CallerIdentity(subject_id="u-1", channel="whatsapp", tenant_id=TenantId(tenant))


def test_the_profile_policy_still_carries_no_tenant() -> None:
    """The CONFIG type stays tenant-free, and that is why the narrowing is its own type.

    `KnowledgePolicy` is loaded from a profile YAML file by `_build_knowledge_policy`,
    which derives the accepted keys from `fields(KnowledgePolicy)`. A `tenant_id` field
    here would therefore become a YAML key, and a profile that names its own tenant is the
    breach wearing a config file's clothes.
    """
    names = {f.name for f in fields(knowledge_module.KnowledgePolicy)}

    assert "tenant_id" not in names, (
        "KnowledgePolicy grew a tenant field. It is profile configuration, so the field "
        "would become an authorable YAML key - the tenant must come from the caller, "
        "never from the file that describes the agent."
    )


def test_a_narrowed_policy_cannot_be_built_without_naming_a_tenant() -> None:
    """The omission is a construction error, not a review comment.

    Every other field of a `KnowledgePolicy` has a default, so a narrowing whose tenant
    could be defaulted would be a narrowing nobody has to perform.
    """
    with pytest.raises(TypeError):
        knowledge_module.TenantKnowledgePolicy()  # type: ignore[call-arg]


def test_the_narrowing_takes_the_tenant_from_the_caller() -> None:
    """`for_caller` is the one intended construction path, exactly like `RuleSet.for_caller`.

    The tenant that scopes the query then always comes from the identity the turn is being
    served for, rather than being copied by hand at a call site where it can be copied
    wrong.
    """
    policy = knowledge_module.KnowledgePolicy(
        enabled=True, collections=(_collection("pricing"),)
    )

    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(_caller("tenant-a"), policy)

    assert narrowed.tenant_id == TenantId("tenant-a")


def test_the_narrowing_copies_every_setting_of_the_policy() -> None:
    """A drift lock: a field added to `KnowledgePolicy` must survive `for_caller`.

    `for_caller` copies the settings one by one, which is readable but is exactly the shape
    that silently drops a field added later. A dropped `min_score` would mean retrieval
    quietly falls back to the default floor for every tenant - no exception, no failing
    test anywhere else.
    """
    policy = knowledge_module.KnowledgePolicy(
        enabled=True,
        collections=(_collection("pricing"), _collection("delivery")),
        mode=knowledge_module.RetrievalMode.HYBRID,
        top_k=11,
        min_score=0.83,
        max_context_chars=1234,
        inject_into_prompt=True,
    )

    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(_caller("tenant-a"), policy)

    for f in fields(knowledge_module.KnowledgePolicy):
        assert getattr(narrowed, f.name) == getattr(policy, f.name), (
            f"`for_caller` did not carry {f.name} across; the narrowed policy is not the "
            f"profile's policy any more."
        )


def test_a_narrowed_policy_cannot_be_repointed_at_another_tenant() -> None:
    """Frozen, like every other permission boundary in this package.

    Without this, an adapter holding tenant A's policy could set the field before building
    its query, and the whole narrowing would be advisory.
    """
    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(
        _caller("tenant-a"), knowledge_module.KnowledgePolicy(enabled=True)
    )

    with pytest.raises(FrozenInstanceError):
        narrowed.tenant_id = TenantId("tenant-b")  # type: ignore[misc]


def test_a_narrowed_policy_is_still_a_knowledge_policy() -> None:
    """The positive control. A narrowing that broke `can_read` would be no use to anyone.

    It is a subclass on purpose: the collection grant and the tenant are ONE value, so
    there is no second object a call site could pair with the wrong first one - which is
    the failure mode a separate `tenant` parameter would have had.
    """
    granted = _collection("pricing")
    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(
        _caller("tenant-a"),
        knowledge_module.KnowledgePolicy(enabled=True, collections=(granted,)),
    )

    assert isinstance(narrowed, knowledge_module.KnowledgePolicy)
    assert narrowed.can_read(granted) is True
    assert narrowed.can_read(_collection("fraud")) is False


# --------------------------------------------------------------------------------------
# t-f8-07 - a chat caller cannot reach ANY write path.
# --------------------------------------------------------------------------------------
#
# Non-negotiable #8 is a claim about an OBJECT, not about a Protocol: "a prompt injection
# cannot call a method that is not on the object the agent holds". test_ports_knowledge_
# base.py pins the Protocol's surface and test_ports_knowledge_admin.py pins the type wall
# around `AdminIdentity`. Neither of them ever builds the thing a turn actually holds, and
# a Protocol proves nothing about a concrete class that satisfies it and then adds a
# method of its own.
#
# So this section takes the read handle a turn would be injected with - a real
# `PgKnowledgeBase` - and asks the three questions that are left:
#
#   1. does the object have a write on it, under any name;
#   2. can the module that defines it even SEE the write port;
#   3. does a call site that tries to write through it compile.
#
# Each half is checked against a deliberately broken subject first. An absence test that
# scans nothing passes exactly as loudly as one that scans everything.


def _refuse_to_connect() -> Any:
    """A `ConnectionFactory` that fails if retrieval ever reaches the database.

    Used wherever the assertion is "this never queried". A test that let a real connection
    be attempted would be asserting a mock's behaviour instead of the adapter's.
    """
    raise AssertionError("the knowledge repository opened a connection it should not have")


def _read_handle() -> repository_module.PgKnowledgeBase:
    """The object a turn holds. Read-only by construction, not by convention."""
    return repository_module.PgKnowledgeBase(_refuse_to_connect)


class _PoisonedReadHandle(repository_module.PgKnowledgeBase):
    """The known-bad control: a read handle that grew a write.

    This is exactly the shape of the mistake - somebody needs to persist something on the
    retrieval path, the read adapter is already wired in, and one method appears on it. The
    screens below must report this object and must not report the real one.
    """

    async def upsert(self, scope: object, doc: object) -> None:  # pragma: no cover
        raise AssertionError("never called; this class exists to be rejected")


# Every method a write would arrive as. Named after the admin port's surface, which
# `tests/unit/test_ports_knowledge_admin.py` freezes as an equality - so a fourth write
# method cannot appear there without that anchor being reopened and this list revisited.
ADMIN_METHODS = frozenset({"upsert", "delete", "list_docs"})

# Second line: a write renamed into something innocuous. Substring match, so `bulk_upsert`
# and `set_price` are both caught. None of `search`, `get` or `full_context` contains one.
MUTATING_VERBS = (
    "write",
    "upsert",
    "insert",
    "update",
    "delete",
    "remove",
    "drop",
    "purge",
    "truncate",
    "create",
    "add",
    "edit",
    "patch",
    "save",
    "store",
    "persist",
    "publish",
    "supersede",
    "ingest",
    "import",
    "put",
    "set",
    "commit",
    "apply",
)


def _writes_on(handle: object) -> set[str]:
    """Every public attribute of `handle` that could carry a write.

    Read off the live object with `dir()`, so an attribute inherited, monkey-patched in, or
    added by a subclass counts. The question is what an injected prompt can reach, and it
    reaches attributes, not annotations.
    """
    public = {name for name in dir(handle) if not name.startswith("_")}
    return {name for name in public if name in ADMIN_METHODS} | {
        name for name in public for verb in MUTATING_VERBS if verb in name.lower()
    }


@pytest.mark.phase("F8")
def test_the_write_screen_reports_a_read_handle_that_grew_a_write() -> None:
    """The control. Without it, the assertion below would pass on an empty `dir()`.

    `docs/WAVES.md` calls a test that certifies nothing while reading as coverage a
    green-on-empty test, and an absence test is the easiest kind to write that way.
    """
    assert _writes_on(_PoisonedReadHandle(_refuse_to_connect)) == {"upsert"}, (
        "The screen did not notice a write bolted onto a read handle. Every clean result "
        "it reports below is worthless until this passes."
    )


@pytest.mark.phase("F8")
def test_the_handle_a_turn_holds_carries_no_write_at_all() -> None:
    """Non-negotiable #8, asserted against the object rather than against the Protocol.

    A prompt injection reaches the methods that are ON the instance. `PgKnowledgeBase`
    satisfies a read-only Protocol, which constrains what it must have and says nothing
    about what it may add - so the Protocol test and this one are different questions and
    neither covers the other.
    """
    handle = _read_handle()
    writes = _writes_on(handle)

    assert writes == set(), (
        f"The knowledge handle a turn holds exposes {sorted(writes)}. Writes live on "
        f"KnowledgeAdmin, which is never injected into anything an agent touches: the "
        f"corpus is persistent, so poisoning it once contaminates every future "
        f"conversation."
    )
    assert {name for name in dir(handle) if not name.startswith("_")} == {
        "search",
        "get",
        "full_context",
    }


# Every name that means "write authority" if it turns up on the retrieval path. The
# adapter class is on the list beside the port: importing the concrete writer is the same
# reachability, and a scan that only knew the Protocol's name would report a module clean
# while it held the thing that actually executes the SQL.
WRITE_AUTHORITY_NAMES = frozenset(
    {
        "KnowledgeAdmin",
        "PgKnowledgeAdmin",
        "AdminIdentity",
        "AdminSubjectId",
        "TenantAdminScope",
    }
)

# The modules a chat turn's retrieval path is made of: the port it is typed as, and the
# adapter it is injected with.
READ_PATH_MODULES = (
    PACKAGE_DIR / "ports" / "knowledge_base.py",
    PACKAGE_DIR / "adapters" / "driven" / "knowledge_pg" / "repository.py",
)

WRITE_PATH_MODULE = PACKAGE_DIR / "adapters" / "driven" / "knowledge_pg" / "admin.py"

KNOWN_BAD_READ_PATH = """
from agent_core.ports.knowledge_admin import KnowledgeAdmin


class PgKnowledgeBase:
    def __init__(self, admin: KnowledgeAdmin) -> None:
        self._admin = admin
"""


def _imported_names(source: str) -> set[str]:
    """Every name this module binds through an import, alias and module segment included.

    Aliases count because `from ...knowledge_admin import KnowledgeAdmin as _Writer` is the
    same reachability wearing a different label, and it is what somebody writes when they
    already suspect the import does not belong.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {alias.name for alias in node.names}
            names |= {alias.asname for alias in node.names if alias.asname is not None}
            if node.module:
                names |= set(node.module.split("."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names |= set(alias.name.split("."))
                if alias.asname is not None:
                    names.add(alias.asname)
    return names


@pytest.mark.phase("F8")
def test_the_import_scan_reports_a_read_module_that_reaches_for_the_write_port() -> None:
    """The control for the sweep below, checked against a module that does the forbidden
    thing. A scan with a typo in a type name reports every real module clean."""
    found = WRITE_AUTHORITY_NAMES & _imported_names(KNOWN_BAD_READ_PATH)

    assert found == {"KnowledgeAdmin"}, (
        f"The import scan missed the write port it was handed: {sorted(found)}."
    )


@pytest.mark.phase("F8")
@pytest.mark.parametrize("module_path", READ_PATH_MODULES, ids=lambda path: path.name)
def test_the_retrieval_path_cannot_even_name_the_write_port(module_path: Path) -> None:
    """The dependency arrow points write -> read, and it must never point back.

    This is the reachability half of non-negotiable #8, and it is stronger than checking a
    surface: a module that cannot import the write port cannot grow a write path later
    without the import appearing here first. `_writes_on` above catches a write once it
    exists; this catches the edit that would make one possible.
    """
    found = WRITE_AUTHORITY_NAMES & _imported_names(module_path.read_text(encoding="utf-8"))

    assert not found, (
        f"{module_path.relative_to(SRC_DIR)} imports {sorted(found)}. The object an agent "
        f"holds is built here; a module on the retrieval path that can name write "
        f"authority is one refactor away from handing it over."
    )


@pytest.mark.phase("F8")
def test_the_write_adapter_depends_on_the_read_one_and_not_the_reverse() -> None:
    """The arrow has to point somewhere, and this pins which way.

    Without it, the sweep above would also pass on a tree where the two adapters simply do
    not know each other, and the disjointness would be an accident rather than a direction.
    `knowledge_pg/admin.py` reuses `repository.py`'s `ConnectionFactory`, which is the
    correct direction: the writer may know the reader, the reader may never know the writer.
    """
    assert "repository" in _imported_names(WRITE_PATH_MODULE.read_text(encoding="utf-8")), (
        "The write adapter no longer imports the read one, so the read adapter's clean "
        "import list above proves only that the two are unrelated today."
    )


@pytest.mark.phase("F8")
def test_a_chat_caller_cannot_be_splatted_into_write_authority(
    caller: CallerIdentity,
) -> None:
    """The one widening spelling mypy is structurally blind to, tried from the chat side.

    `dataclasses.asdict` returns `dict[str, Any]` and unpacking `Any` is always legal, so
    the type checker cannot object here at all. The defence is the field names: the two
    identities share exactly one, and a `TenantAdminScope` needs an `admin` that nothing in
    a chat caller can become.
    """
    with pytest.raises(TypeError):
        admin_module.AdminIdentity(**dataclasses.asdict(caller))

    with pytest.raises(TypeError):
        admin_module.TenantAdminScope(**dataclasses.asdict(caller))


# --------------------------------------------------------------------------------------
# The mypy half of t-f8-07: is a write through the agent's handle even legal to write?
# --------------------------------------------------------------------------------------
#
# `dir()` sees names. It cannot see whether an EXPRESSION compiles, and "a chat caller
# cannot reach a write path" is a claim about expressions. Same technique, and the same
# reason, as tests/unit/test_ports_knowledge_base.py.


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module against the real ports.

    Written outside the repository tree on purpose: a fixture that deliberately fails to
    type-check must never be picked up by the project-wide mypy run.
    """
    module = tmp_path / "snippet.py"
    module.write_text(source, encoding="utf-8")

    env = dict(os.environ)
    env["MYPYPATH"] = str(SRC_DIR)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=str(CORE_DIR),
        env=env,
        check=False,
    )


THE_WRITE_ADAPTER_IS_A_KNOWLEDGE_ADMIN = """
from __future__ import annotations

from agent_core.adapters.driven.knowledge_pg.admin import PgKnowledgeAdmin
from agent_core.ports.knowledge_admin import KnowledgeAdmin


def admin_port(adapter: PgKnowledgeAdmin) -> KnowledgeAdmin:
    return adapter
"""

THE_READ_PORT_IN_AN_ADMIN_SEAT = """
from __future__ import annotations

from agent_core.ports.knowledge_admin import KnowledgeAdmin
from agent_core.ports.knowledge_base import KnowledgeBase


def admin_port(kb: KnowledgeBase) -> KnowledgeAdmin:
    return kb
"""

THE_READ_ADAPTER_IN_AN_ADMIN_SEAT = """
from __future__ import annotations

from agent_core.adapters.driven.knowledge_pg.repository import PgKnowledgeBase
from agent_core.ports.knowledge_admin import KnowledgeAdmin


def admin_port(kb: PgKnowledgeBase) -> KnowledgeAdmin:
    return kb
"""

A_WRITE_THROUGH_THE_AGENTS_HANDLE = """
from __future__ import annotations

from agent_core.domain.knowledge import KnowledgeDoc
from agent_core.ports.knowledge_base import KnowledgeBase


async def poison(kb: KnowledgeBase, doc: KnowledgeDoc) -> None:
    await kb.upsert(doc)
"""


@pytest.mark.phase("F8")
def test_the_write_port_is_satisfiable_by_its_own_adapter(tmp_path: Path) -> None:
    """The positive control. The rejections below must be about the READ handle.

    Without this, a `KnowledgeAdmin` that nothing on earth satisfies would make every
    assertion in this section pass while the admin route was broken.
    """
    result = _type_check(THE_WRITE_ADAPTER_IS_A_KNOWLEDGE_ADMIN, tmp_path)

    assert result.returncode == 0, (
        "PgKnowledgeAdmin no longer satisfies KnowledgeAdmin, so the refusals below prove "
        f"nothing about the read handle.\n{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("the port", THE_READ_PORT_IN_AN_ADMIN_SEAT),
        ("the adapter", THE_READ_ADAPTER_IN_AN_ADMIN_SEAT),
    ],
    ids=["port", "adapter"],
)
def test_the_agents_handle_is_not_accepted_where_write_authority_is_required(
    name: str, source: str, tmp_path: Path
) -> None:
    """Both are `Protocol`s, so satisfaction is by shape - and the shapes must stay disjoint.

    The moment the two surfaces overlap, an object handed out as a read-only handle starts
    to structurally satisfy the write port, and "different types, different routes" becomes
    a naming convention. The adapter is checked as well as the port because it is the
    concrete class that would drift: adding one method to it is a smaller-looking edit than
    adding one to a Protocol with a docstring forbidding it.
    """
    result = _type_check(source, tmp_path)

    assert result.returncode != 0, (
        f"The read handle ({name}) was accepted as a KnowledgeAdmin. Nothing then stops a "
        f"turn from being wired with an object that can edit the corpus.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
def test_a_write_through_the_agents_handle_does_not_type_check(tmp_path: Path) -> None:
    """The attack spelled out at the call site: `await kb.upsert(doc)`.

    No cast, no `Any`, no ignore comment - the honest-looking version, which is the one that
    gets written. It has to fail because the METHOD IS ABSENT, so the rejection is checked
    to be `attr-defined` rather than any incidental error: a signature mismatch would mean
    the method exists and merely disagreed about its arguments.
    """
    result = _type_check(A_WRITE_THROUGH_THE_AGENTS_HANDLE, tmp_path)

    assert result.returncode != 0, (
        "KnowledgeBase.upsert type-checked. There is no knowledge-write tool, ever "
        f"(non-negotiable #8).\n{result.stdout}{result.stderr}"
    )
    assert "attr-defined" in result.stdout, (
        "Expected the rejection to be that the attribute does not EXIST. Any other error "
        "means a write method is present and only its signature was wrong.\n"
        f"{result.stdout}{result.stderr}"
    )


# --------------------------------------------------------------------------------------
# t-f8-08 - a collection absent from a profile returns empty, and never raises.
# --------------------------------------------------------------------------------------
#
# The cross-TENANT half of this anchor is not here, and docs/TASKS.md explains why: the
# "tenant A asks for tenant B" call no longer type-checks, so it cannot be written as a
# domain case at all. It is proven structurally in tests/unit/test_ports_knowledge_base.py
# and behaviourally against the SQL in the adapter's own suite. Only cross-COLLECTION
# stays here.
#
# The property is precisely "indistinguishable", not "empty". An error would confirm that
# the collection exists, and the confirmation IS the leak - a fraud agent probing for
# `pricing` learns whether the business has one either way. So a forbidden collection and a
# permitted collection with nothing to match must produce the SAME answer, and the
# forbidden one must not even reach the database: a query is a side effect, and a side
# effect is an answer to somebody watching.


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _RecordingConnection:
    """A psycopg-shaped connection that records the statements it is handed.

    The adapter is sync inside `asyncio.to_thread` (D13), and its whole database seam is
    `with self._connect() as connection: connection.execute(...)`. Standing in at that seam
    keeps this a unit test while still exercising the real intersection logic - what is
    under test is which collections reach the query, not what Postgres does with them.
    """

    def __init__(
        self, log: list[tuple[str, tuple[Any, ...]]], rows: list[tuple[Any, ...]]
    ) -> None:
        self._log = log
        self._rows = rows

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        """Returns None, never True: a context manager that swallowed an exception here
        would turn a broken query into an empty result, which is the one failure this
        section cannot tell apart from success."""
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
        self._log.append((sql, params))
        return _Cursor(self._rows)


def _repository(
    rows: list[tuple[Any, ...]] | None = None,
) -> tuple[repository_module.PgKnowledgeBase, list[tuple[str, tuple[Any, ...]]]]:
    log: list[tuple[str, tuple[Any, ...]]] = []
    served = [] if rows is None else rows
    return repository_module.PgKnowledgeBase(lambda: _RecordingConnection(log, served)), log


def _profile(*collections: str) -> AgentProfile:
    """A profile granting exactly `collections`, built through the real loader.

    Going through `from_mapping` rather than constructing a `KnowledgePolicy` by hand is the
    point of the anchor: "absent from a profile" is a statement about the YAML an operator
    writes, and the loader is what turns that into a permission.
    """
    return AgentProfile.from_mapping(
        {
            "id": "delivery",
            "persona": "p",
            "model": "m",
            "knowledge": {"enabled": True, "collections": list(collections)},
        }
    )


def _narrowed(profile: AgentProfile) -> knowledge_module.TenantKnowledgePolicy:
    return knowledge_module.TenantKnowledgePolicy.for_caller(
        _caller("tenant-a"), profile.knowledge
    )


# (doc_id, collection, title, body, version, score) - the shape _SEARCH_SQL selects.
A_MATCHING_ROW: tuple[Any, ...] = ("d-1", "pricing", "Prices", "A large is 12.", 1, 1.0)


@pytest.mark.phase("F8")
def test_a_granted_collection_does_reach_the_database_and_returns_its_hits() -> None:
    """The positive control, and it comes first on purpose.

    Every assertion below is that retrieval returned nothing. An adapter that returns
    nothing unconditionally satisfies all of them, so the case that must NOT be empty is
    what makes the rest mean anything.
    """
    repository, log = _repository([A_MATCHING_ROW])
    policy = _narrowed(_profile("pricing"))

    hits = asyncio.run(repository.search(policy, "price", collections=(_collection("pricing"),)))

    assert [hit.doc_id for hit in hits] == ["d-1"]
    assert len(log) == 1
    assert "pricing" in log[0][1][2]


@pytest.mark.phase("F8")
def test_a_collection_absent_from_the_profile_returns_empty_instead_of_raising() -> None:
    """The anchor. A second agent asking for a corpus its profile does not list gets `()`.

    F8 is done when "a second agent whose profile does not list that collection cannot
    retrieve from it", and this is the shape that has to hold: not an exception, not a
    partial answer, not a permission error. `()` is a complete, ordinary, unremarkable reply.
    """
    repository, log = _repository([A_MATCHING_ROW])
    policy = _narrowed(_profile("delivery"))

    hits = asyncio.run(repository.search(policy, "price", collections=(_collection("pricing"),)))

    assert hits == ()
    assert log == [], (
        "The forbidden collection reached the database. An adapter that queries first and "
        "filters afterwards returns the same rows on a good day and leaks the day somebody "
        "adds an early return above the filter - and a query is also a side effect, so "
        "'does that collection exist' becomes readable from timing alone."
    )


@pytest.mark.phase("F8")
def test_absence_and_emptiness_are_the_same_answer() -> None:
    """Indistinguishable, which is a stronger claim than either one being empty.

    A forbidden collection and a permitted one that simply matched nothing must give the
    same reply. If they ever differ - a different type, a different exception, a different
    empty - the difference answers "does this business have a price list", and that answer
    is the leak this port exists to prevent.
    """
    forbidden_repository, _ = _repository([A_MATCHING_ROW])
    permitted_repository, _ = _repository([])

    forbidden = asyncio.run(
        forbidden_repository.search(
            _narrowed(_profile("delivery")), "price", collections=(_collection("pricing"),)
        )
    )
    permitted = asyncio.run(
        permitted_repository.search(
            _narrowed(_profile("pricing")), "price", collections=(_collection("pricing"),)
        )
    )

    assert type(forbidden) is type(permitted)
    assert forbidden == permitted == ()


@pytest.mark.phase("F8")
def test_a_profile_that_says_nothing_about_knowledge_retrieves_nothing() -> None:
    """The default shape, and the one most profiles will have.

    `KnowledgePolicy.can_read` already pins that an empty grant list means nothing; this
    pins that the ADAPTER agrees rather than reading an empty list as "unfiltered", which is
    how `WHERE collection = ANY('{}')` behaves if the empty case is passed straight through
    to the query instead of short-circuiting before it.
    """
    profile = AgentProfile.from_mapping({"id": "fraud", "persona": "p", "model": "m"})
    repository, log = _repository([A_MATCHING_ROW])
    policy = _narrowed(profile)

    assert asyncio.run(repository.search(policy, "price")) == ()
    assert asyncio.run(repository.search(policy, "price", collections=())) == ()
    assert asyncio.run(repository.get(policy, knowledge_module.DocId("d-1"))) is None
    assert asyncio.run(repository.full_context(policy)) == ""
    assert log == [], (
        "A profile that grants nothing still reached the database. All three reads have to "
        "stop before the statement: `ANY('{}')` matches nothing today, but a grant list "
        "nobody was added to should never become a query at all."
    )


@pytest.mark.phase("F8")
def test_a_mixed_request_is_narrowed_rather_than_refused() -> None:
    """Asking for one permitted and one forbidden collection serves the permitted one.

    Refusing the whole call would be the other way to be "safe", and it would leak the same
    fact: a request that fails only when it names `pricing` has confirmed `pricing`. The
    requested list may narrow what the policy grants; it can never widen it, and it can
    never turn into an error.
    """
    repository, log = _repository([A_MATCHING_ROW])
    policy = _narrowed(_profile("pricing"))

    hits = asyncio.run(
        repository.search(
            policy, "price", collections=(_collection("pricing"), _collection("fraud"))
        )
    )

    assert [hit.doc_id for hit in hits] == ["d-1"]
    assert log[0][1][2] == ["pricing"], (
        f"The forbidden collection was still bound into the query: {log[0][1][2]}. The "
        f"intersection has to happen before the statement, not inside it."
    )


@pytest.mark.phase("F8")
def test_the_other_two_reads_ask_only_for_the_collections_the_profile_granted() -> None:
    """`get` and `full_context` follow the same rule, and they are separately reachable.

    Neither of them takes a collection argument, so their entire narrowing IS the profile's
    grant list, and what this layer decides is which collections reach the statement. A
    `DocId` living in a collection the profile omits is then the ordinary "missing" case -
    `get` answers None because the predicate excluded the row, not because a check was
    remembered after the rows came back. Asserting the bound parameter rather than the
    returned value is deliberate: a stand-in connection cannot enforce a WHERE clause, and
    a test that pretended otherwise would be asserting its own fake.

    `full_context` is the dangerous one of the pair. It needs no query to leak, it hands
    the corpus to the model as a system prompt, and the agent then answers from it
    confidently for the rest of the conversation.
    """
    repository, log = _repository([])
    policy = _narrowed(_profile("delivery"))

    assert asyncio.run(repository.get(policy, knowledge_module.DocId("d-1"))) is None
    assert asyncio.run(repository.full_context(policy)) == ""

    assert [parameters[-1] for _, parameters in log] == [["delivery"], ["delivery"]], (
        f"A read bound collections the profile never granted: {log}. `pricing` must not "
        f"be nameable by an agent whose profile lists only `delivery`, in any of the three "
        f"reads."
    )
