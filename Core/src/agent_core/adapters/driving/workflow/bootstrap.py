"""Driving adapter: start the durable runtime. The ONE place `dbos` may be launched.

Phase:   F2 (durability)
Tasks:   docs/TASKS.md#t-f2-13
Status:  IMPLEMENTED - `launch_dbos` takes the composition root's configuration and has
         nowhere to be handed another one.

WHY THIS FILE EXISTS, AND WHY IT IS HERE AND NOT IN `main.py`
    `t-f2-12` built `dbos_config()` at the composition root and nothing consumed it:
    nothing in `Core/src` launched DBOS at all. `main.py` cannot be the launcher - `dbos`
    is a banned import outside this package (Core/pyproject.toml, flake8-tidy-imports; the
    mechanical form of CLAUDE.md non-negotiable #1) and that module's own docstring says
    so. So a process started by `serve()` answered on every route and every `POST /turns`
    failed when it reached the queue.

    The launch belongs beside the workflow it launches, which is this package, and this is
    the only module in it that performs the launch.

WHAT IS ACTUALLY BEING DEFENDED: THE CONFIG'S ORIGIN, NOT THE STARTUP
    The obvious signature here is `launch_dbos(config)`, and it is the defect. DBOS
    recovers a PENDING workflow only when the row's `application_version` matches the one
    the recovering process claims; left unset, that version is an md5 of the registered
    workflow sources, so every deploy is a new version. `agent-core-turns` is partitioned
    with `partition_concurrency=1`, and the unrecovered row a killed worker leaves holds
    the only slot that session has - forever. Later turns are accepted, enqueued and never
    dequeued. NOTHING RAISES; the whole symptom is a customer saying the assistant stopped
    answering. `composition.dbos_config` carries the full evidence.

    So a launcher that assembles its own `{"name": ..., "database_url": ...}` beside the
    composition root silently restores that deadlock. The defence is structural rather
    than documentary: this function takes the wired `Container` and READS the config off
    it. There is no parameter a second opinion fits in, and
    `tests/integration/test_workflow_bootstrap.py` asserts that there is not.

THE PRICE OF THE PIN, CARRIED FORWARD TO WHOEVER LAUNCHES
    A pinned `application_version` removes the check DBOS uses to decide whether a
    recovered workflow's code is the code that wrote it. An incompatible deploy WILL
    recover an in-flight workflow into changed code, and a workflow replaying against a
    different step sequence is exactly the crash-recovery bug CLAUDE.md's silent-bug table
    warns about - visible only on recovery, never in a green suite.

    The rule that price buys is a human one and cannot be automated away: **bump
    `PINNED_DBOS_APPLICATION_VERSION` deliberately whenever a change to
    `run_turn_workflow` is incompatible with the turns already in flight** - a step added,
    removed or reordered, or a changed workflow argument. Editing a step's INSIDES is not
    a bump; changing the body's decision sequence is. Read `composition.dbos_config`
    before touching it.

IMPORTING THIS MODULE IS PART OF THE LAUNCH, NOT AN INCIDENTAL DETAIL
    `@DBOS.workflow()`, `@DBOS.step()` and `Queue(...)` all register at import time, so
    the workflow module has to be imported before `DBOS.launch()` runs or the engine comes
    up knowing about no workflows and no queues. `turn_workflow` is therefore imported at
    module scope here, deliberately and not merely for the name it provides.

THE LAUNCH ITSELF IS INJECTED, FOR THE SAME REASON `pool_factory` IS
    A DBOS runtime is a process-global singleton with threads and a system database behind
    it; a test that started one would leave it running for every later test in the
    process. `launch` defaults to the real thing, so the production path and the asserted
    path differ only in that one object.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from dbos import DBOS, DBOSConfig

from agent_core.adapters.driving.workflow import turn_workflow as turn_workflow
from agent_core.composition import Container, DbosConfig, dbos_config

__all__ = ["DbosLaunch", "launch_dbos"]

# What actually brings the engine up, given a configuration somebody else owns. A named
# alias rather than an inline signature, because the whole point of this module is that
# the thing on the left of the arrow never constructs the thing on the right.
DbosLaunch = Callable[[DbosConfig], None]


def _launch_dbos_runtime(config: DbosConfig) -> None:
    """Construct the DBOS singleton and start it. The only I/O in this module.

    `cast` rather than a conversion: `DbosConfig` is `dict[str, str]` because
    `composition.py` may not name `dbos.DBOSConfig` (the import ban), and every key it
    sets is one `DBOSConfig` declares - so the value that crosses is already the right
    shape and only the type name was missing.
    """
    DBOS(config=cast(DBOSConfig, config))
    DBOS.launch()


def launch_dbos(container: Container, *, launch: DbosLaunch = _launch_dbos_runtime) -> None:
    """Start the durable engine for THIS container, on the config the container was built from.

    Call it once, after `start_container()` and before anything enqueues a turn. It is
    idempotent in neither direction: DBOS is a process singleton and launching twice is a
    launcher's bug, not something this function hides.

    THE ARGUMENT IS A `Container` AND NOT A CONFIG, AND THAT IS THE ANCHOR
        The configuration comes from `dbos_config(container.settings)` - the same
        `Settings` every pool, adapter and profile in that container was wired from. A
        launcher cannot hand in a version of its own because there is no parameter for
        one; see WHAT IS ACTUALLY BEING DEFENDED above for what happens when it can.

    IT DOES NOT WIRE THE WORKFLOW. `build_container` already did, through
    `bind_turn_workflow`, and it has to: the dependencies must be bound before a recovered
    workflow's first step runs, and recovery begins inside `DBOS.launch()`.
    """
    launch(dbos_config(container.settings))
