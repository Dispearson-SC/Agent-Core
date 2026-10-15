"""The fast path's switch, thresholds and decision rules (Phase 5, S10).

Subject: domain/fast_path.py.

THE RULES UNDER TEST, in their order: an action request passes to the entry profile; a message that
depends on the conversation (or might: a value at or above the follow-up threshold, or no answer
when the session has history) passes; a complex one passes; more intents than `max_intents`
passes; one or more intents at or above the intent threshold is a composite; otherwise the
existing route logic runs (`defer:no_intent`). A missing action or complexity answer assumes
nothing (`defer:unassessed`).
"""

from __future__ import annotations

import pytest

from agent_core.domain.fast_path import (
    COMPOSITE,
    DEFER_NO_INTENT,
    PASS_ACTION,
    PASS_COMPLEX,
    PASS_FOLLOW_UP,
    PASS_TOO_MANY,
    Assessment,
    CompositeCall,
    CompositeMember,
    FastPathMode,
    FastPathThresholds,
    decide_fast_path,
)

T = FastPathThresholds()


def _assess(
    intents: dict[str, float] | None = None,
    *,
    action: float | None = 0.02,
    follow_up: float | None = None,
    complexity: float | None = 0.1,
) -> Assessment:
    return Assessment.of(
        intents or {}, action=action, follow_up=follow_up, complexity=complexity
    )


def test_the_switch_has_three_modes() -> None:
    assert [m.value for m in FastPathMode] == ["off", "shadow", "on"]
    assert FastPathMode("shadow") is FastPathMode.SHADOW


def test_two_intents_above_the_threshold_are_a_composite_of_both_sorted() -> None:
    verdict = decide_fast_path(
        _assess({"order_plan": 0.81, "day_summary": 0.93, "staffing": 0.2}), T, has_history=False
    )
    assert verdict.code == COMPOSITE and verdict.composite
    assert verdict.intents == ("day_summary", "order_plan")


def test_one_intent_above_the_threshold_is_a_composite_too() -> None:
    verdict = decide_fast_path(_assess({"day_summary": 0.5}), T, has_history=False)
    assert (verdict.code, verdict.intents) == (COMPOSITE, ("day_summary",))


def test_no_intent_above_the_threshold_defers_to_the_existing_logic() -> None:
    verdict = decide_fast_path(_assess({"day_summary": 0.49}), T, has_history=False)
    assert (verdict.code, verdict.intents) == (DEFER_NO_INTENT, ())


def test_an_action_request_passes_whatever_intents_it_also_names() -> None:
    verdict = decide_fast_path(_assess({"order_plan": 0.95}, action=0.7), T, has_history=False)
    assert verdict.code == PASS_ACTION and verdict.passes
    assert verdict.intents == ("order_plan",), "the detected intents are still recorded"


def test_a_message_that_depends_on_the_conversation_passes() -> None:
    verdict = decide_fast_path(
        _assess({"order_plan": 0.95}, follow_up=0.6), T, has_history=True
    )
    assert verdict.code == PASS_FOLLOW_UP


def test_a_low_confidence_independence_passes_too() -> None:
    """Above the (low) follow-up threshold the router is not sure the message stands alone."""
    verdict = decide_fast_path(
        _assess({"order_plan": 0.95}, follow_up=0.3), T, has_history=True
    )
    assert verdict.code == PASS_FOLLOW_UP


def test_a_follow_up_question_nobody_answered_passes_when_there_is_history() -> None:
    verdict = decide_fast_path(_assess({"order_plan": 0.95}), T, has_history=True)
    assert verdict.code == PASS_FOLLOW_UP


def test_a_standalone_message_in_a_conversation_may_be_a_composite() -> None:
    verdict = decide_fast_path(
        _assess({"order_plan": 0.95}, follow_up=0.05), T, has_history=True
    )
    assert verdict.code == COMPOSITE


def test_the_follow_up_answer_is_ignored_without_history() -> None:
    verdict = decide_fast_path(
        _assess({"order_plan": 0.95}, follow_up=0.9), T, has_history=False
    )
    assert verdict.code == COMPOSITE


def test_a_complex_message_passes() -> None:
    verdict = decide_fast_path(_assess({"order_plan": 0.95}, complexity=0.6), T, has_history=False)
    assert verdict.code == PASS_COMPLEX


def test_more_intents_than_the_budget_passes() -> None:
    four = {name: 0.9 for name in ("a", "b", "c", "d")}
    assert decide_fast_path(_assess(four), T, has_history=False).code == PASS_TOO_MANY
    roomy = FastPathThresholds(max_intents=4)
    assert decide_fast_path(_assess(four), roomy, has_history=False).code == COMPOSITE


@pytest.mark.parametrize("missing", ["action", "complexity"])
def test_a_missing_answer_assumes_nothing(missing: str) -> None:
    assessment = _assess({"order_plan": 0.95}, **{missing: None})  # type: ignore[arg-type]
    assert decide_fast_path(assessment, T, has_history=False).code == "defer:unassessed"


def test_the_rules_run_in_order_action_first() -> None:
    everything = _assess({"a": 0.9}, action=0.9, follow_up=0.9, complexity=0.9)
    assert decide_fast_path(everything, T, has_history=True).code == PASS_ACTION


def test_the_thresholds_are_configurable_and_checked() -> None:
    strict = FastPathThresholds(intent=0.9)
    assert decide_fast_path(_assess({"a": 0.8}), strict, has_history=False).code == DEFER_NO_INTENT
    for bad in ({"intent": 1.5}, {"action": -0.1}, {"follow_up": float("nan")}, {"max_intents": 0}):
        with pytest.raises(ValueError):
            FastPathThresholds(**bad)  # type: ignore[arg-type]


def test_an_assessment_refuses_impossible_values_and_summarises_every_answer() -> None:
    with pytest.raises(ValueError):
        Assessment.of({"a": 1.2})
    with pytest.raises(ValueError):
        Assessment(intents=(("a", 0.1), ("a", 0.2)))
    with pytest.raises(ValueError):
        Assessment.of({}, action=2.0)
    assessment = _assess({"b": 0.1, "a": 0.934}, follow_up=None, complexity=0.21)
    assert assessment.intents == (("a", 0.934), ("b", 0.1))
    assert assessment.summary() == "intents=a:0.93,b:0.10;action=0.02;follow_up=-;complexity=0.21"


def test_a_composite_orders_its_members_by_intent_and_needs_one() -> None:
    call = CompositeCall(
        (CompositeMember("order_plan", "supply", 1), CompositeMember("day_summary", "present", 1))
    )
    assert call.intents == ("day_summary", "order_plan")
    with pytest.raises(ValueError):
        CompositeCall(())
