"""KnowledgeAdmin port shape - and the one-way wall between caller and administrator.

Phase:   F8 - Knowledge retrieval
Tasks:   docs/TASKS.md#t-f8-03
Status:  t-f8-03 PINNED (AdminIdentity is unreachable from CallerIdentity, by type; every
         write is scoped to one tenant by TenantAdminScope)

THE SECOND HOLE, FOUND WHILE CLOSING THE ONE ON THE READ PORT
    `KnowledgeBase` told its adapter to filter by tenant and never gave it a tenant to
    filter on. The write port had the same gap and no warning on it: `AdminIdentity` is
    `subject_id`, `collections`, `is_superuser`, and `KnowledgeDoc` carries no tenant
    either, so `list_docs(admin, CollectionId("pricing"))` named a corpus in every tenant
    at once.

    Collection ids are not tenant-unique - that is settled by the read port needing BOTH a
    collection intersection AND a tenant predicate - so a franchise manager scoped to
    `pricing` was scoped to everybody's `pricing`. On a write path that is not a leak, it
    is an edit to another business's prices.

    The tenant could not join `AdminIdentity` itself: the `**asdict(caller)` defence below
    rests on the two identities sharing exactly one field NAME, and `tenant_id` is one of
    the three the class docstring forbids for precisely that reason. So it wraps the
    identity instead - `TenantAdminScope`, deliberately NOT a subclass, because a subclass
    of `AdminIdentity` would walk straight past the widening scan in this file.

WHAT THIS FILE IS ACTUALLY DEFENDING
    Non-negotiable #9 in CLAUDE.md: "`AdminIdentity` is never derived from
    `CallerIdentity`. Different types, different routes, different policies. No code path
    widens a chat client into an administrator."

    `docs/ARCHITECTURE.md` section 13 makes the stronger claim, and it is the claim this
    file exists to keep honest: "The type checker enforces the boundary before a test
    would." That sentence is only true while the two identities cannot be spelled into one
    another. It was NOT true when this test was written - see below.

THE DEFECT THIS TEST WAS WRITTEN AGAINST
    `AdminIdentity.subject_id` and `CallerIdentity.subject_id` were both plain `str`, so

        def widen(caller: CallerIdentity) -> AdminIdentity:
            return AdminIdentity(subject_id=caller.subject_id)

    type-checked cleanly under `mypy --strict`. Nothing in the system stopped a chat
    client's identity from being retyped as an administrator's in one line, and nothing
    would have failed when it happened. The separation was a naming convention wearing a
    type's clothes.

    The fix is the same idiom the rest of the codebase already uses for identifiers that
    must not be interchangeable (`DocId`, `CollectionId`, `TenantId`): a distinct
    `AdminSubjectId`. A `str` no longer satisfies the seat, so producing one is an
    explicit, greppable act rather than an accident of assignment.

WHY BOTH A MYPY HALF AND A RUNTIME HALF
    They catch different failures and neither subsumes the other.

    `inspect` cannot see whether an expression is LEGAL - it sees names and arities. The
    property being defended is about legality, so half the file runs mypy in a subprocess
    over throwaway modules, the same way `test_ports_agent_runner.py` does.

    But mypy only judges the code it is shown. It cannot answer "does a widening callable
    exist ANYWHERE in this package", because the question is about absence across a tree.
    That half walks the AST of `src/agent_core` and is checked against a known-bad source
    first, so an assertion that scans nothing cannot pass by scanning nothing.

WHY THE NEGATIVE TESTS COME WITH POSITIVE ONES
    A type-check that rejects everything proves nothing at all. Every "this must not
    type-check" below is paired with a legitimate construction that MUST type-check, so a
    port that has simply become unusable fails here rather than looking maximally secure.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import pytest

from agent_core.domain.knowledge import CollectionId, DocId, KnowledgeDoc
from agent_core.domain.turn import CallerIdentity, TenantId
from agent_core.ports.knowledge_admin import (
    AdminIdentity,
    AdminSubjectId,
    KnowledgeAdmin,
    TenantAdminScope,
)

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"
PACKAGE_DIR = SRC_DIR / "agent_core"

# The whole permitted surface of the write port. Frozen here so a fourth method cannot
# arrive without this anchor being reopened.
ADMIN_SURFACE = frozenset({"upsert", "delete", "list_docs"})

# Every return type that means "this callable produced write authority". `TenantAdminScope`
# is on the list because it CONTAINS an `AdminIdentity`: a function handing one back from a
# chat caller is the same widening wearing the newer type's name, and a scan that only knew
# the old name would report the package clean while the path was open.
WRITE_AUTHORITY_RETURNS = frozenset({"AdminIdentity", "TenantAdminScope"})


# --------------------------------------------------------------------------------------
# The runtime half: does a widening callable exist anywhere in the package?
# --------------------------------------------------------------------------------------


def _annotation_names(node: ast.AST | None) -> set[str]:
    """Every bare name mentioned by an annotation expression.

    Taken as names rather than as resolved types on purpose: `AdminIdentity`,
    `AdminIdentity | None` and `tuple[AdminIdentity, ...]` all count, and a module that
    imports the type under an alias still trips the alias check below.
    """
    if node is None:
        return set()
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)} | {
        child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
    }


def _widening_callables(source: str, origin: str) -> list[str]:
    """Every callable in `source` that turns a `CallerIdentity` into an `AdminIdentity`.

    A widening callable is one that can be HANDED a caller and RETURNS an administrator.
    That is the shape non-negotiable #9 forbids, and it is the shape a well-meaning
    refactor produces - `def admin_for(caller)` reads perfectly reasonable at the call
    site, which is exactly why it needs a mechanical objection.
    """
    tree = ast.parse(source)
    offenders: list[str] = []

    for node in ast.walk(tree):
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
            "CallerIdentity" in _annotation_names(argument.annotation)
            for argument in parameters
        )
        returns_admin = bool(WRITE_AUTHORITY_RETURNS & _annotation_names(node.returns))

        if accepts_caller and returns_admin:
            offenders.append(f"{origin}:{node.lineno} {node.name}")

    return offenders


KNOWN_BAD_SOURCE = """
from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import AdminIdentity


def admin_for(caller: CallerIdentity) -> AdminIdentity:
    return AdminIdentity(subject_id=caller.subject_id)
"""

KNOWN_BAD_SCOPE_SOURCE = """
from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import AdminIdentity, TenantAdminScope


def scope_for(caller: CallerIdentity) -> TenantAdminScope:
    return TenantAdminScope(
        admin=AdminIdentity(subject_id=caller.subject_id), tenant_id=caller.tenant_id
    )
"""


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "source"),
    [("AdminIdentity", KNOWN_BAD_SOURCE), ("TenantAdminScope", KNOWN_BAD_SCOPE_SOURCE)],
    ids=["admin-identity", "tenant-admin-scope"],
)
def test_the_widening_scan_detects_a_widening_it_is_shown(name: str, source: str) -> None:
    """The scan is checked against a known-bad module before it is trusted on a clean one.

    Without this, the sweep below would pass just as happily with a broken parser, an
    empty file list, or a typo in the type name - certifying nothing while reading as
    coverage. `docs/WAVES.md` calls that a green-on-empty test and stops the task for it.

    Both spellings are shown to it. The scan matches the return type by NAME, so the day a
    second type carrying write authority appeared, the sweep silently stopped covering the
    newer of the two paths - which is the shape of hole this whole file exists to object
    to.
    """
    offenders = _widening_callables(source, "known-bad")

    assert len(offenders) == 1, (
        f"The widening scan missed the {name} widening it was handed: {offenders}. "
        f"Every clean result it reports below is worthless until this passes."
    )


@pytest.mark.phase("F8")
def test_no_callable_in_the_package_produces_an_admin_identity_from_a_caller_identity() -> None:
    """The sweep. No function anywhere in `agent_core` widens a caller into an admin.

    Package-wide rather than port-local because the forbidden path does not have to live
    in `ports/`. An HTTP dependency, a composition helper or an adapter is where somebody
    would actually write it, and the wall is only as good as its least-watched segment.
    """
    offenders: list[str] = []

    for module_path in sorted(PACKAGE_DIR.rglob("*.py")):
        source = module_path.read_text(encoding="utf-8")
        offenders += _widening_callables(source, str(module_path.relative_to(SRC_DIR)))

    assert not offenders, (
        f"These callables widen a chat client into an administrator: {offenders}. "
        f"Non-negotiable #9: different types, different routes, different policies. An "
        f"administrator's identity is established by the admin route, never derived from "
        f"whoever happened to be talking to the agent."
    )


@pytest.mark.phase("F8")
def test_the_two_identities_are_unrelated_types() -> None:
    """Neither inherits from the other, so no `isinstance` check can confuse them.

    Inheritance would make every widening path invisible: a subclass IS the parent, and a
    seat typed `AdminIdentity` would silently accept a caller forever after.

    There is deliberately no `AdminIdentity is not CallerIdentity` line here: mypy rejects
    that comparison as non-overlapping, which is the property itself being reported by the
    checker rather than by the suite.
    """
    assert not issubclass(AdminIdentity, CallerIdentity)
    assert not issubclass(CallerIdentity, AdminIdentity)

    shared = {f.name for f in dataclasses.fields(AdminIdentity)} & {
        f.name for f in dataclasses.fields(CallerIdentity)
    }
    assert shared == {"subject_id"}, (
        f"The two identities now share {sorted(shared)}. Every field name they have in "
        f"common is a landing site for the `**asdict(caller)` widening below, which is the "
        f"one spelling the type checker cannot object to."
    )


@pytest.mark.phase("F8")
def test_the_tenant_scope_wraps_an_admin_identity_instead_of_extending_one() -> None:
    """`TenantAdminScope` must NOT be an `AdminIdentity` subclass, and this is why.

    The sweep above matches the return annotation by name. `def scope_for(caller) ->
    TenantAdminScope` returns something that IS an `AdminIdentity` under inheritance, so
    the forbidden path would exist while the scan reported the package clean - the wall
    replaced by a wall-shaped hole with the same paint on it.

    Composition also keeps the tenant OFF the identity, which the `AdminIdentity` docstring
    requires: `tenant_id` is one of the three field names it refuses, because each one is a
    landing site for `AdminIdentity(**asdict(caller))`.
    """
    assert not issubclass(TenantAdminScope, AdminIdentity), (
        "TenantAdminScope inherits from AdminIdentity. A subclass carries write authority "
        "under a name the widening scan does not know."
    )

    scope_fields = {f.name: f for f in dataclasses.fields(TenantAdminScope)}
    assert scope_fields.keys() == {"admin", "tenant_id"}
    assert scope_fields["admin"].type in (AdminIdentity, "AdminIdentity")

    admin_fields = {f.name for f in dataclasses.fields(AdminIdentity)}
    assert "tenant_id" not in admin_fields, (
        "AdminIdentity grew a tenant field. Its own docstring forbids exactly that: the "
        "two identities share one field name today, and every one they add is a landing "
        "site for the splat widening."
    )


@pytest.mark.phase("F8")
def test_a_tenant_scope_cannot_be_built_without_naming_a_tenant() -> None:
    """An administrator alone is not a scope. Naming the tenant is the whole act.

    Mirrors `TenantKnowledgePolicy` on the read port: the tenant seat has no default, so a
    write path that never decided which business it was editing does not get built.
    """
    admin = AdminIdentity(subject_id=AdminSubjectId("ops-1"))

    with pytest.raises(TypeError):
        TenantAdminScope(admin)  # type: ignore[call-arg]

    scoped = TenantAdminScope(admin, tenant_id=TenantId("tenant-a"))
    assert scoped.tenant_id == TenantId("tenant-a")
    assert scoped.admin is admin


@pytest.mark.phase("F8")
def test_a_tenant_scope_cannot_be_splatted_out_of_a_caller_identity(
    caller: CallerIdentity,
) -> None:
    """The splat widening, retried against the newer type. It must fail the same way.

    `CallerIdentity` has a `tenant_id`, so the scope's tenant seat IS a landing site - and
    that is fine, because the authority seat is not: nothing in a chat caller can become an
    `AdminIdentity`, so the unpacking has nowhere to put one.
    """
    with pytest.raises(TypeError):
        TenantAdminScope(**dataclasses.asdict(caller))


@pytest.mark.phase("F8")
def test_an_admin_identity_cannot_be_splatted_out_of_a_caller_identity(
    caller: CallerIdentity,
) -> None:
    """`AdminIdentity(**asdict(caller))` must fail, and it must fail at runtime.

    This is the one widening spelling mypy cannot object to: `dataclasses.asdict` returns
    `dict[str, Any]`, and unpacking `Any` is legal by construction. The type checker is
    blind here, so the field names carry the defence - the two dataclasses share exactly
    one field name, and the rest of the caller's shape has nowhere to land.

    Which is also why `channel`, `tenant_id` and `roles` must never be added to
    `AdminIdentity` for convenience: doing so would open this path silently.
    """
    with pytest.raises(TypeError):
        AdminIdentity(**dataclasses.asdict(caller))


# --------------------------------------------------------------------------------------
# The mypy half: is the widening even legal to write?
# --------------------------------------------------------------------------------------


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module against the real port.

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


LEGITIMATE_ADMIN = """
from __future__ import annotations

from agent_core.domain.knowledge import CollectionId
from agent_core.ports.knowledge_admin import AdminIdentity, AdminSubjectId

admin = AdminIdentity(
    subject_id=AdminSubjectId("ops-1"),
    collections=(CollectionId("pricing"),),
)
"""

WIDENING_FUNCTION = """
from __future__ import annotations

from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import AdminIdentity


def admin_for(caller: CallerIdentity) -> AdminIdentity:
    return AdminIdentity(subject_id=caller.subject_id)
"""

DIRECT_RETURN = """
from __future__ import annotations

from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import AdminIdentity


def admin_for(caller: CallerIdentity) -> AdminIdentity:
    return caller
"""

CALLER_SUBJECT_INTO_ADMIN_SEAT = """
from __future__ import annotations

from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import AdminIdentity, KnowledgeAdmin, TenantAdminScope
from agent_core.domain.knowledge import KnowledgeDoc


async def escalate(admin_port: KnowledgeAdmin, caller: CallerIdentity, doc: KnowledgeDoc) -> None:
    scope = TenantAdminScope(AdminIdentity(caller.subject_id), tenant_id=caller.tenant_id)
    await admin_port.upsert(scope, doc)
"""

CALLER_IN_THE_ADMIN_SEAT = """
from __future__ import annotations

from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_admin import KnowledgeAdmin
from agent_core.domain.knowledge import KnowledgeDoc


async def escalate(admin_port: KnowledgeAdmin, caller: CallerIdentity, doc: KnowledgeDoc) -> None:
    await admin_port.upsert(caller, doc)
"""

LEGITIMATE_SCOPED_WRITE = """
from __future__ import annotations

from agent_core.domain.knowledge import CollectionId, KnowledgeDoc
from agent_core.domain.turn import TenantId
from agent_core.ports.knowledge_admin import (
    AdminIdentity,
    AdminSubjectId,
    KnowledgeAdmin,
    TenantAdminScope,
)


async def edit(admin_port: KnowledgeAdmin, doc: KnowledgeDoc) -> KnowledgeDoc:
    scope = TenantAdminScope(
        AdminIdentity(
            subject_id=AdminSubjectId("ops-1"), collections=(CollectionId("pricing"),)
        ),
        tenant_id=TenantId("tenant-a"),
    )
    return await admin_port.upsert(scope, doc)
"""

UNSCOPED_ADMIN_AT_A_CALL_SITE = """
from __future__ import annotations

from agent_core.domain.knowledge import KnowledgeDoc
from agent_core.ports.knowledge_admin import AdminIdentity, KnowledgeAdmin


async def edit(admin_port: KnowledgeAdmin, admin: AdminIdentity, doc: KnowledgeDoc) -> None:
    await admin_port.upsert(admin, doc)
"""

UNSCOPED_LIST = """
from __future__ import annotations

from agent_core.domain.knowledge import CollectionId, KnowledgeDoc
from agent_core.ports.knowledge_admin import AdminIdentity, KnowledgeAdmin


async def audit(
    admin_port: KnowledgeAdmin, admin: AdminIdentity, collection: CollectionId
) -> tuple[KnowledgeDoc, ...]:
    return await admin_port.list_docs(admin, collection)
"""


@pytest.mark.phase("F8")
def test_a_real_administrator_can_still_be_constructed(tmp_path: Path) -> None:
    """The positive control. A wall that rejects everything is a broken port, not a safe one.

    An administrator identity minted through the admin route - explicitly, by naming the
    admin subject type - has to remain ordinary to write.
    """
    result = _type_check(LEGITIMATE_ADMIN, tmp_path)

    assert result.returncode == 0, (
        "A legitimately constructed AdminIdentity no longer type-checks. The boundary is "
        "supposed to make widening impossible, not make the port unusable.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
def test_a_function_widening_a_caller_into_an_admin_does_not_type_check(tmp_path: Path) -> None:
    """The core assertion, and the one that was RED when this file was written.

    `AdminIdentity(subject_id=caller.subject_id)` is the honest-looking version of the
    attack: no cast, no `Any`, no ignore comment, and it used to compile. Reusing the
    caller's own subject id is precisely how a chat client would be widened in practice,
    because it is the only field the two identities have in common.
    """
    result = _type_check(WIDENING_FUNCTION, tmp_path)

    assert result.returncode != 0, (
        "A CallerIdentity's subject_id was accepted into AdminIdentity's subject seat, so "
        "widening a chat client into an administrator is one line and type-checks "
        "cleanly. docs/ARCHITECTURE.md section 13 says the type checker enforces this "
        "boundary before a test would; right now it does not enforce it at all.\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "arg-type" in result.stdout, (
        "Expected the rejection to be about the ARGUMENT type, so it is the identity that "
        "is refused rather than some incidental error.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
def test_passing_a_widened_caller_into_the_admin_port_does_not_type_check(tmp_path: Path) -> None:
    """The same widening at the place it would actually be used: an `upsert` call.

    Checked separately because this is the call site a reviewer sees. A rule that only
    holds at the definition of the type, and dissolves at the call, is not a rule.
    """
    result = _type_check(CALLER_SUBJECT_INTO_ADMIN_SEAT, tmp_path)

    assert result.returncode != 0, (
        "A caller's subject id reached KnowledgeAdmin.upsert's admin seat. This is the "
        "corpus-poisoning path in one call, and the corpus is persistent: contaminate it "
        "once and every future conversation is affected.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
def test_a_scoped_write_is_ordinary_to_perform(tmp_path: Path) -> None:
    """The positive control for the tenant seat. The admin route must still be able to write.

    It also pins the intended spelling: the identity and the tenant are paired ONCE, where
    the administrator's credential was actually verified, and travel together from there.
    """
    result = _type_check(LEGITIMATE_SCOPED_WRITE, tmp_path)

    assert result.returncode == 0, (
        "A properly scoped administrator can no longer write. The tenant is supposed to be "
        "impossible to omit, not impossible to supply.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "source"),
    [("upsert", UNSCOPED_ADMIN_AT_A_CALL_SITE), ("list_docs", UNSCOPED_LIST)],
    ids=["upsert", "list_docs"],
)
def test_a_write_that_names_no_tenant_does_not_type_check(
    name: str, source: str, tmp_path: Path
) -> None:
    """A real administrator, passed straight through, must not reach the write path.

    This is the write-side twin of the retrieval hole, and it is worse: retrieval reads
    another tenant's prices, this one EDITS them. The call is the honest-looking version -
    a genuine `AdminIdentity`, no cast and no ignore comment - and it used to compile,
    because the port asked for an identity and that is what an admin route holds.
    """
    result = _type_check(source, tmp_path)

    assert result.returncode != 0, (
        f"KnowledgeAdmin.{name} accepted an administrator who named no tenant. Collection "
        f"ids are not tenant-unique, so a manager scoped to `pricing` was scoped to every "
        f"tenant's `pricing`.\n{result.stdout}{result.stderr}"
    )
    assert "arg-type" in result.stdout, (
        "Expected the rejection to be about the ARGUMENT type, so it is the missing "
        "narrowing that is refused rather than some incidental error.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "source"),
    [("returned directly", DIRECT_RETURN), ("passed to upsert", CALLER_IN_THE_ADMIN_SEAT)],
)
def test_a_caller_identity_is_never_an_admin_identity(
    name: str, source: str, tmp_path: Path
) -> None:
    """The blunt spellings. These already failed; they are pinned so they keep failing.

    The two dataclasses are structurally different today, but `AdminIdentity` gaining a
    `channel` or a `roles` field for convenience would quietly make a caller assignable
    where an administrator is required.
    """
    result = _type_check(source, tmp_path)

    assert result.returncode != 0, (
        f"A CallerIdentity was accepted as an AdminIdentity ({name}).\n"
        f"{result.stdout}{result.stderr}"
    )


# --------------------------------------------------------------------------------------
# The port surface itself
# --------------------------------------------------------------------------------------


def _public_surface(protocol: type) -> frozenset[str]:
    """Every name a holder of `protocol` can reach, read off `dir()`.

    Nothing hand-maintained: the assertion has to notice a method nobody told it about.
    """
    inherited = set(dir(Protocol)) | set(dir(object))
    return frozenset(
        name for name in dir(protocol) if not name.startswith("_") and name not in inherited
    )


def _signature(name: str) -> inspect.Signature:
    # eval_str resolves the port's `from __future__ import annotations` strings back into
    # the real types, so the assertions compare types rather than spelling.
    return inspect.signature(getattr(KnowledgeAdmin, name), eval_str=True)


def _parameters(name: str) -> list[inspect.Parameter]:
    return [p for p_name, p in _signature(name).parameters.items() if p_name != "self"]


@pytest.mark.phase("F8")
def test_the_admin_surface_is_exactly_the_three_writes() -> None:
    """Frozen as an equality: a fourth method reopens this anchor rather than slipping in."""
    surface = _public_surface(KnowledgeAdmin)

    assert surface == ADMIN_SURFACE, (
        f"KnowledgeAdmin's surface changed to {sorted(surface)}. This port is never "
        f"injected into anything an agent touches, so every method added here widens the "
        f"blast radius of an administrator credential."
    )


@pytest.mark.phase("F8")
def test_every_admin_method_is_authorised_by_a_tenant_scoped_admin_first() -> None:
    """`TenantAdminScope` leads every signature, and no method accepts a `CallerIdentity`.

    Leading rather than merely present: an authorisation argument that can be defaulted or
    passed last is one a caller forgets, and the forgetful caller still type-checks.

    The seat is the SCOPE and not the bare identity because an administrator scoped to
    `pricing` is not scoped to anybody in particular otherwise - collection ids are not
    tenant-unique, and a write aimed at the wrong business is not recoverable by reading
    more carefully afterwards.
    """
    for name in sorted(ADMIN_SURFACE):
        parameters = _parameters(name)

        assert parameters, f"KnowledgeAdmin.{name} takes no identity at all."
        assert parameters[0].name == "scope", (
            f"KnowledgeAdmin.{name}'s first parameter is {parameters[0].name!r}, not 'scope'."
        )
        assert parameters[0].annotation is TenantAdminScope, (
            f"KnowledgeAdmin.{name} is authorised by {parameters[0].annotation!r}, which "
            f"names no tenant. Every write needs to know which business it is editing."
        )

        annotations = [p.annotation for p in parameters]
        assert CallerIdentity not in annotations, (
            f"KnowledgeAdmin.{name} accepts a CallerIdentity. A chat client's identity "
            f"never reaches a write path, in any seat."
        )
        assert AdminIdentity not in annotations, (
            f"KnowledgeAdmin.{name} takes a bare AdminIdentity alongside its scope. Two "
            f"authority arguments can disagree about who is writing."
        )
        assert TenantId not in annotations[1:], (
            f"KnowledgeAdmin.{name} takes a second tenant. The tenant is the narrowing on "
            f"the scope; a write that can name another one is the breach itself."
        )


@pytest.mark.phase("F8")
def test_every_admin_method_is_awaitable() -> None:
    """D13: ports are async. All three cross an adapter boundary into the database."""
    for name in sorted(ADMIN_SURFACE):
        assert inspect.iscoroutinefunction(getattr(KnowledgeAdmin, name)), (
            f"KnowledgeAdmin.{name} is sync; ports and adapters are async (D13)."
        )


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("upsert", KnowledgeDoc),
        ("delete", None),
        ("list_docs", tuple[KnowledgeDoc, ...]),
    ],
)
def test_the_admin_return_types_are_frozen(name: str, expected: object) -> None:
    """`upsert` returns the stored document because the version number is the receipt.

    Returning `None` would leave the caller unable to say WHICH version it just created,
    and the version is the whole answer to "why did the agent quote the old price".
    """
    assert _signature(name).return_annotation == expected


@pytest.mark.phase("F8")
def test_upsert_carries_the_document_and_a_scheduled_effective_date() -> None:
    """`effective_from` is keyword-only and optional: a scheduled change, not a scheduler.

    Retrieval already filters on `effective_from`, so a future date is the whole feature.
    Keeping it keyword-only stops it being confused with `updated_at` at a call site.
    """
    parameters = _parameters("upsert")

    assert [p.name for p in parameters] == ["scope", "doc", "effective_from"]
    assert parameters[1].annotation is KnowledgeDoc
    assert parameters[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[2].annotation == datetime | None
    assert parameters[2].default is None


@pytest.mark.phase("F8")
def test_delete_and_list_docs_are_addressed_by_domain_identifiers() -> None:
    """`DocId` and `CollectionId` are NewTypes, so a bare string cannot be swapped in.

    The same reasoning as `AdminSubjectId`: identifiers that must not be interchangeable
    are given types, and the type checker does the remembering.
    """
    delete_parameters = _parameters("delete")
    assert [p.name for p in delete_parameters] == ["scope", "doc_id"]
    assert delete_parameters[1].annotation is DocId

    list_parameters = _parameters("list_docs")
    assert [p.name for p in list_parameters] == ["scope", "collection", "include_superseded"]
    assert list_parameters[1].annotation is CollectionId
    assert list_parameters[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert list_parameters[2].annotation is bool
    assert list_parameters[2].default is False


@pytest.mark.phase("F8")
def test_the_admin_identity_grant_list_denies_by_default() -> None:
    """An administrator added with no collections edits nothing, the same reading as
    `KnowledgePolicy.can_read`. Permissive defaults are unacceptable for a write path."""
    fields: dict[str, Any] = {f.name: f for f in dataclasses.fields(AdminIdentity)}

    assert fields["collections"].default == ()
    assert fields["is_superuser"].default is False
