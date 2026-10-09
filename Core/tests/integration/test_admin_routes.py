"""The admin HTTP surface, and the Postgres `KnowledgeAdmin` behind it.

Phase:   F8 - Knowledge: Skills and FULL_TEXT
Tasks:   docs/TASKS.md#t-f8-06
Covers:  adapters/driving/http/admin_routes.py
         adapters/driven/knowledge_pg/admin.py

WHAT IS BEING DEFENDED

    1. A WRITE LANDS, AND THE VERY NEXT READ SEES IT. An admin surface that accepts a
       price change and stores it somewhere retrieval does not look is worse than no
       admin surface: the operator believes the price changed and the agent goes on
       quoting the old one. So the write goes in through the HTTP route and the assertion
       is made through `PgKnowledgeBase` - the same object the agent holds - rather than
       through the writer's own read-back.

       The pair is asserted the way `test_knowledge_repository.py` asserts its pair: the
       NEW value must be present AND the old one absent. An adapter that stores nothing at
       all passes the second half alone, which is the green-on-empty test `docs/WAVES.md`
       refuses.

    2. NO CHAT-SURFACE IDENTITY REACHES ANY WRITE PATH. Non-negotiable #9. This is
       asserted three ways because each one covers what the others cannot:

         - by type: a `CallerIdentity` in the write seat is rejected by `mypy --strict`,
           checked against a control snippet that must type-check clean, so a broken
           snippet cannot certify the wall by failing for an unrelated reason;
         - by structure: no callable in either of this anchor's modules takes a caller and
           hands back write authority, and the chat identity type is not so much as
           imported by the admin surface;
         - at runtime: the admin routes refuse a request carrying chat headers - including
           `X-Roles: admin`, the spelling a reasonable person would expect to work - and
           the write port is never called at all.

       The runtime half matters even though the type half exists. `mypy` only judges code
       it is shown, and an HTTP request is a `dict[str, str]` of headers that no type
       checker will ever object to.

    3. THE TENANT COMES FROM THE VERIFIED CREDENTIAL, NEVER FROM THE REQUEST BODY. A body
       naming its own tenant is refused, and every statement the adapter emits carries the
       tenant predicate with the scope's tenant bound to it.

The structural and mypy halves need no infrastructure and always run. The rest need a real
Postgres and skip cleanly without one, the same pattern as `test_knowledge_repository.py`.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_core.adapters.driven.knowledge_pg.repository import PgKnowledgeBase
from agent_core.adapters.driven.persistence_pg import knowledge_migration, migrations
from agent_core.domain.knowledge import (
    CollectionId,
    DocId,
    KnowledgeDoc,
    TenantKnowledgePolicy,
)
from agent_core.domain.turn import CallerIdentity, TenantId
from agent_core.ports.knowledge_admin import (
    AdminIdentity,
    AdminSubjectId,
    TenantAdminScope,
)

TESTS_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = TESTS_DIR.parent
SRC_DIR = CORE_DIR / "src"

ADMIN_ROUTES_MODULE = "agent_core.adapters.driving.http.admin_routes"
ADMIN_PG_MODULE = "agent_core.adapters.driven.knowledge_pg.admin"

ADMIN_ROUTES_PATH = SRC_DIR / "agent_core" / "adapters" / "driving" / "http" / "admin_routes.py"
ADMIN_PG_PATH = SRC_DIR / "agent_core" / "adapters" / "driven" / "knowledge_pg" / "admin.py"

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TENANT_A = TenantId("t-admin-routes-a")
_TENANT_B = TenantId("t-admin-routes-b")
_PRICING = CollectionId("pricing")
_PAYROLL = CollectionId("payroll")

# The same doc id in both tenants. Document ids are guessable, and a missing predicate
# must not be able to hide behind an id that happened not to collide.
_SHARED_DOC_ID = DocId("doc-admin-pricing")

_TOKEN_A = "admin-token-tenant-a"
_TOKEN_B = "admin-token-tenant-b"


# --------------------------------------------------------------------------------------
# Loading the two modules this anchor owns
# --------------------------------------------------------------------------------------


def _load(module_name: str) -> Any:
    """Import a module this anchor is meant to have written, or fail with a message.

    Deliberately not a top-level import. A module that does not exist yet turns a
    top-level import into a COLLECTION error, and `docs/WAVES.md` is explicit that a
    collection error is not a red test - it reports that the file is missing, not that the
    behaviour is wrong, and it takes the rest of the file down with it.
    """
    assert importlib.util.find_spec(module_name) is not None, (
        f"`{module_name}` does not exist yet. t-f8-06 owns it; see docs/TASKS.md."
    )
    return importlib.import_module(module_name)


def _scope(
    tenant_id: TenantId, *, collections: tuple[CollectionId, ...] = (_PRICING,)
) -> TenantAdminScope:
    """One administrator, narrowed to one tenant. Built here the only way it can be built:
    from a credential this test is standing in for, never from a chat caller."""
    return TenantAdminScope(
        admin=AdminIdentity(subject_id=AdminSubjectId("ops-1"), collections=collections),
        tenant_id=tenant_id,
    )


def _authenticator() -> Any:
    """The seam the route authenticates through: an opaque token to a tenant-scoped
    administrator. Two tokens, two tenants, and nothing that accepts a chat identity."""

    scopes = {_TOKEN_A: _scope(_TENANT_A), _TOKEN_B: _scope(_TENANT_B)}

    def authenticate(token: str) -> TenantAdminScope | None:
        return scopes.get(token)

    return authenticate


# --------------------------------------------------------------------------------------
# A recording stand-in for the write port, so the refusal tests need no database
# --------------------------------------------------------------------------------------


class _RecordingAdmin:
    """A `KnowledgeAdmin` that records every call it receives.

    The refusal assertions are about a call that must NEVER HAPPEN, and a 401 alone does
    not prove that: a route could authorise late, after the write. So the port records,
    and the tests assert the record is empty.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, TenantAdminScope]] = []

    def _authorise(self, scope: TenantAdminScope, collection: CollectionId) -> None:
        if not scope.admin.is_superuser and collection not in scope.admin.collections:
            raise PermissionError(f"not granted: {collection}")

    async def upsert(
        self,
        scope: TenantAdminScope,
        doc: KnowledgeDoc,
        *,
        effective_from: datetime | None = None,
    ) -> KnowledgeDoc:
        self._authorise(scope, doc.collection)
        self.calls.append(("upsert", scope))
        return doc

    async def delete(self, scope: TenantAdminScope, doc_id: DocId) -> None:
        self.calls.append(("delete", scope))

    async def list_docs(
        self,
        scope: TenantAdminScope,
        collection: CollectionId,
        *,
        include_superseded: bool = False,
    ) -> tuple[KnowledgeDoc, ...]:
        self._authorise(scope, collection)
        self.calls.append(("list_docs", scope))
        return ()


def _client(knowledge_admin: Any) -> TestClient:
    routes = _load(ADMIN_ROUTES_MODULE)
    app = FastAPI()
    app.include_router(
        routes.create_admin_router(
            knowledge_admin=knowledge_admin, authenticate=_authenticator()
        )
    )
    return TestClient(app)


def _doc_body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "doc_id": str(_SHARED_DOC_ID),
        "collection": str(_PRICING),
        "title": "Standard delivery pricing",
        "body": "Standard delivery costs 40 pesos across the whole city.",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# Postgres fixtures - the same shape as test_knowledge_repository.py
# --------------------------------------------------------------------------------------


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


@pytest.fixture(scope="module")
def app_conninfo() -> Iterator[str]:
    if not _postgres_reachable():
        pytest.skip("no reachable Postgres instance")
    app_db = "agent_core_admin_routes_test"
    dbos_db = "agent_core_admin_routes_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(conninfo))
    asyncio.run(knowledge_migration.apply_knowledge_migration(conninfo))
    yield conninfo


@pytest.fixture
def clean(app_conninfo: str) -> Iterator[str]:
    with psycopg.connect(app_conninfo) as conn:
        conn.execute(
            "DELETE FROM knowledge_docs WHERE tenant_id IN (%s, %s)", (_TENANT_A, _TENANT_B)
        )
    yield app_conninfo


def _read_policy(tenant_id: TenantId) -> TenantKnowledgePolicy:
    return TenantKnowledgePolicy(
        enabled=True, collections=(_PRICING,), min_score=0.0, tenant_id=tenant_id
    )


class _RecordingConnection:
    """A psycopg connection that keeps every statement and parameter set it is handed.

    The tenant assertion has to be made about the SQL, not about the rows: a write
    narrowed by tenant and one narrowed afterwards in Python touch the same row on a good
    day, and diverge only in production, as one business editing another's prices.
    """

    def __init__(self, conninfo: str, log: list[tuple[str, Any]]) -> None:
        self._conninfo = conninfo
        self._log = log
        self._connection: psycopg.Connection[Any] | None = None

    def __enter__(self) -> _RecordingConnection:
        connection = psycopg.connect(self._conninfo)
        connection.__enter__()
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.__exit__(exc_type, exc, traceback)

    def execute(self, sql: str, params: Any = None) -> Any:
        self._log.append((sql, params))
        assert self._connection is not None
        return self._connection.execute(sql, params)


def _recording_factory(conninfo: str, log: list[tuple[str, Any]]) -> Any:
    def connect() -> _RecordingConnection:
        return _RecordingConnection(conninfo, log)

    return connect


# --------------------------------------------------------------------------------------
# 1. The write lands, and the very next read returns the new value
# --------------------------------------------------------------------------------------


@pytest.mark.phase("F8")
def test_an_admin_write_lands_and_the_very_next_read_returns_the_new_value(clean: str) -> None:
    """Through the HTTP route in, through `KnowledgeBase` out.

    Asserted through the READ adapter rather than the writer's own read-back, because the
    failure this guards against is a write that lands somewhere retrieval does not look:
    the operator believes the price changed, and the agent goes on quoting the old one
    with no error anywhere.
    """
    pg_admin = _load(ADMIN_PG_MODULE)
    client = _client(pg_admin.PgKnowledgeAdmin(lambda: psycopg.connect(clean)))
    base = PgKnowledgeBase(lambda: psycopg.connect(clean))
    policy = _read_policy(_TENANT_A)

    created = client.post(
        "/admin/knowledge/documents",
        json=_doc_body(),
        headers={"X-Admin-Token": _TOKEN_A},
    )
    assert created.status_code == 200, created.text

    first = asyncio.run(base.get(policy, _SHARED_DOC_ID))
    assert first is not None, (
        "the document written through the admin route is invisible to the read adapter; "
        "the write went somewhere retrieval does not look"
    )
    assert "40 pesos" in first.body
    assert first.version == 1

    updated = client.post(
        "/admin/knowledge/documents",
        json=_doc_body(body="Standard delivery costs 55 pesos across the whole city."),
        headers={"X-Admin-Token": _TOKEN_A},
    )
    assert updated.status_code == 200, updated.text

    second = asyncio.run(base.get(policy, _SHARED_DOC_ID))
    assert second is not None
    assert "55 pesos" in second.body, (
        "the price change did not reach retrieval - the operator would believe it had"
    )
    assert "40 pesos" not in second.body
    assert second.version == 2, (
        "an update overwrote in place instead of creating version n+1; the record of what "
        f"the agent used to tell customers is gone. Version was {second.version}"
    )

    context = asyncio.run(base.full_context(policy))
    assert "55 pesos" in context and "40 pesos" not in context, (
        "the injected system-prompt context still carries the superseded price"
    )

    history = client.get(
        f"/admin/knowledge/collections/{_PRICING}/documents",
        params={"include_superseded": True},
        headers={"X-Admin-Token": _TOKEN_A},
    )
    assert history.status_code == 200, history.text
    bodies = [doc["body"] for doc in history.json()["documents"]]
    assert any("40 pesos" in body for body in bodies), (
        "the superseded version was not kept. A hard overwrite destroys the answer to "
        "'why did the agent quote the old price on Tuesday?'"
    )


@pytest.mark.phase("F8")
def test_a_soft_delete_withdraws_the_document_from_retrieval_and_keeps_the_row(
    clean: str,
) -> None:
    """Soft delete: retrieval stops seeing it, the audit trail does not."""
    pg_admin = _load(ADMIN_PG_MODULE)
    client = _client(pg_admin.PgKnowledgeAdmin(lambda: psycopg.connect(clean)))
    base = PgKnowledgeBase(lambda: psycopg.connect(clean))
    policy = _read_policy(_TENANT_A)

    client.post(
        "/admin/knowledge/documents", json=_doc_body(), headers={"X-Admin-Token": _TOKEN_A}
    )
    assert asyncio.run(base.get(policy, _SHARED_DOC_ID)) is not None

    removed = client.delete(
        f"/admin/knowledge/documents/{_SHARED_DOC_ID}", headers={"X-Admin-Token": _TOKEN_A}
    )
    assert removed.status_code == 204, removed.text

    assert asyncio.run(base.get(policy, _SHARED_DOC_ID)) is None, (
        "a deleted document is still being retrieved"
    )

    kept = client.get(
        f"/admin/knowledge/collections/{_PRICING}/documents",
        params={"include_superseded": True},
        headers={"X-Admin-Token": _TOKEN_A},
    )
    assert any(doc["doc_id"] == str(_SHARED_DOC_ID) for doc in kept.json()["documents"]), (
        "the row was hard-deleted; the record of what the agent used to say is destroyed"
    )


# --------------------------------------------------------------------------------------
# 2. No chat-surface identity reaches any write path
# --------------------------------------------------------------------------------------


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "headers"),
    [
        ("no-credentials", {}),
        (
            "chat-headers",
            {"X-Subject-Id": "u-1", "X-Tenant-Id": str(_TENANT_A), "X-Channel": "http"},
        ),
        (
            "chat-headers-claiming-the-admin-role",
            {
                "X-Subject-Id": "u-1",
                "X-Tenant-Id": str(_TENANT_A),
                "X-Channel": "http",
                "X-Roles": "admin",
            },
        ),
        ("chat-subject-id-as-a-token", {"X-Admin-Token": "u-1"}),
    ],
    ids=["none", "chat", "chat-admin-role", "subject-as-token"],
)
def test_no_chat_surface_identity_reaches_the_write_path_at_runtime(
    name: str, headers: dict[str, str]
) -> None:
    """The runtime half of non-negotiable #9.

    `X-Roles: admin` is included deliberately: `transcript_routes.py` gates its wider READ
    projection on exactly that role, so it is the spelling a reasonable person carries over
    to this surface. It must not work here. A read projection is safe to widen by role; a
    write to the corpus every future conversation is answered from is not.

    The port records its calls, because a 401 does not by itself prove the write never
    happened - a route could authorise after writing.
    """
    admin = _RecordingAdmin()
    client = _client(admin)

    response = client.post("/admin/knowledge/documents", json=_doc_body(), headers=headers)

    assert response.status_code == 401, (
        f"{name} reached the admin write surface with status {response.status_code}; a chat "
        "client was widened into an administrator"
    )
    assert admin.calls == [], (
        f"{name} was refused, but the write port had already been called: {admin.calls}"
    )


@pytest.mark.phase("F8")
def test_the_tenant_is_taken_from_the_credential_and_never_from_the_request_body() -> None:
    """A body naming its own tenant is refused outright rather than quietly ignored.

    Ignoring it would be almost as bad: the caller believes they addressed tenant B, the
    write lands in tenant A, and nothing says so.
    """
    admin = _RecordingAdmin()
    client = _client(admin)

    response = client.post(
        "/admin/knowledge/documents",
        json=_doc_body(tenant_id=str(_TENANT_B)),
        headers={"X-Admin-Token": _TOKEN_A},
    )

    assert response.status_code == 422, (
        f"a request body naming its own tenant was accepted with {response.status_code}"
    )
    assert admin.calls == []


@pytest.mark.phase("F8")
def test_an_administrator_cannot_write_to_a_collection_they_were_not_granted() -> None:
    """`collections` scopes which corpora an administrator may edit, and the route must
    surface the refusal rather than turning it into a 500."""
    admin = _RecordingAdmin()
    client = _client(admin)

    response = client.post(
        "/admin/knowledge/documents",
        json=_doc_body(collection=str(_PAYROLL)),
        headers={"X-Admin-Token": _TOKEN_A},
    )

    assert response.status_code == 403, response.text
    assert admin.calls == []


@pytest.mark.phase("F8")
def test_the_admin_surface_does_not_so_much_as_name_the_chat_identity_type() -> None:
    """`CallerIdentity` must not appear in either module this anchor owns.

    Not a style rule. Every widening spelling starts with a caller being in scope, so the
    cheapest possible wall is that the type is never imported onto the write surface at
    all - there is nothing to widen from.
    """
    for path in (ADMIN_ROUTES_PATH, ADMIN_PG_PATH):
        assert path.exists(), f"{path.relative_to(CORE_DIR)} does not exist yet (t-f8-06)"
        source = path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        body = code.split('"""')[-1] if code.count('"""') >= 2 else code
        assert "CallerIdentity" not in body, (
            f"{path.relative_to(CORE_DIR)} names `CallerIdentity` in its code. The admin "
            "surface establishes an administrator from a credential; a chat caller has no "
            "business being in scope on a write path."
        )


def _annotation_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)} | {
        child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
    }


_WRITE_AUTHORITY_RETURNS = frozenset({"AdminIdentity", "TenantAdminScope"})


def _widening_callables(source: str, origin: str) -> list[str]:
    """Every callable that can be handed a caller and returns write authority.

    Same scan as `tests/unit/test_ports_knowledge_admin.py` runs package-wide. It is
    repeated here, over this anchor's two files, because the route is precisely where
    somebody would write `def scope_for(caller)` and it would read perfectly reasonable.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        parameters = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        ]
        accepts_caller = any(
            "CallerIdentity" in _annotation_names(argument.annotation) for argument in parameters
        )
        if accepts_caller and _WRITE_AUTHORITY_RETURNS & _annotation_names(node.returns):
            offenders.append(f"{origin}:{node.lineno} {node.name}")
    return offenders


_KNOWN_BAD = """
from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import AdminIdentity, TenantAdminScope


def scope_for(caller: CallerIdentity) -> TenantAdminScope:
    return TenantAdminScope(
        admin=AdminIdentity(subject_id=caller.subject_id), tenant_id=caller.tenant_id
    )
"""


@pytest.mark.phase("F8")
def test_the_widening_scan_detects_a_widening_it_is_shown() -> None:
    """The scan is checked against a known-bad module before it is trusted on a clean one.

    Without this, the sweep below would report both files clean just as happily with a
    typo in the type name - certifying nothing while reading as coverage.
    """
    assert len(_widening_callables(_KNOWN_BAD, "known-bad")) == 1


@pytest.mark.phase("F8")
def test_neither_module_this_anchor_owns_widens_a_caller_into_an_administrator() -> None:
    offenders: list[str] = []
    for path in (ADMIN_ROUTES_PATH, ADMIN_PG_PATH):
        assert path.exists(), f"{path.relative_to(CORE_DIR)} does not exist yet (t-f8-06)"
        offenders += _widening_callables(
            path.read_text(encoding="utf-8"), str(path.relative_to(SRC_DIR))
        )

    assert not offenders, (
        f"These callables widen a chat client into an administrator: {offenders}. "
        "Non-negotiable #9: different types, different routes, different policies."
    )


@pytest.mark.phase("F8")
def test_a_caller_identity_cannot_be_splatted_into_a_scope_for_this_adapter(
    caller: CallerIdentity,
) -> None:
    """The one widening spelling mypy is structurally blind to, refused at runtime.

    `dataclasses.asdict` returns `dict[str, Any]` and unpacking `Any` is always legal, so
    the field names carry this one: the caller's shape has nowhere to land.
    """
    with pytest.raises(TypeError):
        AdminIdentity(**dataclasses.asdict(caller))
    with pytest.raises(TypeError):
        TenantAdminScope(**dataclasses.asdict(caller))


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module, outside the repository tree.

    Outside on purpose: a fixture that deliberately fails to type-check must never be
    picked up by the project-wide `mypy` run.
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


_LEGITIMATE_WRITE = """
from __future__ import annotations

from agent_core.adapters.driven.knowledge_pg.admin import PgKnowledgeAdmin
from agent_core.domain.knowledge import CollectionId, KnowledgeDoc
from agent_core.domain.turn import TenantId
from agent_core.ports.knowledge_admin import (
    AdminIdentity,
    AdminSubjectId,
    KnowledgeAdmin,
    TenantAdminScope,
)


async def edit(admin: PgKnowledgeAdmin, doc: KnowledgeDoc) -> KnowledgeDoc:
    port: KnowledgeAdmin = admin
    scope = TenantAdminScope(
        admin=AdminIdentity(
            subject_id=AdminSubjectId("ops-1"), collections=(CollectionId("pricing"),)
        ),
        tenant_id=TenantId("t-1"),
    )
    return await port.upsert(scope, doc)
"""

_CALLER_IN_THE_WRITE_SEAT = """
from __future__ import annotations

from agent_core.adapters.driven.knowledge_pg.admin import PgKnowledgeAdmin
from agent_core.domain.knowledge import KnowledgeDoc
from agent_core.domain.turn import CallerIdentity


async def escalate(admin: PgKnowledgeAdmin, caller: CallerIdentity, doc: KnowledgeDoc) -> None:
    await admin.upsert(caller, doc)
"""


@pytest.mark.phase("F8")
def test_the_legitimate_write_type_checks_and_the_adapter_satisfies_the_port(
    tmp_path: Path,
) -> None:
    """The control. Without it, the refusal below could be passing for any reason at all -
    a typo, a missing module, a broken MYPYPATH - and would still read as a wall.

    It also asserts the thing a `Protocol` is for: `PgKnowledgeAdmin` is accepted in a
    `KnowledgeAdmin` seat, structurally, with nothing declaring it.
    """
    result = _type_check(_LEGITIMATE_WRITE, tmp_path)

    assert result.returncode == 0, (
        "the legitimate admin write does not type-check, so the refusal test below proves "
        f"nothing:\n{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
def test_a_caller_identity_in_the_write_seat_does_not_type_check(tmp_path: Path) -> None:
    """The type half of non-negotiable #9, asserted against THIS adapter.

    `mypy --strict` must reject a `CallerIdentity` handed to `upsert`. The seat takes a
    `TenantAdminScope`, the two types are unrelated, and no cast, alias or ignore comment
    is involved.
    """
    result = _type_check(_CALLER_IN_THE_WRITE_SEAT, tmp_path)

    assert result.returncode != 0, (
        "mypy --strict accepted a CallerIdentity in the admin write seat; the wall between "
        f"chat client and administrator is not type-checked:\n{result.stdout}"
    )
    assert "CallerIdentity" in result.stdout, (
        f"mypy failed for some other reason than the widening:\n{result.stdout}{result.stderr}"
    )


# --------------------------------------------------------------------------------------
# 3. Every statement carries the tenant, and it comes from the scope
# --------------------------------------------------------------------------------------


@pytest.mark.phase("F8")
def test_every_sql_constant_the_admin_adapter_exposes_names_the_tenant() -> None:
    """No infrastructure needed, and that is the point: this must never lapse quietly.

    A write path against `knowledge_docs` with no tenant predicate does not fail a test -
    it edits somebody else's prices, and the row it touched looks exactly like a row it
    was allowed to touch.
    """
    module = _load(ADMIN_PG_MODULE)
    constants = [
        value
        for name, value in vars(module).items()
        if isinstance(value, str) and not name.startswith("__") and "knowledge_docs" in value
    ]

    assert constants, (
        "the admin adapter exposes no SQL constant against `knowledge_docs`; the tenant "
        "predicate cannot be asserted against SQL that does not exist"
    )
    for sql in constants:
        assert "tenant_id" in sql, f"a statement against `knowledge_docs` has no tenant:\n{sql}"
        if not re.search(r"\bINSERT\s+INTO\b", sql) or re.search(r"\bWHERE\b", sql):
            assert "tenant_id = %s" in sql, (
                f"a statement carries no bound tenant predicate:\n{sql}\n"
                "Narrowing by tenant after the fact is the multi-tenant breach on a write "
                "path: it does not leak a price, it changes one."
            )


@pytest.mark.phase("F8")
def test_the_tenant_reaches_postgres_bound_from_the_scope_and_not_from_the_payload(
    clean: str,
) -> None:
    """Assert on the statements, not only on the rows.

    Rows cannot tell a tenant-narrowed write from one narrowed afterwards in Python; both
    touch tenant A's row on a good day. So the recorder reads what actually went to
    Postgres, and the behaviour that predicate buys is checked from the other side: an
    identical doc id exists in tenant B and must be untouched.
    """
    pg_admin = _load(ADMIN_PG_MODULE)

    other = pg_admin.PgKnowledgeAdmin(lambda: psycopg.connect(clean))
    asyncio.run(
        other.upsert(
            _scope(_TENANT_B),
            KnowledgeDoc(
                doc_id=_SHARED_DOC_ID,
                collection=_PRICING,
                title="Standard delivery pricing",
                body="Standard delivery costs 900 pesos across the whole city.",
            ),
        )
    )

    log: list[tuple[str, Any]] = []
    admin = pg_admin.PgKnowledgeAdmin(_recording_factory(clean, log))
    client = _client(admin)

    response = client.post(
        "/admin/knowledge/documents",
        json=_doc_body(),
        headers={"X-Admin-Token": _TOKEN_A},
    )
    assert response.status_code == 200, response.text

    statements = [(sql, params) for sql, params in log if "knowledge_docs" in sql]
    assert statements, "the admin adapter reached Postgres without naming `knowledge_docs`"
    for sql, params in statements:
        assert "tenant_id" in sql, f"a statement reached Postgres with no tenant:\n{sql}"
        assert params is not None and _TENANT_A in tuple(params), (
            f"the tenant was never bound to the statement; parameters were {params!r}"
        )

    base = PgKnowledgeBase(lambda: psycopg.connect(clean))
    theirs = asyncio.run(base.get(_read_policy(_TENANT_B), _SHARED_DOC_ID))
    assert theirs is not None and "900 pesos" in theirs.body, (
        "tenant B's document was overwritten by a write scoped to tenant A - the shared "
        "doc id was enough to reach across the tenant boundary"
    )


@pytest.mark.phase("F8")
def test_a_listing_scoped_to_one_tenant_never_shows_another_tenants_documents(
    clean: str,
) -> None:
    """`pricing` exists in every tenant, so a listing is `(tenant, collection)` or it is a
    directory of other people's documents."""
    pg_admin = _load(ADMIN_PG_MODULE)
    admin = pg_admin.PgKnowledgeAdmin(lambda: psycopg.connect(clean))
    client = _client(admin)

    client.post(
        "/admin/knowledge/documents",
        json=_doc_body(body="Tenant B pays 900 pesos."),
        headers={"X-Admin-Token": _TOKEN_B},
    )
    client.post(
        "/admin/knowledge/documents",
        json=_doc_body(doc_id="doc-a-only", body="Tenant A pays 40 pesos."),
        headers={"X-Admin-Token": _TOKEN_A},
    )

    listing = client.get(
        f"/admin/knowledge/collections/{_PRICING}/documents",
        headers={"X-Admin-Token": _TOKEN_A},
    )

    assert listing.status_code == 200, listing.text
    bodies = [doc["body"] for doc in listing.json()["documents"]]
    assert bodies == ["Tenant A pays 40 pesos."], (
        f"tenant A's listing returned documents it does not own: {bodies}"
    )


@pytest.mark.phase("F8")
def test_a_scheduled_change_is_stored_and_is_not_retrieved_before_it_takes_effect(
    clean: str,
) -> None:
    """`effective_from` in the future is a scheduled change and needs no scheduler,
    because retrieval already filters on it. This is the admin side of that promise."""
    pg_admin = _load(ADMIN_PG_MODULE)
    client = _client(pg_admin.PgKnowledgeAdmin(lambda: psycopg.connect(clean)))
    base = PgKnowledgeBase(lambda: psycopg.connect(clean))

    client.post(
        "/admin/knowledge/documents", json=_doc_body(), headers={"X-Admin-Token": _TOKEN_A}
    )
    monday = datetime.now(UTC) + timedelta(days=3)
    scheduled = client.post(
        "/admin/knowledge/documents",
        json=_doc_body(
            doc_id="doc-admin-pricing-monday",
            body="Standard delivery costs 55 pesos across the whole city.",
            effective_from=monday.isoformat(),
        ),
        headers={"X-Admin-Token": _TOKEN_A},
    )
    assert scheduled.status_code == 200, scheduled.text

    context = asyncio.run(base.full_context(_read_policy(_TENANT_A)))
    assert "40 pesos" in context, "the document in force right now is missing"
    assert "55 pesos" not in context, (
        "a scheduled change is being quoted to customers before it takes effect"
    )
