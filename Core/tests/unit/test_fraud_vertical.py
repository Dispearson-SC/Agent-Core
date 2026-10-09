"""The fraud vertical - the REAL proof the core did not change.

Phase:   after F4 - the second vertical, deliberately unlike the first
Tasks:   docs/TASKS.md#t-later-02
Covers:  adapters/driven/tools/fraud/tools.py, Core/profiles/fraud_analyst.yaml

WHY A SECOND VERTICAL PROVES SOMETHING THE FIRST COULD NOT
    `delivery` was written alongside the core (docs/DECISIONS.md#d11), so its own success
    proves only that the core can serve the one agent it was shaped around.
    `test_contract.py` locks the sentence at the top of CLAUDE.md mechanically, but a
    mechanical lock that has only ever been tripped by one tree is still unproven against a
    second one. This file is that second tree: a fraud analyst, nothing like a delivery
    dispatcher, built without touching a single line the delivery vertical needed.

WHAT "END TO END" MEANS HERE
    Not a dict inspection of `FunctionToolset.tools` - the same reasoning
    `test_tool_provider.py` gives for reading the model's own advertised tool list rather
    than reaching into the toolset object: a provider could compose the right objects and
    still advertise the wrong names to the model. So the assertion drives a real
    `pydantic_ai.Agent` and reads what it was actually offered, then calls each tool
    directly to prove the functions behind those names really run.

WHY THIS DOES NOT REGISTER "fraud" IN `provider.py`'s `DEFAULT_TOOL_PACKAGES`
    This anchor's write set is `adapters/driven/tools/fraud/tools.py`, this test file, and
    one profile YAML - `provider.py` is deliberately not in it (fourteen other agents are
    editing this repository's other files concurrently). `LocalToolProvider` already has the
    seat for exactly this case: `build_tool_provider(packages=...)`, documented on that
    module as "the override exists for an embedding host [that] wants to register their own
    handful of tools, not inherit ours" - which is precisely what a test proving a SECOND,
    independently-registerable vertical needs. Wiring the production registry line is
    barrier-owned reconciliation, not this anchor's write.

WHY THE ZERO-CORE-CHANGE CHECK IS ALSO INLINE, NOT ONLY IN `test_contract.py`
    That file already runs a full, general scan (docstring-excluded, string literals
    included) over every vertical the tree contains. This one is narrower on purpose - it
    checks identifiers and imports for exactly the fraud vocabulary - because the point
    here is not to re-prove the general mechanism; it is that THIS anchor's own diff, read
    on its own, touched none of domain/, application/ or ports/. Two independent checks
    finding the same zero is stronger evidence than one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset

from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.adapters.driven.tools import provider as tool_provider
from agent_core.adapters.driven.tools.fraud import tools as fraud_tools
from agent_core.domain.profile import AgentProfile

pytestmark = [pytest.mark.phase("later"), pytest.mark.contract]

_CORE_LAYERS = ("domain", "application", "ports")
_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_core"
_PROFILES_ROOT = Path(__file__).resolve().parents[2] / "profiles"
_PROFILE_ID = "fraud_analyst"

EXPECTED_TOOL_NAMES = frozenset(
    {"sql_readonly", "account_history", "case_notes_append", "freeze_account"}
)

# Every token that names this vertical and nothing else - the same "vocabulary" idea
# test_contract.py uses, sized for one vertical instead of every vertical in the tree.
_FRAUD_VOCABULARY = frozenset({_PROFILE_ID, *EXPECTED_TOOL_NAMES})


def _tool_names(toolset: object) -> frozenset[str]:
    return frozenset(toolset.tools.keys())  # type: ignore[attr-defined]


def _profile(*toolsets: str) -> AgentProfile:
    return AgentProfile(
        id=_PROFILE_ID,
        persona="You review flagged transactions for fraud signals.",
        model="minimax/MiniMax-M3",
        toolsets=toolsets,
    )


def _fraud_only_provider() -> tool_provider.LocalToolProvider:
    """A provider registering ONLY the fraud package, through the public override seat.

    See the module docstring's "WHY THIS DOES NOT REGISTER" section. The `hasattr` check
    turns "the vertical is not implemented yet" into a normal assertion failure here,
    rather than an `AttributeError` escaping from the middle of a test body.
    """
    assert hasattr(fraud_tools, "build_toolset"), (
        "agent_core.adapters.driven.tools.fraud.tools has no build_toolset yet - "
        "docs/TASKS.md#t-later-02 is not implemented"
    )
    return tool_provider.build_tool_provider({"fraud": fraud_tools.build_toolset})


async def _advertised_tool_names(toolset: AbstractToolset[None]) -> frozenset[str]:
    """The tool names a model is actually offered - `test_tool_provider.py`'s own method."""
    model = TestModel(call_tools=[])
    await Agent(model, toolsets=[toolset]).run("hello")
    parameters = model.last_model_request_parameters
    assert parameters is not None, "the run made no model request; nothing was advertised"
    return frozenset(definition.name for definition in parameters.function_tools)


def _core_references_to_fraud() -> list[str]:
    """Every place a domain/application/ports module names the fraud vertical, or none."""
    offences: list[str] = []
    for layer in _CORE_LAYERS:
        root = _PACKAGE_ROOT / layer
        for module_path in sorted(root.rglob("*.py")):
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "tools.fraud" in alias.name:
                            offences.append(
                                f"{module_path}:{node.lineno} imports {alias.name!r}"
                            )
                    continue
                if isinstance(node, ast.ImportFrom):
                    if node.module and "tools.fraud" in node.module:
                        offences.append(f"{module_path}:{node.lineno} imports {node.module!r}")
                    continue

                name: str | None = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.arg):
                    name = node.arg
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    name = node.name
                if name is not None and name in _FRAUD_VOCABULARY:
                    lineno = getattr(node, "lineno", "?")
                    offences.append(f"{module_path}:{lineno} names {name!r}")
    return offences


@pytest.mark.asyncio
async def test_the_fraud_vertical_works_end_to_end_with_zero_core_changes() -> None:
    """docs/TASKS.md#t-later-02: THE proof of the contract sentence at the top of CLAUDE.md.

    Two halves, both required - see the module docstring:
      1. The vertical works end to end: it resolves through the same `LocalToolProvider`
         the delivery vertical resolves through, and a real `pydantic_ai.Agent` is actually
         offered exactly its four tools.
      2. `domain/`, `application/` and `ports/` name none of it.
    If either half fails, the port cut is wrong - CLAUDE.md, "The contract to defend".
    """
    provider = _fraud_only_provider()
    profile = _profile("fraud")

    names = await provider.tool_names_for(profile)
    assert frozenset(names) == EXPECTED_TOOL_NAMES

    toolset = await provider.toolset_for(profile)
    assert isinstance(toolset, AbstractToolset), (
        "PydanticAgentRunner._toolsets_for rejects anything that is not an AbstractToolset"
    )

    advertised = await _advertised_tool_names(toolset)
    assert advertised == EXPECTED_TOOL_NAMES, (
        f"the model was offered {advertised!r}, not exactly the four fraud tools - "
        "no auto-discovery means the advertised set is exactly the registered names"
    )

    offences = _core_references_to_fraud()
    assert not offences, (
        "the fraud vertical leaked into the core - adding a vertical must cost one profile "
        "file plus one tools package, zero changes elsewhere:\n  " + "\n  ".join(offences)
    )


def test_fraud_tools_actually_execute_not_just_advertise() -> None:
    """Advertising a name is not the same as the function behind it working.

    Each call is real, deterministic behaviour against the vertical's own fixture data -
    the same "stand-in, named as one" pattern delivery's `_ORDERS` uses - never a stub that
    merely returns its input.
    """
    assert hasattr(fraud_tools, "build_toolset"), (
        "agent_core.adapters.driven.tools.fraud.tools has no build_toolset yet - "
        "docs/TASKS.md#t-later-02 is not implemented"
    )

    history = fraud_tools.account_history("acc-1")
    assert history["account_id"] == "acc-1"

    read = fraud_tools.sql_readonly("SELECT * FROM transactions WHERE account_id = 'acc-1'")
    assert "row_count" in read

    with pytest.raises(ValueError):
        fraud_tools.sql_readonly("DELETE FROM transactions")

    note = fraud_tools.case_notes_append("case-1", "flagged for velocity")
    assert note["case_id"] == "case-1"

    frozen = fraud_tools.freeze_account("acc-1")
    assert frozen["frozen"] == "True"

    after = fraud_tools.account_history("acc-1")
    assert after["frozen"] == "True", "freeze_account did not actually change the account"


def test_a_fraud_profile_selects_exactly_the_fraud_toolset() -> None:
    """`Core/profiles/fraud_analyst.yaml` exists and names exactly the `fraud` toolset -
    the other half of "one profile file plus one tools package", at this vertical's own
    granularity (test_contract.py's `test_a_profile_selects_exactly_one_tools_package`
    proves the same thing across every profile in the tree; this pins THIS one)."""
    path = _PROFILES_ROOT / "fraud_analyst.yaml"
    assert path.is_file(), f"{path} does not exist yet - docs/TASKS.md#t-later-02"

    profile = load_profile_sync(path)

    assert profile.id == _PROFILE_ID
    assert profile.toolsets == ("fraud",)
