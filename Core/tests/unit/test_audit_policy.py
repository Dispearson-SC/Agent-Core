"""A tools package contributes its own audit argument policy.

Subject: adapters/driven/persistence_pg/audit_repository.py (`collect_audit_policy`,
`PgAuditSink._redact`)

A vertical used to edit the Core file `audit_repository.py` to get its tool arguments into
the trail. Now the package module that holds `build_toolset` may declare `AUDIT_ALLOW`
(stored by value) and `AUDIT_PRESENCE_ONLY` (stored by presence), and the composition root
collects them from the registry it already hand-writes (no auto-discovery).
"""

from __future__ import annotations

import sys
from datetime import date, datetime

import pytest

from agent_core.adapters.driven.persistence_pg import audit_repository
from agent_core.adapters.driven.persistence_pg.audit_repository import (
    PRESENCE_RECORDED,
    PgAuditSink,
    collect_audit_policy,
)


def _builder() -> None:  # stands in for a package's `build_toolset`; only __module__ matters
    return None


def _this_module() -> object:
    return sys.modules[__name__]


def _sink(allow: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]) -> PgAuditSink:
    return PgAuditSink(lambda: None, argument_allowlist=allow, argument_presence_only=presence)  # type: ignore[arg-type,return-value]


def test_a_package_contributes_value_and_presence_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_this_module(), "AUDIT_ALLOW", {"t1": frozenset({"sku"})}, raising=False)
    monkeypatch.setattr(
        _this_module(), "AUDIT_PRESENCE_ONLY", {"t1": frozenset({"note"})}, raising=False
    )

    allow, presence = collect_audit_policy({"pkg": _builder})  # type: ignore[dict-item]

    assert allow["t1"] == frozenset({"sku"})
    assert presence["t1"] == frozenset({"note"})


def test_the_builtin_entries_still_work_alongside_contributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_this_module(), "AUDIT_ALLOW", {"t1": frozenset({"sku"})}, raising=False)

    allow, presence = collect_audit_policy({"pkg": _builder})  # type: ignore[dict-item]

    assert allow["pricing_apply"] == audit_repository.ARGUMENT_ALLOWLIST["pricing_apply"]
    assert presence["case_notes_append"] == frozenset({"note"})


def test_a_package_without_a_policy_contributes_nothing() -> None:
    allow, presence = collect_audit_policy({"pkg": _builder})  # type: ignore[dict-item]

    assert allow == dict(audit_repository.ARGUMENT_ALLOWLIST)
    assert presence == dict(audit_repository.ARGUMENT_PRESENCE_ONLY)


def test_a_package_cannot_redefine_a_tool_another_source_already_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently widening what an existing tool records would be a data-exposure path."""
    monkeypatch.setattr(
        _this_module(), "AUDIT_ALLOW", {"pricing_apply": frozenset({"secret"})}, raising=False
    )

    with pytest.raises(ValueError, match="pricing_apply"):
        collect_audit_policy({"pkg": _builder})  # type: ignore[dict-item]


def test_the_same_decision_repeated_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _this_module(),
        "AUDIT_ALLOW",
        {"pricing_apply": audit_repository.ARGUMENT_ALLOWLIST["pricing_apply"]},
        raising=False,
    )

    collect_audit_policy({"pkg": _builder})  # type: ignore[dict-item]


def test_a_malformed_policy_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_this_module(), "AUDIT_ALLOW", {"t1": ["sku"]}, raising=False)

    with pytest.raises(TypeError, match="AUDIT_ALLOW"):
        collect_audit_policy({"pkg": _builder})  # type: ignore[dict-item]


def test_date_and_datetime_arguments_are_stored_as_iso_strings() -> None:
    sink = _sink({"t": frozenset({"d", "ts", "n"})}, {})

    stored = sink._redact(
        "t", {"d": date(2026, 1, 2), "ts": datetime(2026, 1, 2, 3, 4), "n": 3, "drop": "x"}
    )

    assert stored == {"d": "2026-01-02", "ts": "2026-01-02T03:04:00", "n": 3}


def test_a_tool_decided_with_an_empty_set_is_a_decision_not_a_gap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _sink({"no_args_tool": frozenset()}, {})

    with caplog.at_level("WARNING"):
        stored = sink._redact("no_args_tool", {"x": 1})
        sink._redact("undecided", {"x": 1})

    assert stored == {}
    assert "no_args_tool" not in caplog.text
    assert "undecided" in caplog.text


def test_presence_fields_store_the_fixed_sentence() -> None:
    sink = _sink({}, {"t": frozenset({"note"})})

    assert sink._redact("t", {"note": "secret"}) == {"note": PRESENCE_RECORDED}
