"""Starting the process: a durable runtime, and a turn that can actually be resumed.

Phase:   F2 (durability) / F3 (the human wait)
Tasks:   docs/TASKS.md#t-f2-13, docs/TASKS.md#t-f3-20
Covers:  agent_core/composition.py (bind_turn_workflow, the `resume_turn` seat),
         adapters/driving/workflow/bootstrap.py (the one place `dbos` may be launched)

TWO HOLES OF THE SAME SHAPE, WHICH IS WHY THEY ARE ASSERTED IN ONE FILE
    Both are a collaborator a TEST supplies and production never binds - the shape this
    build has now hit five times (docs/STATE.md, "the recurring process defect").

    t-f3-20. `_step_resume` resolves `ResumeTurn` off the workflow's bound dependencies
    and the container never built one, so every process that suspended on a human and got
    an answer died on `TurnWorkflowNotWiredError` - naming the fix, the same way
    `human_gateway` did one wave earlier. Every test of the step passed, because every one
    of them bound the use case itself.

    t-f2-13. Nothing in `Core/src` launched DBOS at all, so `dbos_config()` - the pinned
    `application_version` that stops a killed worker from silencing a session forever
    (docs/TASKS.md#t-f2-12) - was owned and unconsumed.

WHY THE SECOND TEST IS ABOUT A SEAM THAT MUST NOT EXIST, NOT ABOUT STARTING
    A test that `launch_dbos` starts DBOS proves the least interesting half. The defect
    t-f2-12 describes comes back the moment a launcher assembles its own `{"name":
    ..., "database_url": ...}` beside the composition root: DBOS then computes the version
    as an md5 of the registered workflow sources, every deploy is a new version, and the
    PENDING row a dying worker left on a `partition_concurrency=1` partition is never
    recovered. Nothing raises. The session simply stops answering.

    So what is asserted is that the launch takes the config FROM the composition root and
    has nowhere to be handed another one - the config value is `dbos_config(...)` key for
    key, and the function exposes no parameter through which a caller could substitute a
    version of its own.

NO DATABASE, NO NETWORK, NO MODEL. `build_container` connects to nothing by design, and
the DBOS launch itself is injected, so nothing here starts a durable runtime inside the
test process - one would then outlive the test and register its queues globally.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from types import ModuleType
from typing import Any

import pytest

from agent_core import composition
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.resume_turn import ResumeTurn, UnknownProfileError
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnRequest,
    UserInput,
)

_TENANT = TenantId("acme")
_TURN = TurnId("11111111-1111-4111-8111-111111111111")
_CALL = ToolCallId("pyd_ai_call_a")

# A profile id no `Core/profiles/*.yaml` can define. `ResumeTurn` resolves the profile
# BEFORE any write, any media lookup and any model call, so an unknown id is how a test
# proves the real use case was reached without the use case reaching anything.
_NO_SUCH_PROFILE = "no-such-profile-t-f3-20"


def _bootstrap() -> ModuleType:
    """The workflow package's launch module, with a readable failure when it is absent.

    Deliberately not a top-level import: that turns "not implemented yet" into a
    collection error, and a collection error is not a red test - it says the file failed
    to load, not that the behaviour is missing (the reason `test_composition.py` reaches
    for `composition` the same way).
    """
    try:
        return importlib.import_module("agent_core.adapters.driving.workflow.bootstrap")
    except ModuleNotFoundError as absent:  # pragma: no cover - the assertion is the report
        pytest.fail(
            "agent_core.adapters.driving.workflow.bootstrap does not exist, so nothing in "
            "Core/src launches DBOS and dbos_config()'s pinned application_version is "
            f"owned and unconsumed (docs/TASKS.md#t-f2-13). Import said: {absent}"
        )


def _suspended_turn(profile_id: str) -> TurnRequest:
    """The request the suspended turn started from. `_step_resume` reads its `caller`."""
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s-1"), tenant_id=_TENANT),
        caller=CallerIdentity(subject_id="u-1", channel="http", tenant_id=_TENANT),
        profile_id=profile_id,
        input=UserInput(text="Freeze the account behind order 4417."),
    )


@pytest.mark.phase("F3")
def test_the_container_builds_a_resume_turn_and_binds_it_to_the_workflow() -> None:
    """t-f3-20. The workflow gets the container's OWN use case, never a second one.

    `_step_resume` reads exactly this field. Unbound, it is `None` by deliberate design -
    there is no correct silent behaviour for "a human answered and nothing applied it" -
    so the seat being empty is a process that cannot resume a turn at all.
    """
    container = composition.build_container(composition.Settings())

    resume_turn = getattr(container, "resume_turn", None)
    assert isinstance(resume_turn, ResumeTurn), (
        "build_container never constructs a ResumeTurn, so there is nothing for "
        "bind_turn_workflow to hand the workflow (docs/TASKS.md#t-f3-20)."
    )

    bound = turn_workflow._dependencies
    assert bound is not None, "bind_turn_workflow never called bind_dependencies()"
    assert bound.resume_turn is resume_turn, (
        "bind_turn_workflow builds TurnWorkflowDependencies without resume_turn, so a "
        "turn that suspends on a human and IS answered dies in _step_resume with "
        "TurnWorkflowNotWiredError (docs/TASKS.md#t-f3-20)."
    )


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_a_production_wired_process_reaches_the_real_resume_use_case() -> None:
    """The behavioural half: the answer actually gets applied, not refused at the seat.

    Field identity alone would still pass if `_step_resume` resolved some other seat. So
    the step is driven, and what is asserted is WHERE it fails: an unknown profile means
    control reached `ResumeTurn.execute` and was refused by the use case's own validation,
    which no unwired process can get to - it raises `TurnWorkflowNotWiredError` first.
    """
    composition.build_container(composition.Settings())
    request = _suspended_turn(_NO_SUCH_PROFILE)
    answer = turn_workflow.HumanAnswer(
        turn_id=_TURN, tool_call_id=_CALL, approved=True, note=None
    )

    with pytest.raises(Exception) as refused:  # noqa: B017 - WHICH exception IS the assertion
        asyncio.run(turn_workflow._step_resume(_TURN, request, answer))

    assert not isinstance(refused.value, turn_workflow.TurnWorkflowNotWiredError), (
        "the human's decision was dropped at the wiring seat: production binds no "
        "ResumeTurn, so the turn expires as though nobody had replied "
        f"(docs/TASKS.md#t-f3-20). It said: {refused.value}"
    )
    assert isinstance(refused.value, UnknownProfileError), (
        "_step_resume did not reach ResumeTurn.execute - the bound seat is not the "
        f"container's use case. It raised {type(refused.value).__name__}: {refused.value}"
    )


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_the_launch_is_configured_from_the_composition_root_s_pinned_config() -> None:
    """t-f2-13. The value DBOS is started with is `dbos_config()`'s, key for key.

    Not "a config containing the right version": equality of the whole mapping is what
    catches a launch site that took the composition root's dict and then edited it.
    """
    bootstrap = _bootstrap()
    container = composition.build_container(composition.Settings())
    launched: list[Any] = []

    bootstrap.launch_dbos(container, launch=launched.append)

    assert launched, "launch_dbos never started anything"
    assert launched[0] == composition.dbos_config(container.settings), (
        "the durable runtime was launched with a configuration that is not the "
        "composition root's. A second dict assembled at the launch site is exactly how "
        "the t-f2-12 deadlock returns, and its only symptom is silence."
    )
    assert launched[0]["application_version"] == composition.PINNED_DBOS_APPLICATION_VERSION, (
        "application_version is not the pinned source constant, so DBOS hashes the "
        "registered workflow sources instead: every deploy is a new version, and the "
        "PENDING row a killed worker left holds its session's only partition slot "
        "forever (docs/TASKS.md#t-f2-12)."
    )


@pytest.mark.phase("F2")
@pytest.mark.silent
def test_the_launch_site_has_no_seam_through_which_to_supply_its_own_version() -> None:
    """The assertion worth more than one proving it starts.

    A `config` parameter - however well documented - is a launcher's invitation to pass
    its own two-key dict, and the resulting deadlock is invisible. The only parameters
    this function may take are the wired container it reads the config OFF, and the
    injected launch itself, which receives that config rather than producing one.
    """
    bootstrap = _bootstrap()

    parameters = set(inspect.signature(bootstrap.launch_dbos).parameters)
    assert parameters == {"container", "launch"}, (
        "launch_dbos exposes a parameter other than the container and the injected "
        f"launch: {sorted(parameters)}. The DBOS configuration - and above all its "
        "application_version - is the composition root's, and a launch site that can be "
        "handed another one is the t-f2-12 defect with an extra step."
    )

    container = composition.build_container(composition.Settings())
    with pytest.raises(TypeError):
        bootstrap.launch_dbos(
            container,
            launch=lambda _config: None,
            config={"name": "agent-core", "database_url": "postgresql://localhost/x"},
        )
