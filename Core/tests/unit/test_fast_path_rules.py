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


# --- P13: the coverage guard and the route choice as a second signal ----------------------------


def test_a_message_asking_more_things_than_the_detected_intents_passes_uncovered() -> None:
    from agent_core.domain.fast_path import PASS_UNCOVERED

    # "How am I doing today and what do I order?": two requests, only order_plan detected. A
    # composite would answer the order and claim the day's figures are unavailable (P12 battery).
    assessment = Assessment.of(
        {"order_plan": 0.81, "day_summary": 0.2}, action=0.02, complexity=0.1, requests=2
    )
    verdict = decide_fast_path(assessment, T, has_history=False)
    assert verdict.code == PASS_UNCOVERED == "pass:uncovered"
    assert verdict.passes and not verdict.composite
    assert verdict.intents == ("order_plan",)


def test_a_message_whose_requests_the_detected_intents_cover_is_a_composite() -> None:
    both = Assessment.of(
        {"order_plan": 0.81, "day_summary": 0.9}, action=0.02, complexity=0.1, requests=2
    )
    assert decide_fast_path(both, T, has_history=False).code == COMPOSITE
    # More intents than requests: a ride-along read, still covered.
    one = Assessment.of({"a": 0.9, "b": 0.8}, action=0.02, complexity=0.1, requests=1)
    assert decide_fast_path(one, T, has_history=False).code == COMPOSITE


def test_without_a_requests_answer_the_p12_rules_hold() -> None:
    verdict = decide_fast_path(_assess({"order_plan": 0.81}), T, has_history=False)
    assert verdict.code == COMPOSITE


def test_the_coverage_guard_runs_after_the_earlier_rules() -> None:
    action = Assessment.of({"a": 0.9}, action=0.9, complexity=0.1, requests=3)
    assert decide_fast_path(action, T, has_history=False).code == PASS_ACTION
    none = Assessment.of({"a": 0.1}, action=0.02, complexity=0.1, requests=2)
    assert decide_fast_path(none, T, has_history=False).code == DEFER_NO_INTENT


def test_a_confident_route_choice_counts_its_intent_as_detected() -> None:
    # "Give me today's summary": the route choice said day_summary at 0.86, its yes/no 0.54.
    assessment = Assessment.of({"day_summary": 0.54}, action=0.02, complexity=0.1, requests=1)
    verdict = decide_fast_path(
        assessment, T, has_history=False, routed=("day_summary", 0.86)
    )
    assert verdict.code == COMPOSITE and verdict.intents == ("day_summary",)
    # It joins the yes/no detections, and it covers a request of its own.
    two = Assessment.of({"order_plan": 0.9}, action=0.02, complexity=0.1, requests=2)
    joined = decide_fast_path(two, T, has_history=False, routed=("day_summary", 0.9))
    assert joined.code == COMPOSITE and joined.intents == ("day_summary", "order_plan")


def test_an_unsure_route_choice_is_not_a_detection() -> None:
    assessment = Assessment.of({"day_summary": 0.3}, action=0.02, complexity=0.1)
    verdict = decide_fast_path(assessment, T, has_history=False, routed=("day_summary", 0.69))
    assert verdict.code == DEFER_NO_INTENT and verdict.intents == ()
    strict = FastPathThresholds(route_intent=0.95)
    sure = decide_fast_path(assessment, strict, has_history=False, routed=("day_summary", 0.9))
    assert sure.code == DEFER_NO_INTENT


def test_the_route_threshold_and_the_requests_answer_are_checked() -> None:
    assert T.route_intent == 0.7
    with pytest.raises(ValueError):
        FastPathThresholds(route_intent=1.5)
    for bad in (0, -1, 256, True):
        with pytest.raises(ValueError):
            Assessment.of({}, requests=bad)  # type: ignore[arg-type]
    counted = Assessment.of({"a": 0.5}, action=0.02, complexity=0.2, requests=2)
    assert counted.summary() == "intents=a:0.50;action=0.02;follow_up=-;complexity=0.20;requests=2"
