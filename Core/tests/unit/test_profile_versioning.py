"""Profile replay from the turn audit record - D20, docs/DECISIONS.md.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-19
Status:  test only - there is no production file for this anchor. It exercises
         docs/TASKS.md#t-f1-17 (agent_core.domain.profile, ProfileVersionRegistry) and
         docs/TASKS.md#t-f1-18 (adapters/driven/persistence_pg/conversation_repository,
         profile_snapshot), both already implemented.

WHAT THIS PROVES
    `profile_version` alone does not answer "what had the agent been told to do" - a
    version number is only a pointer, and the file it points at may since have been
    edited or reverted. `profile_snapshot()` renders the RESOLVED profile as plain JSON
    at append time, so a turn row is self-sufficient. This test "replays" that stored
    record after the profile YAML changes under the same id, and asserts it still
    names the persona and approval rules that were IN FORCE at the turn it was taken
    for - never the ones the file says today.

    No database is needed: `profile_snapshot()` and `ProfileVersionRegistry` are both
    pure, so this belongs in `tests/unit/`, not `tests/integration/`.
"""

from __future__ import annotations

from pathlib import Path

from agent_core.adapters.driven.persistence_pg.conversation_repository import profile_snapshot
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.domain.profile import ProfileVersionRegistry


def _write_profile(path: Path, *, persona: str, approval_reason: str) -> Path:
    path.write_text(
        f"""
        id: audited_agent
        persona: "{persona}"
        model: claude-sonnet-5
        approval_rules:
          - tool_name: refund_issue
            reason: "{approval_reason}"
        """,
        encoding="utf-8",
    )
    return path


def test_replaying_a_turn_audit_record_reproduces_the_profile_in_force_at_that_turn(
    tmp_path: Path,
) -> None:
    registry = ProfileVersionRegistry()
    path = tmp_path / "audited_agent.yaml"

    # Turn N runs under this persona and this approval rule.
    _write_profile(path, persona="cautious refund agent", approval_reason="refund over $500")
    profile_at_turn = registry.assign(load_profile_sync(path))

    # This is the audit record a turn row stores at append time (t-f1-18): a plain
    # JSON copy taken from the profile that actually served the turn, not a reference
    # to whatever is on disk when someone reads it back later.
    turn_audit_record = profile_snapshot(profile_at_turn)

    # The profile YAML is edited afterwards under the SAME id - persona rewritten,
    # approval rule loosened - and a later turn runs under a new version.
    _write_profile(path, persona="lenient refund agent", approval_reason="refund over $5000")
    profile_after_edit = registry.assign(load_profile_sync(path))
    later_record = profile_snapshot(profile_after_edit)

    assert profile_after_edit.version > profile_at_turn.version

    # Replaying turn N's stored record must reproduce what turn N actually ran under.
    assert turn_audit_record["persona"] == "cautious refund agent"
    assert turn_audit_record["approval_rules"] == [
        {"tool_name": "refund_issue", "reason": "refund over $500", "condition": None}
    ]
    assert turn_audit_record["version"] == profile_at_turn.version

    # It must NOT have silently become the edited content: this is exactly the hole
    # D20 exists to close - a version number pointing at a file that has since moved.
    assert turn_audit_record["persona"] != later_record["persona"]
    assert turn_audit_record["approval_rules"] != later_record["approval_rules"]
