"""ToolPolicy - a SILENT-BUG AREA. These tests are the only alarm that exists.

Each test below encodes a decision from domain/policy.py. If one starts failing, the
question is which behaviour changed, not how to make it pass.
"""

import pytest


@pytest.mark.silent
@pytest.mark.skip(reason="F1 - docs/TASKS.md#t-f1-02")
def test_no_matching_rule_denies() -> None:
    """An unknown tool name is a typo or an unregistered addition. Both deserve refusal.

    ALLOW-by-default means every newly imported tool is silently world-usable.
    """


@pytest.mark.silent
@pytest.mark.skip(reason="F1")
def test_deny_beats_needs_approval_beats_allow() -> None:
    """EFFECT_PRECEDENCE, regardless of rule order or specificity. The only ordering that
    fails safe."""


@pytest.mark.silent
@pytest.mark.skip(reason="F1")
def test_empty_role_set_means_any_role_not_no_roles() -> None:
    """The asymmetry that bites: empty `subject_roles` on a PolicyRule means ANY role,
    while empty `accepted_kinds` on a MediaPolicy means NOTHING.

    Getting the policy one backwards makes a rule silently stop applying to everybody.
    """


@pytest.mark.silent
@pytest.mark.skip(reason="F1")
def test_store_unreachable_fails_closed() -> None:
    """Postgres down -> DENY and surface the error. An agent running unrestricted because
    the database blipped is worse than an agent that stops."""


@pytest.mark.silent
@pytest.mark.skip(reason="F6")
def test_mcp_tool_obeys_the_same_policy_as_a_local_tool() -> None:
    """CLAUDE.md non-negotiable #4. An MCP server is third-party code."""


@pytest.mark.silent
@pytest.mark.skip(reason="F6")
def test_mcp_server_cannot_shadow_a_local_tool_name() -> None:
    """Register a hostile server advertising `write_file`. The local tool must win."""
