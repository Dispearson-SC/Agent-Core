"""The SkillRegistry port contract.

Tasks: docs/TASKS.md#t-f6-01

The property under test is the whole reason the port is split in two: `index` answers
WHAT EXISTS in metadata only, and a body is fetched deliberately, one at a time, by name.
Progressive disclosure is not a hint to the adapter - it is the shape of the contract.

If any member of this port could hand back a body without being given a name, the
optimisation is gone: the index is built on every turn, so a body-carrying index puts
every skill in every request. Nothing fails, no test goes red, and the only symptom is a
larger bill - which is exactly the class of defect `CLAUDE.md`'s silent-bug table is
about. So the lock is structural and lives here, on the port, rather than in an adapter.

The port is checked structurally, never through an adapter: `ports/` owns the contract,
and a test that has to import a concrete adapter to verify it is testing the adapter.
The body-lock is applied to a deliberate counterexample too, because a rule that has
never rejected anything is not known to reject anything.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Callable
from typing import Protocol, get_args, get_type_hints

# Imported as a module so that a name that is not frozen yet fails inside the test that
# needs it, rather than failing the whole file at COLLECTION. The submodule is imported
# explicitly rather than pulled off the package: `ports/__init__.py` re-exports nothing.
import agent_core.ports.skill_registry as skill_registry_port
from agent_core.domain.profile import AgentProfile

PROFILE = AgentProfile(
    id="delivery_optimizer",
    persona="does not matter here",
    model="claude-sonnet-5",
    skill_namespaces=("delivery",),
)

# A marker that only ever appears in a BODY. Any assertion that finds it on the index side
# has found the optimisation being undone.
BODY_MARKER = "ZONE_BODY_MARKER"

_CORPUS: dict[str, tuple[str, str]] = {
    "delivery-zone-rules": (
        "Zone surcharges and cutoff times for delivery pricing.",
        f"# Delivery zone rules\n\n{BODY_MARKER}: surcharge is 4.50 after 18:00.",
    ),
    "refund-windows": (
        "How long a customer has to ask for a refund, per channel.",
        f"# Refund windows\n\n{BODY_MARKER}: 14 days on web, 7 in store.",
    ),
}


class InMemoryRegistry:
    """A recording fake: it reports which bodies were read, so the test asserts on what
    happened rather than on what a mock was told to expect."""

    def __init__(self) -> None:
        self.bodies_read: list[str] = []

    async def index(self, profile: AgentProfile) -> tuple[skill_registry_port.SkillMeta, ...]:
        return tuple(
            skill_registry_port.SkillMeta(
                name=name, description=description, path=f"skills/{name}/SKILL.md"
            )
            for name, (description, _body) in sorted(_CORPUS.items())
        )

    async def read(self, name: str) -> str:
        self.bodies_read.append(name)
        return _CORPUS[name][1]


class _BulkLoadingRegistry(Protocol):
    """The counterexample the body-lock exists to reject: a third member that hands back
    every body at once. It satisfies the two real members, so `isinstance` would accept
    it - structural conformance only checks that required members EXIST. The lock has to
    read the port's own declared surface instead."""

    async def index(self, profile: AgentProfile) -> tuple[skill_registry_port.SkillMeta, ...]: ...

    async def read(self, name: str) -> str: ...

    async def read_all(self, profile: AgentProfile) -> tuple[str, ...]: ...


def _mentions_str(annotation: object) -> bool:
    """Does this annotation carry a skill body anywhere inside it?

    A body is `str`. Metadata is `SkillMeta`, whose own fields are strings but which is
    not itself one - so `tuple[SkillMeta, ...]` is clean and `tuple[str, ...]` is not.
    """
    if annotation is str:
        return True
    return any(_mentions_str(arg) for arg in get_args(annotation))


def _declared_methods(protocol: type) -> dict[str, Callable[..., object]]:
    return {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def _body_returning_methods(protocol: type) -> dict[str, Callable[..., object]]:
    return {
        name: member
        for name, member in _declared_methods(protocol).items()
        if _mentions_str(get_type_hints(member).get("return"))
    }


def _is_runtime_checkable(protocol: type) -> bool:
    return getattr(protocol, "_is_runtime_protocol", False) is True


def test_index_returns_metadata_only() -> None:
    """The index is rebuilt for every turn, so what it carries is what the prompt pays for."""
    port = skill_registry_port.SkillRegistry

    return_type = get_type_hints(port.index)["return"]
    assert not _mentions_str(return_type), (
        "index must not return a skill body - it is rebuilt on every turn"
    )
    assert get_args(return_type)[0] is skill_registry_port.SkillMeta

    fields = tuple(field.name for field in dataclasses.fields(skill_registry_port.SkillMeta))
    assert fields == ("name", "description", "path"), (
        "SkillMeta is the prompt payload; a body-shaped field here defeats the whole design"
    )
    for field in dataclasses.fields(skill_registry_port.SkillMeta):
        assert field.name != "body"


def test_index_is_answerable_without_reading_a_single_body() -> None:
    registry = InMemoryRegistry()

    assert _is_runtime_checkable(port := skill_registry_port.SkillRegistry), (
        "SkillRegistry must be a runtime_checkable Protocol"
    )
    assert isinstance(registry, port)

    metas = asyncio.run(registry.index(PROFILE))

    assert [meta.name for meta in metas] == ["delivery-zone-rules", "refund-windows"]
    assert registry.bodies_read == [], "building the index must not open a body"
    for meta in metas:
        assert BODY_MARKER not in (meta.name + meta.description + meta.path)


def test_no_method_returns_a_body_without_being_given_a_name() -> None:
    """The lock. One body, asked for by name, or the port is not this port any more."""
    port = skill_registry_port.SkillRegistry

    body_methods = _body_returning_methods(port)
    assert set(body_methods) == {"read"}, (
        "exactly one member may return a body; found " + repr(sorted(body_methods))
    )

    hints = get_type_hints(body_methods["read"])
    assert hints["return"] is str, "a body member returns ONE body, never a collection"
    assert hints["name"] is str

    signature = inspect.signature(body_methods["read"])
    parameters = [name for name in signature.parameters if name != "self"]
    assert parameters == ["name"], (
        "a body is reached by skill name only - anything else is a bulk load by another name"
    )


def test_the_lock_rejects_a_registry_that_bulk_loads_bodies() -> None:
    """A rule that has never rejected anything is not known to reject anything."""
    flagged = _body_returning_methods(_BulkLoadingRegistry)

    assert set(flagged) == {"read", "read_all"}
    assert get_type_hints(flagged["read_all"])["return"] is not str
    assert "name" not in inspect.signature(flagged["read_all"]).parameters


def test_both_port_methods_are_awaitable() -> None:
    """D13: both are filesystem I/O - a scan on one side, a file read on the other."""
    port = skill_registry_port.SkillRegistry

    assert inspect.iscoroutinefunction(port.index)
    assert inspect.iscoroutinefunction(port.read)
