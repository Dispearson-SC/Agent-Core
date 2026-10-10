"""Every glazed tool has a deliberate audit argument decision (no silent blank trail)."""

from __future__ import annotations

import inspect
import logging

import pytest

from agent_core.adapters.driven.persistence_pg import audit_repository as ar
from agent_core.adapters.driven.tools.glazed import tools as gt


def _glazed_tool_names() -> set[str]:
    return {name for builder in gt.TOOLSETS.values() for name in builder().tools}


def _model_params(name: str) -> set[str]:
    return set(inspect.signature(getattr(gt, name)).parameters) - {"ctx"}


def test_every_glazed_tool_has_an_audit_decision() -> None:
    decided = set(ar.ARGUMENT_ALLOWLIST) | set(ar.ARGUMENT_PRESENCE_ONLY)
    missing = sorted(_glazed_tool_names() - decided)
    assert not missing, f"glazed tools without an audit argument decision: {missing}"


def test_decisions_name_only_real_parameters_and_cover_all_of_them() -> None:
    for name in _glazed_tool_names():
        decided = set(ar.ARGUMENT_ALLOWLIST.get(name, ())) | set(
            ar.ARGUMENT_PRESENCE_ONLY.get(name, ())
        )
        assert decided == _model_params(name), name


def test_free_text_is_presence_only() -> None:
    assert "rationale" in ar.ARGUMENT_PRESENCE_ONLY["propose_action"]
    assert "note" in ar.ARGUMENT_PRESENCE_ONLY["record_event"]
    assert "rationale" not in ar.ARGUMENT_ALLOWLIST["propose_action"]


def test_a_tool_without_arguments_is_a_decision_not_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = ar.PgAuditSink(lambda: None)  # type: ignore[arg-type, return-value]
    with caplog.at_level(logging.WARNING):
        assert sink._redact("get_order_plan", {}) == {}
    assert "No audit argument decision" not in caplog.text


def test_new_tools_decisions_record_ids_by_value_and_free_text_by_presence() -> None:
    assert ar.ARGUMENT_ALLOWLIST["check_compliance"] == {"decision_id"}
    assert ar.ARGUMENT_ALLOWLIST["classify_text"] == {"kind"}
    assert ar.ARGUMENT_PRESENCE_ONLY["classify_text"] == {"text"}
    assert ar.ARGUMENT_ALLOWLIST["audit_promo"] == {"department", "discount_pct"}
    assert ar.ARGUMENT_ALLOWLIST["get_integrity_issues"] == frozenset()


def test_snapshot_and_briefing_have_an_argument_free_audit_decision() -> None:
    assert ar.ARGUMENT_ALLOWLIST["get_snapshot"] == frozenset()
    assert ar.ARGUMENT_ALLOWLIST["get_briefing"] == frozenset()


def test_redacted_date_arguments_are_json_serializable() -> None:
    """A typed `date` argument must not crash the audit INSERT (json.dumps)."""
    import json
    from datetime import date

    sink = ar.PgAuditSink(lambda: None)  # type: ignore[arg-type, return-value]
    redacted = sink._redact("forecast_demand", {"sku": "SKU-1", "target_date": date(2026, 7, 25)})
    assert json.loads(json.dumps(redacted)) == {"sku": "SKU-1", "target_date": "2026-07-25"}


def test_note_and_transfer_tools_audit_free_text_by_presence_and_the_rest_by_value() -> None:
    assert ar.ARGUMENT_PRESENCE_ONLY["save_manager_note"] == {"text"}
    assert "text" not in ar.ARGUMENT_ALLOWLIST["save_manager_note"]
    assert ar.ARGUMENT_ALLOWLIST["save_manager_note"] == {
        "category",
        "date_from",
        "date_to",
        "impact",
    }
    assert ar.ARGUMENT_ALLOWLIST["get_transfer_options"] == {"sku", "limit"}
    assert ar.ARGUMENT_ALLOWLIST["propose_transfer"] == {"option_id"}
    assert ar.ARGUMENT_PRESENCE_ONLY["propose_transfer"] == {"rationale"}
    assert "offer_surplus" not in ar.ARGUMENT_ALLOWLIST
