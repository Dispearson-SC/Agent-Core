"""The glazed routes file for `system_one_router`: glazed_vertical/routing/system_one_routes.yaml.

THE RULES UNDER TEST. The file is a valid routes file for the generic router; its targets are
exactly the routes the glazed routing question set labels (the ten layer-B intents and the seven
specialists), so the benchmark measures every one of them; and the vertical does not turn the
router on by itself (it does not depend on the router addon; the orchestrator's opt-in is inert
without it).
"""

from __future__ import annotations

import yaml
from evals.routing import PASS, load_question_set

from tests.unit import glazed_vertical_support as glazed
from tests.unit.test_system_one_router import SYSTEM_ONE

ROUTES = glazed.ADDON / "routing" / "system_one_routes.yaml"
QUESTIONS = glazed.ADDON / "evals" / "routing_questions.yaml"


def test_the_routes_file_is_valid_and_targets_every_labelled_route_of_the_set() -> None:
    table = SYSTEM_ONE.parse_routes(
        yaml.safe_load(ROUTES.read_text(encoding="utf-8")),
        escape_label="none_of_these",
        source=str(ROUTES),
    )
    targets = {f"{route.kind}:{route.name}" for route in table.routes}
    labelled = {q.expected for q in load_question_set(QUESTIONS).questions} - {PASS}

    assert targets == labelled
    assert sum(route.kind == "intent" for route in table.routes) == 10
    assert sum(route.kind == "profile" for route in table.routes) == 7


def test_the_vertical_does_not_enable_the_router() -> None:
    """The deployment enables the router addon and AGENT_CORE_TURN_ROUTING; the vertical never
    depends on it, so routing stays off-switchable (the orchestrator's block is inert without
    it)."""
    manifest = yaml.safe_load((glazed.ADDON / "addon.yaml").read_text(encoding="utf-8"))
    assert "system_one_router" not in manifest.get("depends", [])
