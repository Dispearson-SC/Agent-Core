"""TranscriptReader port shape - who is asking, and which tenant, on every read.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-02

WHY THIS PORT NEEDS A SHAPE TEST AT ALL
    Non-negotiable #11: transcripts store everything and filter on read. That makes the
    READ signature the entire security boundary of F10. Storage keeps the tool arguments,
    the reasoning and the policy denials for every conversation in every tenant; the only
    thing standing between an operator's row and a user's screen is which parameters this
    Protocol forces a caller to supply.

    A behaviour test cannot defend that yet - the projection is `t-f10-03` and does not
    exist. But the seam it will be written against is frozen NOW, and a wrong seat here is
    cheap to type and expensive to discover once four endpoints and a repository are built
    on it. So this module asserts the seam itself.

THE TWO SEATS EVERY READ MUST HAVE
    `Audience`  - the filter. `VISIBLE_TO[audience]` is the single place redaction is
                  expressed (`domain/transcript.py`), and a method with no audience seat
                  cannot consult it. It would have to pick a default, and a default here is
                  either "admin" - a leak - or "user" - an operator tool that hides the
                  rows it exists to show.
    `TenantId`  - the scope. The port's own docstring says tenant scoping belongs IN the
                  query; a method that is never handed a tenant has nothing to put in the
                  predicate. These reads return WHOLE conversations, so a missing tenant is
                  a cross-tenant breach with a friendly UI on top.

    A tenant seat is satisfied by `TenantId` directly OR by a domain type that carries one
    as a field - `SessionRef` exists precisely so a session can never be named without its
    tenant. It is deliberately NOT satisfied by adding a second `tenant: TenantId` beside a
    `SessionRef`: two sources for one fact can disagree, and the caller then decides which
    one scopes the query. CLAUDE.md's "do not duplicate a fact that can drift" is the
    general rule; here the drift is a data breach.

WHY NO CALLER-SUPPLIED FILTER, EVER
    The moment a read takes `where`, `sql`, a predicate callable or a free-form mapping,
    the tenant predicate stops being something this port guarantees and becomes something
    each call site is trusted to remember. Every narrowing option must be an enumerated,
    typed parameter the adapter itself compiles into SQL. `suspended_only: bool` is a
    filter; `filter: str` is a hole.

WHY EVERY MEMBER IS CHECKED, NOT A HAND-KEPT LIST
    `t-f10-06` builds four read endpoints over this port, so the surface may still grow.
    The rules below are applied to whatever `dir()` reports, so a method added tomorrow is
    held to them without anybody remembering to come back here.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Callable
from typing import Any, Protocol, cast

# Imported as a MODULE so a missing name fails inside the test that needs it rather than at
# collection time, which would take the whole file down and report nothing.
import agent_core.ports.transcript_reader as port_module
from agent_core.domain.transcript import Audience
from agent_core.domain.turn import TenantId

# The reads that exist today. This is a floor, not a lock: the rules below run over the
# whole surface, and this set only proves the iteration is not vacuous.
KNOWN_READS = frozenset({"page", "list_conversations"})

# Parameter names that mean "the caller wrote the narrowing". Substring match, so
# `where_clause`, `extra_filter`, `raw_sql` and `order_by_expr` are all caught.
PREDICATE_NAMES = (
    "sql",
    "where",
    "filter",
    "predicate",
    "clause",
    "criteria",
    "condition",
    "expr",
    "raw",
    "order_by",
    "sort_by",
)

# Annotations open enough to smuggle a predicate through. `object` and `Any` accept a
# callable; a bare mapping or sequence of strings is how a `{"tenant_id": ...}` filter dict
# arrives. None of them can be compiled into SQL by the adapter without trusting the caller.
OPEN_ENDED_ANNOTATIONS = ("Callable", "Any", "Mapping", "dict", "list", "Sequence")


def _protocol() -> type:
    member = port_module.TranscriptReader
    assert isinstance(member, type), "TranscriptReader is not a class"
    return member


def _read_surface() -> frozenset[str]:
    """Every name a holder of the port can reach, read off `dir()`.

    `Protocol` and `object` contribute their own members; subtracting them leaves exactly
    what this port declares.
    """
    inherited = set(dir(Protocol)) | set(dir(object))
    return frozenset(
        name
        for name in dir(_protocol())
        if not name.startswith("_") and name not in inherited
    )


def _member(name: str) -> Callable[..., Any]:
    member = getattr(_protocol(), name)
    assert callable(member), f"TranscriptReader.{name} is not callable"
    return cast("Callable[..., Any]", member)


def _signature(name: str) -> inspect.Signature:
    # eval_str resolves the port's `from __future__ import annotations` strings back into
    # the real domain types, so the assertions compare types rather than spelling.
    return inspect.signature(_member(name), eval_str=True)


def _parameters(name: str) -> list[inspect.Parameter]:
    return [p for p_name, p in _signature(name).parameters.items() if p_name != "self"]


def _carries_a_tenant(annotation: object) -> bool:
    """True when this annotation cannot be supplied without also supplying a tenant.

    Either the parameter IS a `TenantId`, or it is a domain dataclass with a `TenantId`
    field - which is what `SessionRef` is for.
    """
    if annotation is TenantId:
        return True
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        hints = typing.get_type_hints(annotation)
        return any(hint is TenantId for hint in hints.values())
    return False


def test_the_surface_contains_the_reads_the_rules_below_run_over() -> None:
    """Guards against a vacuous pass: rules over an empty surface assert nothing."""
    surface = _read_surface()

    assert KNOWN_READS <= surface, (
        f"TranscriptReader is missing {sorted(KNOWN_READS - surface)}. The paged "
        f"conversation and the inbox list are what F10 is done-when."
    )


def test_every_read_takes_an_audience() -> None:
    """`VISIBLE_TO[audience]` is the filter; a read with no audience seat cannot use it.

    There is no defensible default. Defaulting to ADMIN leaks tool arguments and reasoning
    into a user-facing view; defaulting to USER produces an operator tool that hides the
    rows it exists to show, and somebody then adds an ad-hoc filter at the call site -
    which is the exact thing `domain/transcript.py` says must never happen.
    """
    for name in sorted(_read_surface()):
        annotations = [p.annotation for p in _parameters(name)]

        assert Audience in annotations, (
            f"TranscriptReader.{name} takes no Audience. Non-negotiable #11: storage keeps "
            f"everything and the audience decides what comes back, so a read with no "
            f"audience seat has to guess - and both guesses are wrong."
        )


def test_every_read_is_scoped_by_a_tenant() -> None:
    """A read that is never handed a tenant has nothing to put in the WHERE clause.

    Satisfied by a `TenantId` or by a domain type carrying one (`SessionRef`). A second
    `tenant` seat beside a `SessionRef` would NOT be an improvement: two sources for one
    fact can disagree, and the caller would decide which one scopes the query.
    """
    for name in sorted(_read_surface()):
        parameters = _parameters(name)

        assert any(_carries_a_tenant(p.annotation) for p in parameters), (
            f"TranscriptReader.{name} takes no TenantId and no tenant-bearing type. These "
            f"reads return whole conversations; an unscoped one is a cross-tenant breach."
        )


def test_no_read_accepts_a_caller_supplied_filter() -> None:
    """Narrowing is enumerated and typed, or it is a hole in the tenant predicate.

    `suspended_only: bool` and `profile_id: str | None` are filters the adapter compiles
    itself. `filter: str` is a fragment the caller wrote, and the tenant predicate then
    depends on every call site remembering to keep it.

    `cursor` is exempt by construction, not by exception: it is an opaque continuation
    token the adapter minted, never a narrowing the caller composed.
    """
    for name in sorted(_read_surface()):
        for parameter in _parameters(name):
            lowered = parameter.name.lower()

            offending = [word for word in PREDICATE_NAMES if word in lowered]
            assert not offending, (
                f"TranscriptReader.{name} takes {parameter.name!r}, which reads as a "
                f"caller-supplied narrowing ({', '.join(offending)}). Every filter on this "
                f"port is an enumerated typed parameter the adapter turns into SQL itself."
            )

            spelled = str(parameter.annotation)
            open_ended = [word for word in OPEN_ENDED_ANNOTATIONS if word in spelled]
            assert not open_ended, (
                f"TranscriptReader.{name}'s {parameter.name!r} is annotated {spelled!r}, "
                f"which is open enough to carry a predicate or a filter mapping. A read on "
                f"this port takes closed, typed options only."
            )


def test_every_read_is_awaitable() -> None:
    """D13: ports are async. Every one of these crosses into the database."""
    for name in sorted(_read_surface()):
        assert inspect.iscoroutinefunction(_member(name)), (
            f"TranscriptReader.{name} is sync; ports and adapters are async (D13)."
        )
