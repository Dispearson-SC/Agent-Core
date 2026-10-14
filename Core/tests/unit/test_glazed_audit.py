"""Every glazed tool has a deliberate audit argument decision (no silent blank trail).

Subject: the `audit` block of addons/glazed_tools/addon.yaml, joined with Core's own decisions by
`collect_audit_for` exactly as the root does. A port of `test_glazed_audit.py` of vertical/glazed
(D88): the same eight behaviors and assertions; the tool list and the parameter names now come from
the generated tools (the old test read the signatures of the hand-written functions).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import (
    AddonContributions,
    collect_audit_for,
    register_addons,
)
from agent_core.adapters.driven.persistence_pg import audit_repository as ar
from agent_core.adapters.driven.tools.http_bridge import clear_openapi_cache
from agent_core.composition import TOOL_PACKAGES

HERE = Path(__file__).resolve()
SNAPSHOT = HERE.parents[1] / "fixtures" / "glazed_tools" / "internal_openapi.json"
ENVIRONMENT = {
    "AGENT_CORE_ADDONS": "http_tool_bridge,glazed_tools",
    "AGENT_CORE_ADDONS_DIR": str(HERE.parents[3] / "addons"),
    "AGENT_CORE_ADDON__GLAZED_TOOLS__OPENAPI_URL": str(SNAPSHOT),
}
ROLES = (
    "orchestrator",
    "present",
    "past",
    "supply",
    "strategist",
    "sentinel",
    "auditor",
    "liaison",
)


@pytest.fixture(scope="module")
def contributions() -> AddonContributions:
    clear_openapi_cache()
    return register_addons(
        load_addons_from_environment(dict(ENVIRONMENT)),
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=(),
    )


@pytest.fixture(scope="module")
def value(contributions: AddonContributions) -> dict[str, frozenset[str]]:
    return collect_audit_for(TOOL_PACKAGES, contributions)[0]


@pytest.fixture(scope="module")
def presence(contributions: AddonContributions) -> dict[str, frozenset[str]]:
    return collect_audit_for(TOOL_PACKAGES, contributions)[1]


def _glazed_tools(contributions: AddonContributions) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for role in ROLES:
        tools.update(contributions.toolsets[f"glazed_{role}"]().tools)
    return tools


def _sink(value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]) -> ar.PgAuditSink:
    return ar.PgAuditSink(
        lambda: None,  # type: ignore[arg-type, return-value]
        argument_allowlist=value,
        argument_presence_only=presence,
    )


def test_every_glazed_tool_has_an_audit_decision(
    contributions: AddonContributions,
    value: dict[str, frozenset[str]],
    presence: dict[str, frozenset[str]],
) -> None:
    decided = set(value) | set(presence)
    missing = sorted(set(_glazed_tools(contributions)) - decided)
    assert not missing, f"glazed tools without an audit argument decision: {missing}"


def test_decisions_name_only_real_parameters_and_cover_all_of_them(
    contributions: AddonContributions,
    value: dict[str, frozenset[str]],
    presence: dict[str, frozenset[str]],
) -> None:
    for name, tool in _glazed_tools(contributions).items():
        decided = set(value.get(name, ())) | set(presence.get(name, ()))
        model_params = set(tool.function_schema.json_schema["properties"])
        assert decided == model_params, name


def test_free_text_is_presence_only(
    value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]
) -> None:
    assert "rationale" in presence["propose_action"]
    assert "note" in presence["record_event"]
    assert "rationale" not in value["propose_action"]


def test_a_tool_without_arguments_is_a_decision_not_a_warning(
    value: dict[str, frozenset[str]],
    presence: dict[str, frozenset[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _sink(value, presence)
    with caplog.at_level(logging.WARNING):
        assert sink._redact("get_order_plan", {}) == {}
    assert "No audit argument decision" not in caplog.text


def test_new_tools_decisions_record_ids_by_value_and_free_text_by_presence(
    value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]
) -> None:
    assert value["check_compliance"] == {"decision_id"}
    assert value["classify_text"] == {"kind"}
    assert presence["classify_text"] == {"text"}
    assert value["audit_promo"] == {"department", "discount_pct"}
    assert value["get_integrity_issues"] == frozenset()


def test_snapshot_and_briefing_have_an_argument_free_audit_decision(
    value: dict[str, frozenset[str]],
) -> None:
    assert value["get_snapshot"] == frozenset()
    assert value["get_briefing"] == frozenset()


def test_redacted_date_arguments_are_json_serializable(
    value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]
) -> None:
    """A typed `date` argument must not crash the audit INSERT (json.dumps)."""
    redacted = _sink(value, presence)._redact(
        "forecast_demand", {"sku": "SKU-1", "target_date": date(2026, 7, 25)}
    )
    assert json.loads(json.dumps(redacted)) == {"sku": "SKU-1", "target_date": "2026-07-25"}


def test_note_and_transfer_tools_audit_free_text_by_presence_and_the_rest_by_value(
    value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]
) -> None:
    assert presence["save_manager_note"] == {"text"}
    assert "text" not in value["save_manager_note"]
    assert value["save_manager_note"] == {"category", "date_from", "date_to", "impact"}
    assert value["get_transfer_options"] == {"sku", "limit"}
    assert value["propose_transfer"] == {"option_id"}
    assert presence["propose_transfer"] == {"rationale"}
    assert "offer_surplus" not in value


# --- beyond the port: the whole old map, entry by entry ---------------------------------------

# The glazed entries of ARGUMENT_ALLOWLIST / ARGUMENT_PRESENCE_ONLY on vertical/glazed, verbatim.
OLD_BY_VALUE: dict[str, set[str]] = {
    "get_snapshot": set(),
    "get_briefing": set(),
    "get_kpis": {"date_from", "date_to"},
    "get_day_summary": {"date"},
    "get_issues": {"limit", "category", "kind"},
    "explain_metric": {"metric"},
    "get_order_plan": set(),
    "project_inventory": {"sku", "horizon"},
    "forecast_demand": {"sku", "target_date"},
    "forecast_daily_flow": {"target_date"},
    "get_inventory_status": {"sort", "limit"},
    "get_expiring": {"days"},
    "get_deliveries": {"days"},
    "get_staffing": {"days"},
    "get_promos": {"status", "days"},
    "get_outlook": {"days"},
    "get_issue_analysis": {"issue_id"},
    "get_supplier_performance": set(),
    "get_history": set(),
    "evaluate_promo": {"promo_id"},
    "recall_experiences": {"limit"},
    "record_event": {"type", "date_from", "date_to"},
    "propose_action": {"issue_id", "option_id"},
    "save_manager_note": {"category", "date_from", "date_to", "impact"},
    "get_transfer_options": {"sku", "limit"},
    "propose_transfer": {"option_id"},
    "check_compliance": {"decision_id"},
    "classify_text": {"kind"},
    "audit_promo": {"department", "discount_pct"},
    "get_integrity_issues": set(),
}
OLD_BY_PRESENCE: dict[str, set[str]] = {
    "propose_action": {"rationale"},
    "record_event": {"note"},
    "recall_experiences": {"query"},
    "classify_text": {"text"},
    "save_manager_note": {"text"},
    "propose_transfer": {"rationale"},
}


def test_the_addon_decides_exactly_the_arguments_the_vertical_branch_recorded(
    value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]
) -> None:
    for tool, fields in OLD_BY_VALUE.items():
        assert set(value[tool]) == fields, tool
    for tool, fields in OLD_BY_PRESENCE.items():
        assert set(presence[tool]) == fields, tool
    # nothing else of the addon's: Core's own decisions are not changed and no extra tool appears
    assert {t for t in value if t in OLD_BY_VALUE} == set(OLD_BY_VALUE)
    assert {t for t in presence if t in OLD_BY_PRESENCE} == set(OLD_BY_PRESENCE)


def test_a_call_is_recorded_as_it_was_on_the_vertical_branch(
    value: dict[str, frozenset[str]], presence: dict[str, frozenset[str]]
) -> None:
    sink = _sink(value, presence)

    row = sink._redact(
        "propose_action",
        {"issue_id": "ISS-1", "option_id": "OPT-2", "rationale": "the manager's words", "x": 1},
    )

    assert row == {"issue_id": "ISS-1", "option_id": "OPT-2", "rationale": ar.PRESENCE_RECORDED}
