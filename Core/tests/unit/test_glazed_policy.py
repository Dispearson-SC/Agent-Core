"""The glazed policy rules live in the addon and decide exactly as they did on vertical/glazed (G3).

Subject: `addons/glazed_tools/policy/rules.yaml` as the root registers it, reduced by the real
`PgToolPolicy.decide` (DENY beats NEEDS_APPROVAL beats ALLOW).
Oracle: `fixtures/glazed_tools/old_glazed_policy_rules.yaml`, the glazed section of the vertical
branch's `Core/policy/rules.yaml` verbatim (34 rules).

ONE RULE CANNOT MOVE INTO THE ADDON (D88). `glazed-orchestrator-asks-specialists` allows `ask_peer`,
a tool of Core, and an addon may allow only its own tools. It stays a rule of the deployment's own
policy directory (`DEPLOYMENT_RULES` below is its text, which the glazed deployment carries); the
other 33 are the addon's. The parity test runs the addon's rules plus that one against the oracle.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
import yaml

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError
from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import AddonContributions, register_addons
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.policy_fs.loader import parse_policy_rules
from agent_core.adapters.driven.tools.http_bridge import clear_openapi_cache
from agent_core.composition import TOOL_PACKAGES
from agent_core.domain.policy import Effect, PolicyDecision, PolicyRule, RuleSet
from agent_core.domain.turn import CallerIdentity, TenantId

HERE = Path(__file__).resolve()
FIXTURES = HERE.parents[1] / "fixtures" / "glazed_tools"
ADDONS_DIR = HERE.parents[3] / "addons"
ENVIRONMENT = {
    "AGENT_CORE_ADDONS": "http_tool_bridge,glazed_tools",
    "AGENT_CORE_ADDONS_DIR": str(ADDONS_DIR),
    "AGENT_CORE_ADDON__GLAZED_TOOLS__OPENAPI_URL": str(FIXTURES / "internal_openapi.json"),
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

# The one glazed rule the deployment keeps (an addon cannot allow Core's `ask_peer`).
DEPLOYMENT_RULES = dedent(
    """\
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
)
MOVED = "glazed-orchestrator-asks-specialists"

MANAGER = ("glazed-manager", "glazed")
SERVICE = ("glazed-service", "glazed")
PEER = ("peer", "peer")


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
def old_rules() -> tuple[PolicyRule, ...]:
    return parse_policy_rules(
        (FIXTURES / "old_glazed_policy_rules.yaml").read_text(encoding="utf-8"), source="old"
    )


@pytest.fixture(scope="module")
def new_rules(contributions: AddonContributions) -> tuple[PolicyRule, ...]:
    return (*contributions.policy_rules, *parse_policy_rules(DEPLOYMENT_RULES, source="deploy"))


def _decide(
    rules: tuple[PolicyRule, ...],
    tool: str,
    identity: tuple[str, str],
    arguments: dict[str, object] | None = None,
) -> PolicyDecision:
    role, channel = identity
    caller = CallerIdentity("subject", channel, TenantId("S030"), frozenset({role}))
    policy = PgToolPolicy(lambda: None)  # type: ignore[arg-type, return-value]
    return policy.decide(RuleSet.for_caller(caller, rules), tool, arguments or {})


def _glazed_tools(contributions: AddonContributions) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for role in ROLES:
        tools.update(contributions.toolsets[f"glazed_{role}"]().tools)
    return tools


# --- the rules are the addon's ------------------------------------------------------------------


def test_the_addon_declares_a_policy_file_with_every_glazed_rule_but_one(
    contributions: AddonContributions, old_rules: tuple[PolicyRule, ...]
) -> None:
    addon_ids = [r.rule_id for r in contributions.policy_rules]

    assert len(addon_ids) == len(set(addon_ids)) == 33
    assert set(addon_ids) | {MOVED} == {r.rule_id for r in old_rules}
    assert MOVED not in addon_ids


def test_the_addon_grants_only_its_own_tools_and_the_rule_that_moved_is_why_ask_peer_is_not_here(
    contributions: AddonContributions,
) -> None:
    own = set(contributions.tools["glazed_tools"])
    for rule in contributions.policy_rules:
        if rule.effect is Effect.ALLOW:
            assert rule.tool_pattern in own, rule.rule_id
    assert not any(
        r.tool_pattern == "ask_peer" and r.effect is Effect.ALLOW
        for r in contributions.policy_rules
    )


# --- rule id -> outcome, for representative tool/caller pairs ---------------------------------

# (rule id, tool, identity, effect). Each row is read off the oracle file, not off the new one.
TABLE: list[tuple[str, str, tuple[str, str], Effect]] = [
    # specialists read through the peer turn, the service through its own channel
    ("glazed-read-get-snapshot", "get_snapshot", PEER, Effect.ALLOW),
    ("glazed-read-get-snapshot", "get_snapshot", SERVICE, Effect.ALLOW),
    ("glazed-read-get-kpis", "get_kpis", PEER, Effect.ALLOW),
    ("glazed-read-forecast-demand", "forecast_demand", SERVICE, Effect.ALLOW),
    ("glazed-read-get-transfer-options", "get_transfer_options", PEER, Effect.ALLOW),
    ("glazed-read-check-compliance", "check_compliance", SERVICE, Effect.ALLOW),
    ("glazed-read-get-integrity-issues", "get_integrity_issues", SERVICE, Effect.ALLOW),
    ("glazed-audit-promo", "audit_promo", PEER, Effect.ALLOW),
    ("glazed-record-event", "record_event", PEER, Effect.ALLOW),
    ("glazed-classify-text", "classify_text", PEER, Effect.ALLOW),
    ("glazed-classify-text", "classify_text", MANAGER, Effect.ALLOW),
    # the orchestrator's own tools
    ("glazed-read-get-briefing", "get_briefing", MANAGER, Effect.ALLOW),
    ("glazed-propose-action", "propose_action", MANAGER, Effect.ALLOW),
    ("glazed-save-manager-note", "save_manager_note", MANAGER, Effect.ALLOW),
    ("glazed-orchestrator-asks-specialists", "ask_peer", MANAGER, Effect.ALLOW),
    # a transfer proposal is a peer turn's alone
    ("glazed-propose-transfer", "propose_transfer", PEER, Effect.ALLOW),
    # the peer-turn denies: a specialist neither proposes nor asks
    ("glazed-peer-turn-no-propose-action", "propose_action", PEER, Effect.DENY),
    ("glazed-peer-turn-no-ask-peer", "ask_peer", PEER, Effect.DENY),
    # the manager identity matches no read row: even a mis-edited profile cannot read data
    (None, "get_snapshot", MANAGER, Effect.DENY),  # type: ignore[list-item]
    (None, "get_kpis", MANAGER, Effect.DENY),  # type: ignore[list-item]
    (None, "forecast_demand", MANAGER, Effect.DENY),  # type: ignore[list-item]
    # the writes of the manager turn are not available to the other identities
    (None, "save_manager_note", PEER, Effect.DENY),  # type: ignore[list-item]
    (None, "save_manager_note", SERVICE, Effect.DENY),  # type: ignore[list-item]
    (None, "propose_action", SERVICE, Effect.DENY),  # type: ignore[list-item]
    (None, "propose_transfer", SERVICE, Effect.DENY),  # type: ignore[list-item]
    (None, "get_briefing", PEER, Effect.DENY),  # type: ignore[list-item]
    # identities and channels that are not glazed's
    (None, "get_snapshot", ("operator", "cli"), Effect.DENY),  # type: ignore[list-item]
    (None, "get_snapshot", ("glazed-service", "http"), Effect.DENY),  # type: ignore[list-item]
    ("glazed-read-get-snapshot", "get_snapshot", ("peer", "glazed"), Effect.ALLOW),
    ("glazed-deny-everything-else", "glazed_anything", MANAGER, Effect.DENY),
    ("glazed-deny-everything-else", "glazed_anything", PEER, Effect.DENY),
]


@pytest.mark.parametrize(("rule_id", "tool", "identity", "effect"), TABLE)
def test_the_rule_that_decides_a_tool_for_a_caller(
    new_rules: tuple[PolicyRule, ...],
    old_rules: tuple[PolicyRule, ...],
    rule_id: str | None,
    tool: str,
    identity: tuple[str, str],
    effect: Effect,
) -> None:
    decision = _decide(new_rules, tool, identity)
    oracle = _decide(old_rules, tool, identity)

    assert (decision.effect, decision.rule_id) == (effect, rule_id)
    assert (oracle.effect, oracle.rule_id) == (effect, rule_id), "the table is the oracle's"


# --- parity: every tool x every identity decides as the vertical branch's file did ------------

IDENTITIES = [
    MANAGER,
    SERVICE,
    PEER,
    ("glazed-manager", "peer"),
    ("glazed-service", "peer"),
    ("peer", "glazed"),
    ("operator", "cli"),
    ("glazed-manager", "http"),
    ("anonymous", "http"),
]
FOREIGN_TOOLS = [
    "ask_peer",
    "orders_lookup",
    "GET_SNAPSHOT",
    "glazed_x",
    "get_snapshots",
    "sql_readonly",
]


def test_every_glazed_tool_decides_as_it_did_for_every_identity(
    contributions: AddonContributions,
    new_rules: tuple[PolicyRule, ...],
    old_rules: tuple[PolicyRule, ...],
) -> None:
    tools = sorted(_glazed_tools(contributions)) + FOREIGN_TOOLS
    assert len(tools) >= 30 + len(FOREIGN_TOOLS)
    differing = []
    for tool, identity in itertools.product(tools, IDENTITIES):
        new, old = _decide(new_rules, tool, identity), _decide(old_rules, tool, identity)
        if (new.effect, new.rule_id, new.reason) != (old.effect, old.rule_id, old.reason):
            differing.append((tool, identity, new.rule_id, old.rule_id))
    assert differing == []


def test_every_glazed_tool_is_reachable_by_at_least_one_identity(
    contributions: AddonContributions, new_rules: tuple[PolicyRule, ...]
) -> None:
    """A tool no identity may call would be a rule gone missing, which parity alone cannot see
    when the oracle lost it too."""
    for tool in _glazed_tools(contributions):
        allowed = [i for i in IDENTITIES if _decide(new_rules, tool, i).effect is Effect.ALLOW]
        assert allowed, tool


def test_without_the_deployment_rule_the_orchestrator_cannot_ask_a_peer(
    contributions: AddonContributions,
) -> None:
    """Why the deployment must carry `glazed-orchestrator-asks-specialists` (G7 note)."""
    decision = _decide(contributions.policy_rules, "ask_peer", MANAGER)

    assert decision.effect is Effect.DENY and decision.rule_id is None


# --- a deployment that cannot hold the rules refuses to start ------------------------------


def test_the_addon_needs_a_deployment_that_manages_policy_files() -> None:
    clear_openapi_cache()
    with pytest.raises(AddonRegistrationError, match="policy directory") as caught:
        register_addons(
            load_addons_from_environment(dict(ENVIRONMENT)),
            builtin_toolsets=TOOL_PACKAGES,
            builtin_fragments_dir=None,
            reserved_channels=(),
            manages_policy_files=False,
        )
    assert caught.value.addon == "glazed_tools" and caught.value.field == "provides.policy"


def test_the_rule_files_are_valid_yaml_documents() -> None:
    document: Any = yaml.safe_load(
        (ADDONS_DIR / "glazed_tools" / "policy" / "rules.yaml").read_text()
    )
    assert isinstance(document["rules"], list)
