"""The glazed vertical as the root builds it from addons (Phase 3, G5): shared by the ported tests.

Not a test module. `profile(role)` is the merged profile of `addons/glazed_vertical` exactly as
`load_profile_sync` returned it from a vertical/glazed file (no version, no `ask_peer` grant), so
the ported assertions keep their old subject; `served()` is what `load_profiles` serves.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import AddonContributions, register_addons
from agent_core.adapters.driven.policy_fs.loader import load_policy_rules, parse_policy_rules
from agent_core.adapters.driven.profiles_fs.inheritance import (
    SlotInsert,
    read_profile_document,
    resolve_documents,
)
from agent_core.adapters.driven.tools.http_bridge import clear_openapi_cache
from agent_core.composition import TOOL_PACKAGES, load_profiles
from agent_core.domain.policy import PolicyRule
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry

HERE = Path(__file__).resolve()
CORE = HERE.parents[2]
ADDONS_DIR = HERE.parents[3] / "addons"
ADDON = ADDONS_DIR / "glazed_vertical"
TOOLS_FIXTURES = HERE.parents[1] / "fixtures" / "glazed_tools"
REFERENCE = HERE.parents[1] / "fixtures" / "glazed_vertical"
ENABLED = "http_tool_bridge,glazed_tools,plain_language,glazed_vertical"
ENVIRONMENT = {
    "AGENT_CORE_ADDONS": ENABLED,
    "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR),
    "AGENT_CORE_ADDONS_CONFIG": str(ADDON / "addons_config.yaml"),
    "AGENT_CORE_ADDON__GLAZED_TOOLS__BASE_URL": "http://backend.test:9",
    "AGENT_CORE_ADDON__GLAZED_TOOLS__OPENAPI_URL": str(TOOLS_FIXTURES / "internal_openapi.json"),
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
# The one glazed rule the deployment keeps: an addon cannot allow Core's `ask_peer` (D88).
DEPLOYMENT_RULES = """\
rules:
  - rule_id: glazed-orchestrator-asks-specialists
    tool_pattern: ask_peer
    effect: allow
    subject_roles:
      - glazed-manager
    channels:
      - glazed
    reason: >-
      The orchestrator delegates to its specialists. The two-sided allowlist and the hop
      limit still decide which peer.
"""


@cache
def contributions() -> AddonContributions:
    clear_openapi_cache()
    return register_addons(
        load_addons_from_environment(dict(ENVIRONMENT)),
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=CORE / "profiles" / "fragments",
        reserved_channels=("http", "cli"),
        builtin_rule_ids=tuple(r.rule_id for r in load_policy_rules(CORE / "policy")),
        manages_policy_files=True,
    )


@cache
def _merged() -> dict[str, AgentProfile]:
    added = contributions()
    documents = [
        read_profile_document(path, owner=addon, addon_fragments=added.fragments)
        for addon, path in added.profile_files
    ]
    inserts = tuple(
        SlotInsert(addon, profile, slot, fragment.read_text(encoding="utf-8").strip())
        for addon, profile, slot, fragment in added.inserts
    )
    profiles = [AgentProfile.from_mapping(m) for m in resolve_documents(documents, inserts)]
    return {p.id: p for p in profiles}


def profile(role: str) -> AgentProfile:
    return _merged()[f"glazed_{role}"]


def persona(role: str) -> str:
    return profile(role).persona


def served(profiles_dir: Path) -> dict[str, AgentProfile]:
    """What the root serves with the glazed addons enabled and `profiles_dir` as Core's own."""
    return load_profiles(profiles_dir, ProfileVersionRegistry(), contributions())


def reference() -> dict[str, AgentProfile]:
    """The eight profiles of vertical/glazed, verbatim, served the same way with no addon."""
    return load_profiles(REFERENCE, ProfileVersionRegistry())


def policy_rules() -> tuple[PolicyRule, ...]:
    """Core's policy files, the glazed addon's rules and the one rule the deployment keeps."""
    return (
        *load_policy_rules(CORE / "policy"),
        *contributions().policy_rules,
        *parse_policy_rules(DEPLOYMENT_RULES, source="glazed deployment"),
    )


# THE ONE INTENDED DIFFERENCE from vertical/glazed's orchestrator outside its section (S9, P11):
# the old persona asked one specialist per step (a workaround for a turn that hung resuming with one
# answer of two); Core collects every ask of a step (C10), so independent asks now go out together.
# Each old fragment occurs exactly once and is replaced by the new words; everything else stays
# byte for byte.
INTENDED_ORCHESTRATOR_CHANGES = (
    (
        "Ask exactly ONE specialist per step: wait for its answer before asking another.\n"
        "Never issue two ask_peer calls in the same step.\n",
        "When a question needs several specialists, ask them all in the SAME step: issue every\n"
        "independent ask_peer call together, so they work in parallel and you continue once with"
        " all\n"
        "their answers. Ask in a later step only when a question depends on an earlier"
        " specialist's\n"
        "answer (for example, the cover the supply specialist reports before deciding on an"
        " order).\n",
    ),
    (
        "asking any specialist. Ask exactly ONE specialist only for drill-down the briefing"
        " lacks.\n",
        "asking any specialist. Ask specialists only for drill-down the briefing lacks, every\n"
        "independent one in the same step.\n",
    ),
)


def with_intended_changes(old_persona: str) -> str:
    """`old_persona` (vertical/glazed's orchestrator) with the intended changes applied."""
    for before, after in INTENDED_ORCHESTRATOR_CHANGES:
        assert old_persona.count(before) == 1, before
        old_persona = old_persona.replace(before, after)
    return old_persona
