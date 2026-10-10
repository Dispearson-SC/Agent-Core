"""Glazed prompts and tool docstrings must match the Wave 5 backend contract.

Subject: profiles/glazed_*.yaml personas and the docstrings of glazed/tools.py. The
mandatory rules keep the LLM from inventing numbers ("code computes, LLM explains").
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.adapters.driven.tools.glazed import tools as gt

PROFILES = Path(__file__).resolve().parents[2] / "profiles"
ROLES = [
    "orchestrator",
    "present",
    "past",
    "supply",
    "strategist",
    "sentinel",
    "auditor",
    "liaison",
]
OPTION_ROLES = ["orchestrator", "present", "supply", "strategist"]


def _persona(role: str) -> str:
    return load_profile_sync(PROFILES / f"glazed_{role}.yaml").persona


@pytest.mark.parametrize("role", ROLES)
def test_every_profile_refers_to_the_store_by_display_name(role: str) -> None:
    persona = _persona(role)
    assert "store_display_name" in persona
    assert "never by code" in persona.lower()


@pytest.mark.parametrize("role", ROLES)
def test_every_profile_takes_numbers_only_verbatim_from_tools(role: str) -> None:
    persona = _persona(role).lower()
    assert "verbatim from a tool result" in persona
    assert "not available" in persona
    assert "never estimate" in persona


@pytest.mark.parametrize("role", OPTION_ROLES)
def test_option_profiles_use_only_issue_options_including_do_nothing(role: str) -> None:
    persona = _persona(role)
    assert "Issue.options" in persona
    assert "do_nothing" in persona
    assert "never invent an option" in persona.lower()


def test_orchestrator_recommends_with_option_fields_and_confidence() -> None:
    persona = _persona("orchestrator")
    for field in ("net_usd_mid", "loss_impact", "confidence.score", "tier", "urgency"):
        assert field in persona
    lowered = persona.lower()
    assert "if-act" in lowered and "if-not" in lowered
    assert "low" in lowered and "confidence" in lowered


def test_orchestrator_proposes_by_ids_only() -> None:
    persona = _persona("orchestrator")
    assert "issue_id" in persona and "option_id" in persona


def test_thin_personas_claim_nothing_beyond_their_tools() -> None:
    sentinel = _persona("sentinel").lower()
    assert "only what" in sentinel and "get_history" in sentinel
    assert "verify whether past decisions worked" not in sentinel
    auditor = _persona("auditor").lower()
    assert "only what" in auditor and "get_issues" in auditor
    assert "anomalies (inventory, prices, sensors" not in auditor


def test_propose_action_takes_only_ids_and_rationale() -> None:
    params = list(inspect.signature(gt.propose_action).parameters)
    assert params == ["ctx", "issue_id", "option_id", "rationale"]


DOC_EXPECTATIONS = {
    "get_issues": ["options", "do_nothing", "evidence", "data_caveats", "store_display_name"],
    "get_order_plan": [
        "service_level_target",
        "order_deadline",
        "projected_stockout_date",
        "stockout_risk_without_order",
        "vs_current_practice",
        "min_order_warning",
    ],
    "get_kpis": ["waste_usd", "lost_sales_usd", "low", "mid", "high"],
    "propose_action": ["issue_id", "option_id", "rationale", "backend"],
    "check_compliance": [
        "execution_status",
        "not_executed",
        "within_range",
        "external_event",
        "forecast_error",
        "decision_id",
    ],
    "classify_text": ["rejection_reason", "deviation_cause", "label", "confidence", "calibrated"],
    "audit_promo": ["verdict", "block", "allow", "insufficient_evidence", "similar", "reasons"],
    "get_integrity_issues": ["exposure_usd", "data_fix_task", "integrity", "do_nothing"],
}


@pytest.mark.parametrize("tool", sorted(DOC_EXPECTATIONS))
def test_tool_docstrings_describe_the_wave5_contract(tool: str) -> None:
    doc = inspect.getdoc(getattr(gt, tool)) or ""
    for term in DOC_EXPECTATIONS[tool]:
        assert term in doc, f"{tool} docstring lacks {term}"


@pytest.mark.parametrize("tool", ["get_issues", "get_order_plan", "get_kpis"])
def test_optional_fields_are_flagged_as_never_to_estimate(tool: str) -> None:
    doc = inspect.getdoc(getattr(gt, tool)) or ""
    assert "may be absent; never estimate it yourself" in doc


def test_orchestrator_asks_exactly_one_specialist_per_step() -> None:
    """Resuming with one peer answer while two ask_peer calls are deferred hangs the turn."""
    persona = _persona("orchestrator")
    assert "Ask exactly ONE specialist per step" in persona
    assert "Never issue two ask_peer calls in the same step" in persona
