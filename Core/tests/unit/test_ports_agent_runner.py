"""AgentRunner port shape - the seam frozen as a type, not as behaviour.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-05

`AgentRunner` exists so use cases and tests never import Pydantic AI (D7). That only
holds while the seam is a CHECKED contract: a fake and the real adapter must be
interchangeable, and the compiler is the thing that proves it.

So this module asserts the port the way a caller actually meets it - by type-checking a
stub against it - and asserts the negative case too. A protocol that accepts anything
proves nothing, so a `run` with the wrong arity MUST be rejected. Without that second
test, a widened or silently-broken signature still looks green.

The checks run mypy in a subprocess over a throwaway module. `inspect` alone cannot do
this: it sees a name and an arity, never whether the assignment `x: AgentRunner = Stub()`
is legal, which is the only property the rest of the codebase relies on.

The port is also ASYNC on purpose (D13) - `run` wraps the longest await in the system -
so the structural guard below pins that too. A sync `def` here would force the adapter to
bridge back the wrong way.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.ports.agent_runner import AgentRunner

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"

CONFORMING_STUB = """
from __future__ import annotations

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest
from agent_core.ports.agent_runner import AgentRunner, ToolResolution


class StubRunner:
    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:
        raise NotImplementedError

    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        raise NotImplementedError


runner: AgentRunner = StubRunner()
"""

WRONG_ARITY_STUB = """
from __future__ import annotations

from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest
from agent_core.ports.agent_runner import AgentRunner, ToolResolution


class WrongArityRunner:
    # The signature the port carried BEFORE the turn_id seat was added. It is the exact
    # shape the lock now has to reject: a `run` that cannot be handed the identifier its
    # own return type requires.
    async def run(
        self, request: TurnRequest, profile: AgentProfile, history: object
    ) -> TurnOutcome:
        raise NotImplementedError

    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        raise NotImplementedError


runner: AgentRunner = WrongArityRunner()
"""


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module against the real port.

    Written outside the repository tree on purpose: a fixture that deliberately fails to
    type-check must never be picked up by the project-wide mypy run.
    """
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


@pytest.mark.phase("F1")
def test_stub_with_the_settled_signatures_type_checks_as_agent_runner(tmp_path: Path) -> None:
    result = _type_check(CONFORMING_STUB, tmp_path)

    assert result.returncode == 0, (
        "A stub carrying the settled run/resume signatures must satisfy AgentRunner.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F1")
def test_stub_with_a_wrong_arity_run_does_not_type_check_as_agent_runner(tmp_path: Path) -> None:
    result = _type_check(WRONG_ARITY_STUB, tmp_path)

    assert result.returncode != 0, (
        "AgentRunner accepted a run() with no turn_id seat - the pre-widening arity. "
        "A port that accepts anything is not a contract."
    )
    assert "Incompatible types in assignment" in result.stdout, (
        "Expected the assignment to AgentRunner to be the rejected expression.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F1")
@pytest.mark.parametrize("name", ["run", "resume"])
def test_both_members_are_coroutines(name: str) -> None:
    """D13: the two longest awaits in the system are not allowed to be sync `def`."""
    member = getattr(AgentRunner, name)

    assert inspect.iscoroutinefunction(member), f"AgentRunner.{name} must be async (D13)"
