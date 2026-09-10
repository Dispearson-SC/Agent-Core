"""KnowledgeBase port shape - the absence of a write method, asserted mechanically.

Phase:   F8 - Knowledge retrieval
Tasks:   docs/TASKS.md#t-f8-02
Status:  t-f8-02 PINNED (read-only surface, TENANT-NARROWED policy scoping, awaitable reads)

THE TENANT HALF OF THIS FILE, AND WHY IT IS HERE RATHER THAN IN THE ADAPTER'S TESTS
    This port told its adapter to "filter by tenant IN THE QUERY, never post-retrieval",
    and then handed it a `KnowledgePolicy` - `enabled`, `collections`, `mode`, `top_k`,
    `min_score`, `max_context_chars`. No tenant, anywhere in the contract. The instruction
    was unfollowable, and `t-f8-04` was about to write the adapter that could not follow
    it.

    A test in the adapter's suite could only ever assert that ONE query remembered the
    predicate. The assertions below are about the SHAPE: the leading parameter is a policy
    narrowed for exactly one tenant, no member takes a tenant of its own, and a call site
    that has not narrowed does not type-check. That closes the hole for every query anybody
    writes later, including the ones nobody has thought of.

WHY A SHAPE TEST AND NOT A BEHAVIOUR TEST
    Non-negotiable #8 in CLAUDE.md is a structural defence: a prompt injection cannot call
    a method that is not on the object the agent holds. The defence is therefore an
    ABSENCE, and an absence has no behaviour to exercise - there is no call to make and no
    result to assert. The only way to test it is to enumerate the surface and check what is
    NOT on it.

    That is also the only way to make it survive. A docstring saying "no write method" is
    read by a human once; `dir()` is read by the suite on every run. The day somebody adds
    `KnowledgeBase.upsert` for what looks like a good local reason, this file fails.

WHY dir() AND NOT A HAND-KEPT LIST
    A hand-kept list of forbidden names only catches the names somebody thought of. The
    surface is taken from `dir()` and compared against the three reads that are allowed, so
    a method added tomorrow fails whatever it is called - `upsert`, `apply_correction`, or
    something nobody would recognise as a write at all.

    The verb screen below is the second line, not the first: it exists so that RENAMING a
    write into something read-shaped still fails, with a message that names the reason.

WHY THE ADMIN PORT IS READ HERE
    `ports/knowledge_admin.py` is not this anchor's file and is not modified. It is read
    for one assertion: the two ports must share no member. If they ever overlap, an object
    handed out as a `KnowledgeBase` starts to structurally satisfy `KnowledgeAdmin`, and
    the separation that non-negotiable #9 rests on stops being structural.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast, get_args

import pytest

# Imported as MODULES so a missing name fails inside the test that needs it rather than at
# collection time, which would take the whole file down and report nothing.
import agent_core.domain.knowledge as knowledge_module
import agent_core.domain.turn as turn_module
import agent_core.ports.knowledge_admin as admin_module
import agent_core.ports.knowledge_base as port_module

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"

# The whole permitted surface. Three questions, all of them "tell me what is there".
READ_SURFACE = frozenset({"search", "get", "full_context"})

# Second line of defence: a write renamed into something read-shaped. Substring match, so
# `bulk_upsert`, `set_price` and `doc_delete` are all caught. None of the three names in
# READ_SURFACE contains any of these, which is what makes the screen usable at all.
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


def _protocol(module: Any, name: str) -> type:
    member = getattr(module, name)
    assert isinstance(member, type), f"{name} is not a class"
    return member


def _public_surface(protocol: type) -> frozenset[str]:
    """Every name a holder of `protocol` can reach, read off `dir()`.

    `Protocol` and `object` contribute their own members to `dir()`; subtracting them
    leaves exactly what this port declares. Nothing here is hand-maintained, which is the
    point - the assertion has to notice a method nobody told it about.
    """
    inherited = set(dir(Protocol)) | set(dir(object))
    return frozenset(
        name for name in dir(protocol) if not name.startswith("_") and name not in inherited
    )


def _member(name: str) -> Callable[..., Any]:
    member = getattr(_protocol(port_module, "KnowledgeBase"), name)
    assert callable(member), f"KnowledgeBase.{name} is not callable"
    return cast("Callable[..., Any]", member)


def _signature(name: str) -> inspect.Signature:
    # eval_str resolves the port's `from __future__ import annotations` strings back into
    # the real domain types, so the assertions compare types rather than spelling.
    return inspect.signature(_member(name), eval_str=True)


def _parameters(name: str) -> list[inspect.Parameter]:
    return [p for p_name, p in _signature(name).parameters.items() if p_name != "self"]


def _annotation_types(annotation: object) -> set[object]:
    """The annotation and everything nested inside it.

    `tuple[TenantId, ...]` and `TenantId | None` have to count as mentioning the tenant:
    the assertion is about whether a caller can NAME a tenant at all, and a container is
    still naming one.
    """
    found: set[object] = {annotation}
    for argument in get_args(annotation):
        found |= _annotation_types(argument)
    return found


def test_the_surface_is_exactly_the_three_reads() -> None:
    """The lock. Any member added to this port fails here, whatever it is called.

    This is deliberately an equality and not a subset: a test that only checked for the
    absence of known write names would pass the day somebody invents a new one.
    """
    surface = _public_surface(_protocol(port_module, "KnowledgeBase"))

    assert surface == READ_SURFACE, (
        f"KnowledgeBase's surface changed to {sorted(surface)}. Non-negotiable #8: this "
        f"port is read-only and writes live on KnowledgeAdmin, which no agent is injected "
        f"with. If a write is genuinely needed, it goes there - not here."
    )


def test_no_member_name_carries_a_mutating_verb() -> None:
    """Catches a write renamed into something that reads as a read."""
    surface = _public_surface(_protocol(port_module, "KnowledgeBase"))

    offenders = {
        name: verb for name in surface for verb in MUTATING_VERBS if verb in name.lower()
    }

    assert not offenders, (
        f"KnowledgeBase members look like writes: {offenders}. Renaming a write does not "
        f"make it a read - the corpus is persistent, so poisoning it once contaminates "
        f"every future conversation."
    )


def test_the_two_knowledge_ports_share_no_member() -> None:
    """A KnowledgeBase must never structurally satisfy KnowledgeAdmin.

    Both are `Protocol`s, so satisfaction is by shape alone. The moment the two surfaces
    overlap, the type checker stops being able to tell an agent's read-only handle apart
    from an administrator's handle, and non-negotiable #9's "different types, different
    routes" becomes a naming convention.
    """
    base = _public_surface(_protocol(port_module, "KnowledgeBase"))
    admin = _public_surface(_protocol(admin_module, "KnowledgeAdmin"))

    assert base.isdisjoint(admin), (
        f"KnowledgeBase and KnowledgeAdmin now share {sorted(base & admin)}. One port, one "
        f"question: 'what does the business offer' and 'change what the business offers' "
        f"are two."
    )
    assert admin, "KnowledgeAdmin declares nothing; the disjointness above proves nothing."


def test_no_method_can_be_handed_a_document_to_write() -> None:
    """A write needs a payload. None of these methods has anywhere to put one.

    `KnowledgeDoc` is what a write would carry, and `AdminIdentity` is what would authorise
    it. Neither appears in any parameter, so even a method named `search` could not be
    quietly turned into a write without failing here first.
    """
    doc = knowledge_module.KnowledgeDoc
    admin_identity = admin_module.AdminIdentity

    for name in sorted(READ_SURFACE):
        annotations = [p.annotation for p in _parameters(name)]

        assert doc not in annotations, (
            f"KnowledgeBase.{name} accepts a KnowledgeDoc, which is a write payload."
        )
        assert admin_identity not in annotations, (
            f"KnowledgeBase.{name} accepts an AdminIdentity. Administrator identity is "
            f"never routed through the port an agent holds."
        )


def test_every_read_is_scoped_by_a_tenant_narrowed_policy_first() -> None:
    """The permission boundary leads every signature, and it names ONE tenant.

    An unscoped overload is how cross-collection retrieval gets introduced by accident:
    the caller that forgets to pass a policy still type-checks. The same reasoning is why
    the seat is `TenantKnowledgePolicy` and not `KnowledgePolicy` - a policy that has not
    been narrowed is one an adapter cannot write a tenant predicate from, and the
    unnarrowed call would otherwise type-check perfectly.
    """
    narrowed = knowledge_module.TenantKnowledgePolicy

    for name in sorted(READ_SURFACE):
        parameters = _parameters(name)

        assert parameters, f"KnowledgeBase.{name} takes no policy at all."
        assert parameters[0].name == "policy", (
            f"KnowledgeBase.{name}'s first parameter is {parameters[0].name!r}, not 'policy'."
        )
        assert parameters[0].annotation is narrowed, (
            f"KnowledgeBase.{name} is scoped by {parameters[0].annotation!r}. This port "
            f"tells its adapter to filter by tenant in the query; a policy that carries no "
            f"tenant cannot be the source of that predicate."
        )

    assert issubclass(narrowed, knowledge_module.KnowledgePolicy), (
        "The narrowed policy must still BE a KnowledgePolicy: `collections`, `min_score` "
        "and `max_context_chars` are what the rest of the retrieval path reads."
    )


def test_no_read_can_be_pointed_at_a_tenant_of_its_own() -> None:
    """The tenant enters exactly once, inside the policy. There is no second seat for it.

    This is the assertion that makes the narrowing worth having. A `tenant` parameter
    alongside the policy - or a `collections`-style override - would let a caller holding
    tenant A's policy ask for tenant B, and the two arguments disagreeing is something no
    type checker can see. With one source there is nothing to disagree with.
    """
    for name in sorted(READ_SURFACE):
        for parameter in _parameters(name)[1:]:
            assert turn_module.TenantId not in _annotation_types(parameter.annotation), (
                f"KnowledgeBase.{name} takes a second tenant in {parameter.name!r}. The "
                f"tenant is the narrowing on the policy; a query that can name another one "
                f"is the cross-tenant leak this port exists to prevent."
            )
            assert "tenant" not in parameter.name.lower(), (
                f"KnowledgeBase.{name} has a {parameter.name!r} parameter. Whatever it is "
                f"typed as, a second way to say which tenant is a second answer."
            )


def test_every_read_is_awaitable() -> None:
    """D13: ports are async. All three cross an adapter boundary into the database."""
    for name in sorted(READ_SURFACE):
        assert inspect.iscoroutinefunction(_member(name)), (
            f"KnowledgeBase.{name} is sync; ports and adapters are async (D13)."
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("search", tuple[knowledge_module.KnowledgeHit, ...]),
        ("get", knowledge_module.KnowledgeDoc | None),
        ("full_context", str),
    ],
)
def test_the_return_types_are_frozen(name: str, expected: object) -> None:
    """`get` returning `None` is load-bearing: missing and forbidden are indistinguishable.

    Raising instead would confirm that a collection exists, and the confirmation IS the
    leak. Pinning the return type is what stops that being tidied into an exception later.
    """
    assert _signature(name).return_annotation == expected


def test_search_takes_collections_as_a_narrowing_request_only() -> None:
    """The requested collections are intersected with the policy's, never trusted.

    Keyword-only and defaulted, so the honest call is the short one; the policy remains the
    thing that decides, and asking for a collection outside it can only ever narrow.
    """
    parameters = _parameters("search")

    assert [p.name for p in parameters] == ["policy", "query", "collections"]
    assert parameters[1].annotation is str
    assert parameters[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[2].annotation == tuple[knowledge_module.CollectionId, ...]
    assert parameters[2].default == ()


# --------------------------------------------------------------------------------------
# The mypy half: is an unnarrowed query even legal to write?
# --------------------------------------------------------------------------------------
#
# `inspect` sees the shape of the port. It cannot see whether a CALL SITE compiles, and the
# property being defended is exactly that: an adapter or a use case must not be able to
# express a knowledge query that has no tenant in it. Same technique, and same reason, as
# tests/unit/test_ports_knowledge_admin.py.


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


NARROWED_QUERY = """
from __future__ import annotations

from agent_core.domain.knowledge import KnowledgeHit, TenantKnowledgePolicy
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import CallerIdentity
from agent_core.ports.knowledge_base import KnowledgeBase


async def retrieve(
    kb: KnowledgeBase, profile: AgentProfile, caller: CallerIdentity, query: str
) -> tuple[KnowledgeHit, ...]:
    policy = TenantKnowledgePolicy.for_caller(caller, profile.knowledge)
    return await kb.search(policy, query)
"""

UNNARROWED_QUERY = """
from __future__ import annotations

from agent_core.domain.knowledge import KnowledgeHit
from agent_core.domain.profile import AgentProfile
from agent_core.ports.knowledge_base import KnowledgeBase


async def retrieve(
    kb: KnowledgeBase, profile: AgentProfile, query: str
) -> tuple[KnowledgeHit, ...]:
    return await kb.search(profile.knowledge, query)
"""

UNNARROWED_FULL_CONTEXT = """
from __future__ import annotations

from agent_core.domain.profile import AgentProfile
from agent_core.ports.knowledge_base import KnowledgeBase


async def preamble(kb: KnowledgeBase, profile: AgentProfile) -> str:
    return await kb.full_context(profile.knowledge)
"""

TENANT_AS_A_BARE_STRING = """
from __future__ import annotations

from agent_core.domain.knowledge import TenantKnowledgePolicy

policy = TenantKnowledgePolicy(enabled=True, tenant_id="tenant-b")
"""

NARROWING_WITH_NO_TENANT = """
from __future__ import annotations

from agent_core.domain.knowledge import TenantKnowledgePolicy

policy = TenantKnowledgePolicy(enabled=True)
"""


@pytest.mark.phase("F8")
def test_a_narrowed_query_is_ordinary_to_write(tmp_path: Path) -> None:
    """The positive control. A boundary that rejects every query is a broken port.

    It also pins the intended path end to end: a profile supplies the settings, the caller
    supplies the tenant, and the two arrive at the adapter as one value.
    """
    result = _type_check(NARROWED_QUERY, tmp_path)

    assert result.returncode == 0, (
        "The intended retrieval path no longer type-checks. The tenant is supposed to be "
        "impossible to omit, not impossible to supply.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "source"),
    [("search", UNNARROWED_QUERY), ("full_context", UNNARROWED_FULL_CONTEXT)],
    ids=["search", "full_context"],
)
def test_a_query_that_names_no_tenant_does_not_type_check(
    name: str, source: str, tmp_path: Path
) -> None:
    """The core assertion, and the one that was RED when this was written.

    `kb.search(profile.knowledge, query)` is the honest-looking version of the breach: the
    profile's own policy, passed straight through, no cast and no ignore comment. It read
    as correct because the port asked for a `KnowledgePolicy` and that is what a profile
    holds - and the resulting SQL had nothing to put in a tenant predicate.
    """
    result = _type_check(source, tmp_path)

    assert result.returncode != 0, (
        f"KnowledgeBase.{name} accepted a policy that names no tenant. The port instructs "
        f"its adapter to filter by tenant IN THE QUERY; with this call legal, the adapter "
        f"has nothing to filter on and a shared index answers tenant A with tenant B's "
        f"documents.\n{result.stdout}{result.stderr}"
    )
    assert "arg-type" in result.stdout, (
        "Expected the rejection to be about the ARGUMENT type, so it is the missing "
        "narrowing that is refused rather than some incidental error.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F8")
@pytest.mark.parametrize(
    ("name", "source"),
    [("a bare string", TENANT_AS_A_BARE_STRING), ("nothing at all", NARROWING_WITH_NO_TENANT)],
    ids=["bare-string", "absent"],
)
def test_the_tenant_seat_takes_a_tenant_id_and_cannot_be_left_empty(
    name: str, source: str, tmp_path: Path
) -> None:
    """`TenantId` is a `NewType`, and the seat has no default.

    Same idiom as `AdminSubjectId` on the write port: an identifier that must not be
    interchangeable gets a type, and the type checker does the remembering. Every other
    field of a `KnowledgePolicy` has a default, so without this the narrowing would be
    something a caller could skip and still compile.
    """
    result = _type_check(source, tmp_path)

    assert result.returncode != 0, (
        f"A tenant given as {name} was accepted into the narrowing.\n"
        f"{result.stdout}{result.stderr}"
    )
