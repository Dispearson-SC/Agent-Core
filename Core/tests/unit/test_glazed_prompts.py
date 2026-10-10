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
    assert "only what" in auditor and "get_integrity_issues" in auditor
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


@pytest.mark.parametrize("role", ROLES)
def test_every_profile_answers_only_in_spanish(role: str) -> None:
    """Live replies once carried stray tokens in Chinese and invented Spanish-looking words."""
    persona = _persona(role)
    assert "Respond only in Spanish; never output words from other languages or scripts" in persona


def test_orchestrator_never_claims_a_capability_is_unavailable_without_a_tool_result() -> None:
    persona = _persona("orchestrator").lower()
    assert "never claim a capability is unavailable" in persona
    assert "verbatim" in persona


def test_orchestrator_classifies_a_rejection_and_mentions_the_category() -> None:
    persona = _persona("orchestrator")
    assert "classify_text" in persona and "rejection_reason" in persona
    assert "category" in persona.lower()


def test_sentinel_explains_deviations_with_compliance_and_classification() -> None:
    persona = _persona("sentinel")
    for term in ("check_compliance", "classify_text", "deviation_cause", "execution_status"):
        assert term in persona


def test_strategist_audits_every_promo_and_does_nothing_on_block() -> None:
    persona = _persona("strategist")
    assert "audit_promo" in persona and "before" in persona.lower()
    assert "block" in persona and "do_nothing" in persona


def test_auditor_reports_integrity_issues_with_their_exposure() -> None:
    persona = _persona("auditor")
    assert "get_integrity_issues" in persona and "exposure_usd" in persona


def test_past_follows_the_verify_hint_of_a_recalled_experience() -> None:
    persona = _persona("past")
    assert "verify" in persona and "ref" in persona
    assert "evaluate_promo" in persona and "get_history" in persona


RULES = {
    "arith": (
        "an option's advantage over do_nothing is its net_usd_mid (do_nothing has net 0); "
        "never subtract do_nothing's cost from an option's net or invent differences"
    ),
    "status": (
        "a proposal with status approved IS an approved decision; executions and compliance "
        "come from check_compliance; never ask the manager whether they approved something "
        "the tools show as approved"
    ),
    "scope": (
        "decisions and history may include other cases of the same store; when answering "
        "about this conversation, prefer decisions of the current case and say when you cite "
        "another case"
    ),
    "cause": (
        "when explaining a deviation, cite recorded events (for example bloqueo) from "
        "compliance or events as candidate causes"
    ),
    "reject": (
        "only generalize a rejection reason to options of the same action_type or kind"
    ),
    "units": (
        "never treat a summed metric as a data defect without comparing like-for-like units "
        "(for example stockout_hours is summed across SKUs and is not comparable with "
        "staff_hours)"
    ),
}
ROLE_RULES = {
    "orchestrator": ["arith", "status", "scope", "cause", "reject", "units"],
    "present": ["arith", "units"],
    "past": ["scope", "reject"],
    "supply": ["arith", "units"],
    "strategist": ["arith", "reject"],
    "sentinel": ["status", "scope", "cause"],
    "auditor": ["units"],
}


def _flat(role: str) -> str:
    return " ".join(_persona(role).split()).lower()


@pytest.mark.parametrize(
    ("role", "rule"), [(r, k) for r, ks in ROLE_RULES.items() for k in ks]
)
def test_personas_carry_the_reasoning_rules(role: str, rule: str) -> None:
    assert RULES[rule].lower() in _flat(role)


SPECIALIST_ROLES = ["present", "past", "supply", "strategist", "sentinel"]


@pytest.mark.parametrize("role", SPECIALIST_ROLES)
def test_specialists_have_a_small_tool_budget(role: str) -> None:
    profile = load_profile_sync(PROFILES / f"glazed_{role}.yaml")
    assert profile.max_iterations <= 8
    assert "call each tool at most twice per question" in _flat(role)


@pytest.mark.parametrize("role", ["present", "strategist"])
def test_kpi_readers_use_one_window_not_per_day_calls(role: str) -> None:
    assert (
        "use get_kpis with a date window once instead of per-day calls" in _flat(role)
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_profile_lists_the_language_glitches_to_avoid(role: str) -> None:
    flat = _flat(role)
    assert "language glitches to avoid" in flat
    for pattern in ("mixed-script words", '"both"', '"informed"', '"leak"'):
        assert pattern in flat


def test_orchestrator_starts_from_the_briefing_and_asks_specialists_only_to_drill_down() -> None:
    flat = _flat("orchestrator")
    assert "call get_briefing first on every question" in flat
    assert "answer directly without asking any specialist" in flat
    assert "ask exactly one specialist only for drill-down the briefing lacks" in flat
    assert "top_issues" in flat and "issue_id" in flat and "option_id" in flat
    profile = load_profile_sync(PROFILES / "glazed_orchestrator.yaml")
    assert profile.max_iterations <= 6


@pytest.mark.parametrize("role", ["present", "past", "supply", "strategist"])
def test_specialists_call_get_snapshot_first_and_do_not_refetch_its_data(role: str) -> None:
    flat = _flat(role)
    assert "call get_snapshot first" in flat
    assert "do not re-fetch the same data" in flat
    assert load_profile_sync(PROFILES / f"glazed_{role}.yaml").max_iterations <= 6


def test_briefing_and_snapshot_docstrings_name_their_fields() -> None:
    briefing = inspect.getdoc(gt.get_briefing) or ""
    for term in ("snapshot", "headline", "suggested_focus", "top_issues"):
        assert term in briefing
    snapshot = inspect.getdoc(gt.get_snapshot) or ""
    for term in ("kpis", "top_issues", "order_plan", "suppliers", "integrity", "may be absent"):
        assert term in snapshot
