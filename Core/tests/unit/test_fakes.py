"""Fakes contract - t-f0-01.

Every fake must satisfy the Protocol it stands in for (structural typing, checked with
mypy against the real port module, not a hand-rolled `isinstance`) and `FakeAuditSink`
must record calls in ORDER, not merely in content - a dict keyed by call kind would
silently collapse two `record_tool_call`s into one and answer "why was this allowed?"
with the wrong story. CLAUDE.md non-negotiable #6.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import CallerIdentity, TenantId, ToolCallId, TurnId, Usage

# Imported as a module, not `from tests.fakes.ports import FakeAuditSink`: the stub file
# is valid Python with no classes yet, so a name-level import would turn "not implemented"
# into a collection-time ImportError instead of a red assertion inside the test body.
from tests.fakes import ports as fakes

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"
FAKES_MODULE = CORE_DIR / "tests" / "fakes" / "ports.py"

# Appended after the real fakes module source, so the check runs against the actual
# implementation instead of a hand-duplicated stub.
_ASSIGNMENTS = """
from agent_core.domain.compaction import CompactionCheckpoint  # noqa: F401
from agent_core.domain.policy import RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    SessionRef,
    TenantId as _TenantId,
    TurnId,
    TurnOutcome,
    TurnResult,
)
from agent_core.ports.agent_runner import AgentRunner
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.conversation_store import ConversationStore
from agent_core.ports.model_gateway import ModelGateway
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.tool_provider import ToolProvider

_profile = AgentProfile(id="a", persona="p", model="m")
_outcome = TurnOutcome(turn_id=TurnId("t1"), result=TurnResult(text="hi"))
_rules = RuleSet(subject_roles=frozenset(), channel="cli")
_session = SessionRef(session_id="s1", tenant_id=_TenantId("t1"))  # type: ignore[arg-type]

runner: AgentRunner = FakeAgentRunner(outcome=_outcome)
provider: ToolProvider = FakeToolProvider(toolset=object(), tool_names=("noop",))
policy: ToolPolicy = FakeToolPolicy(rules=_rules)
store: ConversationStore = FakeConversationStore()
sink: AuditSink = FakeAuditSink()
gateway: ModelGateway = FakeModelGateway()
"""


def _type_check(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check the real fakes module plus one assignment per port.

    Concatenating the real source (rather than importing `tests.fakes.ports` as a
    package) keeps this test independent of package resolution and proves the actual
    implementation - not a duplicate - satisfies each Protocol.
    """
    source = FAKES_MODULE.read_text(encoding="utf-8") + "\n" + _ASSIGNMENTS
    module = tmp_path / "snippet.py"
    module.write_text(source, encoding="utf-8")

    env = dict(os.environ)
    env["MYPYPATH"] = str(SRC_DIR)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=str(CORE_DIR),
        env=env,
        check=False,
    )


@pytest.mark.phase("F0")
def test_every_fake_type_checks_against_its_port(tmp_path: Path) -> None:
    result = _type_check(tmp_path)

    assert result.returncode == 0, (
        "A fake must be assignable to the Protocol it stands in for.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F0")
@pytest.mark.silent
def test_fake_audit_sink_records_calls_in_order() -> None:
    sink = fakes.FakeAuditSink()
    caller = CallerIdentity(subject_id="u-1", channel="cli", tenant_id=TenantId("t-1"))
    decision = PolicyDecision(effect=Effect.ALLOW, reason="ok", rule_id="r-1")
    turn_id = TurnId("turn-1")

    async def _drive() -> None:
        # Two calls to the SAME method, interleaved with a different one. A fake that
        # keyed its recording by call kind (e.g. a dict) would collapse the two
        # tool_call entries into one and lose the fact that tool_a happened before the
        # human decision, which happened before tool_b.
        await sink.record_tool_call(turn_id, caller, "tool_a", {"x": 1}, decision)
        await sink.record_human_decision(turn_id, ToolCallId("call-1"), "u-1", True, "approved")
        await sink.record_tool_call(turn_id, caller, "tool_b", {"y": 2}, decision)
        await sink.record_turn_end(turn_id, Usage(input_tokens=10), Decimal("0.01"))

    asyncio.run(_drive())

    kinds = [call.kind for call in sink.calls]
    assert kinds == ["tool_call", "human_decision", "tool_call", "turn_end"], (
        "FakeAuditSink must preserve call ORDER, not merely record content."
    )

    tool_names = [call.payload[2] for call in sink.calls if call.kind == "tool_call"]
    assert tool_names == ["tool_a", "tool_b"], (
        "Both tool_call recordings must survive, in the order they happened."
    )
