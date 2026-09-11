"""The shipped process hands the console and the runner every collaborator they need.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-33, docs/TASKS.md#t-f11-34
Subject: Core/src/agent_core/main.py, Core/src/agent_core/composition.py

WHY THIS FILE IS NOT ANOTHER `test_console.py`

    `tests/unit/test_console.py` is green and always was, because it constructs the
    `Console` itself and passes a `profiles_dir`, a `load_profiles`, an `approvals` and a
    `transcripts` by hand. The peer tests are green for the identical reason: they build a
    mailbox. **A fixture that supplies the missing piece is exactly what stops anyone
    noticing it is missing** - docs/TASKS.md records that sentence seven times now, and
    these are the seventh and eighth.

    So nothing here constructs a collaborator. Everything is asked of the objects
    `build_container()` and `build_console()` actually return, which is the pair the
    shipped process runs. That is the only difference between this file and the unit
    tests, and it is the whole difference.

NO DATABASE AND NO MODEL, DELIBERATELY

    `build_container` connects to nothing (composition.py, NOTHING HERE CONNECTS) and
    `build_console` performs no I/O, so the wiring is a question that can be asked on a
    laptop with nothing running. The commands driven below are driven for their WIRING and
    not for their result: `:sessions` reaching a closed pool prints an error, and an error
    is a wired seat failing. "This console has no ... wired" is the unwired one, and it is
    the only string asserted on.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.composition import Settings, build_container
from agent_core.main import build_console
from agent_core.ports.agent_mailbox import AgentMailbox

# The exact sentence every unwired console seat answers with
# (adapters/driving/cli/console.py). Read from the console's own refusals rather than
# restated loosely, so a reworded refusal cannot make this file pass by missing it.
_UNWIRED = "this console has no"

# The shipped relationship, as configuration: docs/TASKS.md#t-f11-15. `support_triage`
# names `billing_specialist` in its `peers:` block, with the two-sided allowlist and the
# hop limit, and nothing else in the repository connects the two.
_ASKING_AGENT = "support_triage"
_PEER_TOOL = "ask_peer"


def _port_members(port: type) -> frozenset[str]:
    """The names a Protocol itself declares, without `object`/`Protocol` noise."""
    declared = getattr(port, "__protocol_attrs__", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(name for name in vars(port) if not name.startswith("_"))


def _shipped_settings(tmp_path: Path) -> Settings:
    """The real profiles, copied so `:new` scaffolds into a temporary directory.

    `:new` WRITES A FILE. Pointed at `Core/profiles`, this test would leave a scaffolded
    profile in the repository and the second run would behave differently from the first.
    The copy keeps the shipped profiles - `support_triage` and its peer among them - which
    is what makes this a test of the deployment rather than of a fixture.
    """
    profiles_dir = tmp_path / "profiles"
    shutil.copytree(Settings().profiles_dir, profiles_dir)
    return replace(Settings.from_env(), profiles_dir=profiles_dir)


def _console_transcript(
    settings: Settings, script: Sequence[str]
) -> tuple[str, ...]:
    """Drive `build_console`'s console over a scripted session; return what it wrote.

    The console is built by `main.build_console` from a container built by
    `composition.build_container` - no seat is passed in here, because a seat passed in
    here is the defect this file exists to catch.
    """
    container = build_container(settings)
    written: list[str] = []
    remaining = list(script)

    def read_line(_prompt: str) -> str | None:
        return remaining.pop(0) if remaining else None

    console = build_console(
        container, write_line=written.append, read_line=read_line
    )
    asyncio.run(console.run())
    return tuple(written)


@pytest.mark.parametrize(
    ("command", "seat"),
    [
        (":new scratch_agent", "profiles_dir"),
        (":reload", "load_profiles"),
        (":approve nope", "approvals"),
        (":refuse nope", "approvals"),
        (":sessions", "transcripts"),
        (":trace", "transcripts"),
    ],
)
def test_the_shipped_console_binds_the_seat_each_command_needs(
    tmp_path: Path, command: str, seat: str
) -> None:
    """Six commands, four seats, one cause: `main.build_console`'s constructor call.

    Parametrised rather than written as one assertion over all six, because a seat left
    out names itself here - the failure says WHICH command went unwired instead of
    printing a list and leaving the reader to work out which constructor argument is
    missing.
    """
    lines = _console_transcript(_shipped_settings(tmp_path), [command])

    unwired = [line for line in lines if _UNWIRED in line]
    assert unwired == [], (
        f"{command!r} answered with an unwired seat, so `main.build_console` never "
        f"passed {seat!r}. The console surface is finished (docs/TASKS.md#t-f11-09 .. "
        "#t-f11-13); the process that ships never hands it the collaborator:\n"
        + "\n".join(unwired)
    )


def test_the_shipped_container_offers_the_peer_tool_to_an_agent_that_names_a_peer() -> (
    None
):
    """A profile that names a peer resolves to `ask_peer`. docs/TASKS.md#t-f11-34.

    THE ASSERTION IS ON `tool_names_for`, WHICH IS THE QUESTION A TURN ASKS.
        `StartTurn` step 3 narrows `ToolProvider.tool_names_for(profile)` through
        `ToolPolicy` and offers the survivors to the model, and the console's `:tools`
        renders the same call. A tool absent from that tuple is a tool the model is never
        told exists, so the two-sided allowlist, the hop limit and the durable mailbox
        (docs/TASKS.md#t-f9-03 .. #t-f9-09) are unreachable from configuration however
        completely they are implemented.
    """
    container = build_container()
    profile = container.profiles[_ASKING_AGENT]

    assert profile.peers.enabled, (
        f"{_ASKING_AGENT}.yaml no longer declares peers, so this asserts nothing. That "
        "is docs/TASKS.md#t-f11-15 regressing rather than the orchestration gap."
    )

    names = asyncio.run(container.tools.tool_names_for(profile))
    assert _PEER_TOOL in names, (
        f"{_ASKING_AGENT} declares a peer and resolves to {names!r} - no {_PEER_TOOL!r}. "
        "One agent orchestrating another is meant to be a YAML change, and the mechanism "
        "is reachable only from a unit test that builds its own provider."
    )


def test_the_shipped_container_holds_the_mailbox_a_peer_ask_travels_on() -> None:
    """`Container` has an `AgentMailbox` seat, and it is the port's shape.

    Asserted against the PORT'S OWN declared members, never against `PgAgentMailbox`:
    t-f9-09's whole point is that the transport is a choice this container makes and
    nothing above it names. A seat asserted on the adapter would put
    `adapters/driven/peers/mailbox_a2a.py` (docs/TASKS.md#t-d2-04) out of reach of the
    swap it exists to be.

    The member list is READ FROM `AgentMailbox` rather than restated, so a member added to
    the port fails here instead of silently going unwired - which is the shape of gap this
    whole file exists for. `isinstance` is not available: the port is not
    `runtime_checkable`, and `ports/` is not this anchor's to change.
    """
    container = build_container()

    mailbox = getattr(container, "mailbox", None)
    assert mailbox is not None, (
        "nothing hands the container an AgentMailbox, so every peer ask in the tree has "
        "no queue to leave on."
    )

    declared = sorted(_port_members(AgentMailbox))
    assert declared, (
        "ports/agent_mailbox.py declares no members, so this assertion would pass against "
        "anything at all."
    )
    missing = [name for name in declared if not callable(getattr(mailbox, name, None))]
    assert missing == [], (
        f"Container.mailbox is a {type(mailbox).__name__} and does not answer "
        f"{missing} - it does not satisfy ports/agent_mailbox.py."
    )
