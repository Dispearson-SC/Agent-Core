"""Driving adapter: the operator console - a REPL for making, using and judging an agent.

Phase:   F0 (the surface) / F11 (the surface FINISHED)
Tasks:   docs/TASKS.md#t-f0-07, #t-f11-09, #t-f11-10, #t-f11-11, #t-f11-12, #t-f11-13
Status:  IMPLEMENTED - navigation, inspection, turns, scaffolding, reload, the approval
         path, whole tool calls, session resumption and the two-audience trace.

WHAT IT IS FOR
    Move between agents, read the configuration the system RESOLVED for each one, see the
    toolset it actually gets, talk to it - and watch how it uses those tools. The last one
    is the point; the rest is navigation.

IT IS A DRIVING ADAPTER, NOT A SECOND COMPOSITION ROOT
    It lives beside `http/`, `channels/`, `workflow/` and `scheduler/` and it calls use
    cases through collaborators handed to it. Every seat below is a port protocol, a use
    case narrowed to the one member this needs, or a domain type, and `main.py` fills them
    from the ONE container `composition.py` built. A console that assembled its own policy
    engine or its own audit sink would be inspecting a system nobody deploys.

    THE ONE CONCRETE IMPORT IS THREE RENDERING CONSTANTS, AND IT IS DELIBERATE.
    `UNTRUSTED_OPEN`, `UNTRUSTED_CLOSE` and `is_untrusted` come from the runner that
    writes the fence. `t-f11-11` asks this console to render untrusted-content wrapping AS
    wrapping so an operator can SEE the boundary defending non-negotiables #4 and #10 -
    and a console holding its OWN copy of the delimiter would keep drawing a fence after
    the runner changed its own, which is precisely the "do not duplicate a fact that can
    drift" rule in CLAUDE.md. Drawing the wrong fence in a silent-bug area is worse than
    an adapter-to-adapter import for three module constants.

TWO MODES, AND WHICH ONE IS ON IS VISIBLE ON EVERY LINE (t-f11-51)
    DIRECT is the default and it calls `StartTurn` in-process. A REPL wants an answer now;
    the 202-plus-poll and the durable per-session partitioned queue exist for channels, and
    going through them would mean typing a sentence and then polling for it. So that path
    skips them, which means two guarantees are NOT on:

      - durability: a crash mid-turn is not recovered and not replayed (t-f2-02)
      - coalescing: the per-session partition and its window are never exercised

    It also means `HumanGateway.publish` never runs for a turn started that way, so such a
    suspension has NO correlation handle: `:pending` says that rather than printing a queue
    an operator cannot act on.

    DURABLE enqueues the turn onto the DBOS queue and polls the stored row exactly as
    `GET /turns/{turn_id}` does. It exists because an entire mechanism was unreachable
    without it: `_step_ask_peers` lives in the workflow body, so an `ask_peer` typed in
    direct mode suspends the turn and NO MAILBOX IS EVER ASKED. Ten times in this build a
    mechanism worked and nothing a human could type reached it; this is the door for that
    one. Coalescing is STILL not exercised - the per-session window belongs to
    `enqueue_turn_window`, which `POST /turns` opens and this does not.

    DELETING THE DIRECT PATH TO GAIN THE DURABLE ONE WOULD BE A BAD TRADE. Answering
    immediately is why a REPL is worth sitting at, and the operator can have both.

    `:mode` switches and, with no argument, answers which one is active. The banner prints
    the active mode's notice at startup and every switch reprints it, and the PROMPT
    carries the mode on every line - because a mode that is legible once is a mode an
    operator forgets. A tool that silently exercises less than the real path is how
    somebody concludes the system works when they have not tested the part that breaks.

WHAT IT DOES NOT SKIP, WHICH IS EVERYTHING THAT DECIDES A REFUSAL
    The turn runs through the real `StartTurn`: the real `AgentRunner` with its
    `PolicyEnforcement` hook, the real `ToolPolicy`, the real `AuditSink`, the real
    profile. Seeing a tool refused, and which `rule_id` refused it, IS the feature. Any
    shortcut there would make the console a liar about the one subsystem CLAUDE.md's
    silent-bug table says never fails a test.

THE AUDIENCE IS ADMIN, DELIBERATELY, AND THE BANNER SAYS THAT TOO
    CLAUDE.md non-negotiable #11 governs what a USER sees: THAT something is pending,
    never WHICH tool. This is an operator surface, so a suspension here is rendered with
    its kind, its tool name and its `tool_call_id` - which is exactly the information
    needed to go and answer it. That is a different audience, not a relaxed rule, so the
    view names itself rather than leaving a reader to assume it is the user's view.

    `:trace user` is how an operator checks that claim instead of trusting it: the same
    turn, projected for the other audience, through the same `TranscriptReader` a customer
    would be served from. Both halves of #11 pull against each other and only a human
    looking at both projections can see whether the projection got them right.

IDENTITY: NOTHING HERE IS AN ADMINISTRATOR, AND THE ONE ADMIN SEAT IS NOT THE CALLER'S
    The console builds a `CallerIdentity` from flags, and that is what the AGENT acts
    under. `AuditReader` takes an `AdminIdentity` because tool names, arguments and policy
    verdicts are admin-audience facts - so the console holds one, handed to it by the
    process, and non-negotiable #9 holds exactly because the two are different types that
    no code path here converts between. There is no parameter a `CallerIdentity` could
    become an `AdminIdentity` through, and no knowledge-write, admin route or admin
    anything is reachable from this REPL.

CREDENTIALS ARE NEVER PRINTED
    `:profile` renders an MCP server by name and transport and deliberately NOT by url or
    command line. An MCP url is exactly where a token lives in real configuration, and a
    console is a thing people run over a shoulder and paste into tickets.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Final, Protocol

from agent_core.adapters.driven.agent_pydantic.runner import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    is_untrusted,
)
from agent_core.application.decide_approval import FourEyesError, UnknownCorrelationError
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.profile import AgentProfile
from agent_core.domain.transcript import (
    VISIBILITY,
    Audience,
    EntryKind,
    TranscriptEntry,
)
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    UserInput,
)
from agent_core.ports.audit_reader import AuditedToolCall, AuditReader
from agent_core.ports.knowledge_admin import AdminIdentity
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.tool_provider import ToolProvider
from agent_core.ports.transcript_reader import TranscriptReader

__all__ = [
    "DURABLE",
    "DIRECT",
    "PROFILE_SCAFFOLD",
    "ApprovalDecider",
    "Console",
    "DurableEnqueue",
    "DurablePoll",
    "DurableUnwiredError",
    "ProfileLoader",
    "TurnRunner",
    "TurnSnapshot",
    "new_turn_id",
]

# The two modes, by the name an operator types and the name the prompt carries.
DIRECT: Final[str] = "direct"
DURABLE: Final[str] = "durable"


class TurnRunner(Protocol):
    """What the console needs from `StartTurn`, and nothing else.

    Narrowed on purpose: a console that held the whole use case could reach its store or
    its audit sink and start answering questions from the side rather than from the turn.
    """

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome: ...


class ApprovalDecider(Protocol):
    """What the console needs from `DecideApproval`, and nothing else. t-f11-10.

    NARROWED FOR THE SAME REASON `TurnRunner` IS, NOT BECAUSE A PORT IS MISSING.
        `DecideApproval` is a USE CASE, not a port - `adapters/driving/http/routes.py`
        narrows it the same way through `TurnStarter`. This is not the mistake
        `ports/audit_reader.py` was written to end: that one was a driving adapter
        declaring the protocol of a DRIVEN collaborator, which is the definition of a
        missing port. Narrowing an application-layer entry point is the opposite - it
        keeps the console from reaching the gateway or the sink behind it.

    Every exception this can raise is part of the contract, and the console catches them
    by type: `FourEyesError` (D25) and `UnknownCorrelationError`.
    """

    async def execute(
        self,
        correlation_id: str,
        subject_id: str,
        approved: bool,
        note: str | None = None,
    ) -> tuple[TurnId, ToolCallId]: ...


class TurnSnapshot(Protocol):
    """One turn as a POLL may see it, and nothing else. t-f11-51.

    NARROWED THE WAY `TurnRunner` AND `ApprovalDecider` ARE, AND FOR A SECOND REASON
        The shape the composition root binds here is `http/routes.py`'s `TurnView`, read
        back by `composition._pg_turn_lookup` from the same `turns` row `StartTurn` wrote.
        Declaring the two members this console reads - rather than importing that dataclass
        - keeps a CLI adapter from depending on an HTTP adapter's vocabulary, which is a
        coupling neither of them needs and neither of them could notice breaking.

    READ-ONLY MEMBERS ON PURPOSE. Spelt as properties so a FROZEN dataclass satisfies it;
    a plain attribute in a Protocol demands a mutable one, and nothing should be able to
    write a status back through this seam.
    """

    @property
    def status(self) -> str:
        """`running`, `waiting` or `finished` - the three states a poll can observe."""
        ...

    @property
    def text(self) -> str | None:
        """The answer, or `None` for anything that has not finished."""
        ...


# Putting one turn on the durable queue and getting back the id it will be filed under.
# t-f11-51.
#
# A CALLABLE RATHER THAN THE WORKFLOW HANDLE, WHICH IS THE WHOLE NARROWING. `enqueue_turn`
# answers with a `WorkflowHandleAsync`, and a console holding one could `get_result()` on it
# - which is a blocking wait on a durable workflow dressed up as a REPL, and it would make
# the poll below decorative. `routes.py` narrows the same handle to `TurnHandle` for the
# same reason. What comes back is the `TurnId`: the workflow's durable id IS the domain
# turn id (`turn_workflow._step_new_turn_id`), so it is also the key `:audit` and `:trace`
# read the turn back under.
DurableEnqueue = Callable[[TurnRequest], Awaitable[TurnId]]

# Reading that turn back, the way `GET /turns/{turn_id}` reads it.
#
# IT TAKES THE CALLER, and that is not decoration: the tenant predicate belongs INSIDE the
# query the adapter runs, so a turn id alone can never read another tenant's answer. The
# console passes its own caller, never one typed at a prompt - the same rule `:resume`
# follows for `SessionRef.tenant_id`.
DurablePoll = Callable[[TurnId, CallerIdentity], Awaitable[TurnSnapshot | None]]


class DurableUnwiredError(ValueError):
    """`durable=True` with no durable seats behind it.

    Raised at CONSTRUCTION rather than on the first turn, for the reason
    `ChannelRegistry` raises on a duplicate id at `__init__`: a console that started in a
    mode it cannot serve would print a banner promising durability and then fail at the
    enqueue, from the prompt, with the operator holding nothing.
    """


# Re-reading the profiles directory, as a callable rather than as a path. t-f11-09.
#
# The console must not know that a profile is a YAML file on a disk - `yaml` may only be
# imported under adapters/driven/profiles_fs/, and versions are assigned by
# `composition.load_profiles` (D20) rather than by whoever happens to read the directory.
# So the composition root binds `lambda: load_profiles(settings.profiles_dir)` and this
# file calls it. What comes back is the WHOLE directory or an exception; there is
# deliberately no per-file member, because a reload that applied some files and not others
# is the half-application `:reload` exists to refuse.
ProfileLoader = Callable[[], Mapping[str, AgentProfile]]


def new_turn_id() -> TurnId:
    """A fresh turn id.

    NOT the non-determinism CLAUDE.md #2 is about. That rule is about a DBOS workflow BODY,
    where a fresh id on replay forks the conversation; this path has no workflow and no
    replay - which is exactly what `_BANNER` warns about. It is injectable anyway so a test
    can read back which id a turn was filed under.
    """
    return TurnId(str(uuid.uuid4()))


# The command table, and the help text is derived from it so the two cannot drift.
_COMMANDS: Final[tuple[tuple[str, str], ...]] = (
    (":agents", "every loaded profile, with the version a turn would record"),
    (":use <id>", "switch agent - persona, toolset and policy surface all change"),
    (":new <id>", "scaffold a profile file from a template worth reading, then :reload"),
    (":reload", "re-read the profiles directory; a file that fails changes nothing"),
    (":profile", "the RESOLVED configuration of the current agent"),
    (":tools", "the toolset this profile gets, and what policy does to each name"),
    (":policy [tool]", "what the engine decides for this caller, and which rule wins"),
    (":audit", "the tool calls of the last turn, whole: arguments, verdict and reason"),
    (":pending", "what the last turn is waiting on a human for"),
    (":approve <id>", "answer a pending request YES (D25 four-eyes applies to YES only)"),
    (":refuse <id>", "answer it NO - a refusal is never blocked by four-eyes"),
    (":sessions", "conversations in this tenant, the ones stuck on a human marked"),
    (":resume <id>", "file the next turns under that session instead of this run's"),
    (":trace [user|admin]", "the last turn as TranscriptReader projects it, either side"),
    (":mode [direct|durable]", "which path a turn takes - and what that path does not run"),
    (":help", "this list"),
    (":quit", "leave"),
)

_BANNER: Final[tuple[str, ...]] = (
    "agent-core console - an ADMIN-audience operator surface.",
    "",
    "Everything that decides a refusal IS real: the profile, the tool provider, the policy",
    "engine and the audit sink are the ones this deployment was built with.",
    "",
    "ADMIN audience: a suspension is shown with its tool name and tool_call_id. A USER",
    "sees only THAT something is pending (CLAUDE.md non-negotiable #11). `:trace user`",
    "renders the same turn the way a user would have been served it.",
    "",
    "Type :help for commands. Plain text is a turn.",
)

# WHAT EACH MODE DOES NOT EXERCISE, PRINTED AT STARTUP AND AGAIN AT EVERY SWITCH.
#
# Both notices are written to be read by somebody deciding whether a green turn here means
# anything. The direct one is the sentence this console shipped with; the durable one is
# NOT a victory lap - it names coalescing, which is still never run, and it names the
# second process without which a delegated turn parks until its reply timeout.
_MODE_NOTICE: Final[dict[str, tuple[str, ...]]] = {
    DIRECT: (
        "MODE direct - a turn calls StartTurn DIRECTLY, in this process, and answers now.",
        "Three guarantees are therefore NOT exercised, and a turn that works here proves",
        "nothing about them:",
        "  - durability: a crash mid-turn is not recovered and not replayed",
        "  - coalescing: the per-session partitioned queue and its window never run",
        "  - publication: HumanGateway.publish never runs, so a suspension raised here has",
        "    no correlation handle and :approve has nothing to answer (see :pending)",
        "It also never reaches _step_ask_peers, which lives in the workflow body: an",
        "ask_peer call SUSPENDS the turn here and no peer is ever actually asked.",
    ),
    DURABLE: (
        "MODE durable - a turn is enqueued onto the DBOS queue and this console polls the",
        "stored row, exactly as GET /turns/{turn_id} does. It answers when the turn does.",
        "What that DOES exercise, and direct mode does not:",
        "  - durability: the turn is a durable workflow, recovered and replayed on a crash",
        "  - publication: HumanGateway.publish runs, so a suspension has a correlation",
        "    handle a human can be given and :approve can answer",
        "  - delegation: _step_ask_peers runs, so an ask_peer call becomes a real mailbox",
        "    ask instead of a suspension nobody receives",
        "STILL NOT EXERCISED: coalescing. The per-session window belongs to",
        "enqueue_turn_window, which POST /turns opens; this enqueues the turn itself.",
        "A delegated turn only comes back if something drains the peer queue. That is a",
        "SECOND process: python -m agent_core peer-worker --agent <the peer>. Without it",
        "the turn parks until its reply timeout and reports that nobody replied.",
    ),
}

# A profile id becomes a FILE NAME, so it is validated as one rather than trusted as one.
# `../../etc/agent` is a perfectly ordinary-looking agent id and a perfectly effective
# path traversal, and `:new` writes a file that decides what an agent may do.
_PROFILE_ID: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# The scaffold. It opens with the comment the shipped profiles open with, and that is not
# decoration: those files say "a profile is DATA - read top to bottom, this file answers
# what this agent is allowed to do without opening any code", and a scaffold that dropped
# the sentence would teach every agent made from it the opposite lesson.
PROFILE_SCAFFOLD: Final[str] = '''# Scaffolded by the operator console (:new).
#
# Edit it - that is what it is for.
#
# A profile is DATA. Read top to bottom, this file answers "what is this agent allowed to
# do" without opening any code. That readability is the point of having a governance
# layer, so keep the comments honest as you change the values - the next person to read
# this file is deciding whether to trust the agent it describes.

id: {profile_id}

persona: |
  Say what this agent is for, how it should answer, and what it must never assume. The
  persona is instructions to a model, not a description for a catalogue.

model: minimax/MiniMax-M3

# Tool PACKAGES this agent may use. Every name here must already be registered in
# DEFAULT_TOOL_PACKAGES, or this profile is refused at LOAD rather than on its third turn
# (docs/TASKS.md#t-f11-05). Adding a new package is the one thing in this file that is
# code, because a tool is behaviour.
toolsets: []

# Tools this agent BORROWS from a third-party process. They arrive named
# `mcp_<server>_<tool>`, obey the same ToolPolicy as a local tool, and get a smaller
# result budget because the text they return is not ours (CLAUDE.md non-negotiable #4).
mcp_servers: []

# Skill namespaces whose index is put in the system prompt.
skill_namespaces: []

max_iterations: 10
max_cost_usd: "0.10"

# What this agent may not do without a person saying yes. `reason` is the sentence a human
# is actually asked, so write it for them and not for us.
approval_rules: []

# Which other agents this one may ask. An empty list means NOBODY - that is the safe
# default and also the usual reason an A2A setup "does not work". The allowlist is
# two-sided: naming a peer here is not enough unless that peer names this agent back.
peers:
  enabled: false
  peers: []
'''


class UnknownConsoleProfileError(KeyError):
    """`--profile` names no loaded profile.

    A `KeyError` subclass for the same reason `StartTurn.UnknownProfileError` is one, and
    refused for the same reason: a session that silently falls back to some other agent
    attributes every answer in it to the wrong profile.
    """


class Console:
    """The REPL. One agent at a time, one session until `:resume` names another.

    INPUT AND OUTPUT ARE SEAMS, WHICH IS WHAT MAKES IT TESTABLE
        A REPL that can only be driven by a human typing at it is a REPL nobody tests, and
        this is the surface an operator forms their opinion of the system from.
        `read_line` returns `None` at end of input - a piped script, or ctrl-D.

    THE SESSION IS FIXED FOR THE RUN unless `:resume` moves it, so history accumulates
    across turns exactly as it does for a real conversation. Switching agent with `:use`
    deliberately does NOT start a new session: watching one agent pick up the thread
    another one left is an operator question worth being able to ask, and `TurnRequest`
    carries the profile per turn anyway.

    `profiles` IS MUTABLE AND IS MUTATED IN PLACE BY `:reload`, WHICH IS THE WHOLE POINT
        `StartTurn` holds the SAME mapping object, and it is the one that resolves the
        profile a turn actually runs under. Rebinding only this console's reference would
        make `:agents` show the new file while the turn kept running the old one - a
        reload that half-applied, silently, in the direction nobody would check.
    """

    def __init__(
        self,
        *,
        profiles: MutableMapping[str, AgentProfile],
        tools: ToolProvider,
        policy: ToolPolicy,
        start_turn: TurnRunner,
        audit: AuditReader,
        admin: AdminIdentity,
        caller: CallerIdentity,
        session: SessionRef,
        write_line: Callable[[str], None],
        read_line: Callable[[str], str | None],
        profile_id: str | None = None,
        new_turn_id: Callable[[], TurnId] = new_turn_id,
        profiles_dir: Path | None = None,
        load_profiles: ProfileLoader | None = None,
        approvals: ApprovalDecider | None = None,
        transcripts: TranscriptReader | None = None,
        durable_enqueue: DurableEnqueue | None = None,
        durable_poll: DurablePoll | None = None,
        durable: bool = False,
        poll_seconds: float = 0.5,
        poll_timeout_seconds: float = 900.0,
    ) -> None:
        if not profiles:
            raise UnknownConsoleProfileError(
                "no profiles are loaded, so there is no agent to inspect. Check "
                "AGENT_CORE_PROFILES_DIR."
            )
        resolved = sorted(profiles)[0] if profile_id is None else profile_id
        if resolved not in profiles:
            raise UnknownConsoleProfileError(
                f"no profile is loaded under id {resolved!r}. Loaded: "
                f"{', '.join(sorted(profiles))}. Refusing to fall back to another agent."
            )

        self._profiles = profiles
        self._tools = tools
        self._policy = policy
        self._start_turn = start_turn
        self._audit = audit
        self._admin = admin
        self._caller = caller
        self._write = write_line
        self._read = read_line
        self._new_turn_id = new_turn_id
        self._profiles_dir = profiles_dir
        self._load_profiles = load_profiles
        self._approvals = approvals
        self._transcripts = transcripts
        self._enqueue = durable_enqueue
        self._poll = durable_poll
        self._poll_seconds = poll_seconds
        self._poll_timeout_seconds = poll_timeout_seconds

        if durable and (durable_enqueue is None or durable_poll is None):
            raise DurableUnwiredError(
                "the console was asked to start in durable mode and holds no durable "
                "path: enqueueing a turn and polling for it are two seats and it has "
                f"{'no enqueue' if durable_enqueue is None else 'no poll'}. Refusing to "
                "start in a mode this console cannot serve - see the class docstring."
            )
        # PUBLIC, because the prompt carries it and a caller has to be able to read it.
        self.mode: str = DURABLE if durable else DIRECT

        self.profile_id: str = resolved
        # PUBLIC because `:resume` moves it and a caller has to be able to read where the
        # conversation went. `profile_id` is public for the same reason.
        self.session: SessionRef = session
        self._last_turn_id: TurnId | None = None
        self._last_calls: tuple[AuditedToolCall, ...] = ()
        self._pending: tuple[PendingRequest, ...] = ()

    # -- the loop -----------------------------------------------------------------------

    async def run(self) -> None:
        """Print the banner, then read-eval-print until `:quit` or end of input.

        `read_line` is called directly rather than through `asyncio.to_thread`. Blocking
        the loop while a person types is correct HERE and nowhere else in this codebase:
        the console serves no HTTP, and the only work this event loop ever has is the turn
        the operator just typed - so there is nothing in flight for the wait to starve.

        THAT SURVIVES DURABLE MODE, BUT ONLY JUST, SO IT IS WRITTEN DOWN (t-f11-51). A
        console started with `--durable` does launch the DBOS engine, and that engine runs
        its queue manager and its recovery on its OWN threads rather than on this loop. The
        only durable work this loop performs is the poll inside `_durable_turn`, which runs
        between reads and never while one is blocked. The moment this process grows a
        background TASK on this loop, the read has to move off it.
        """
        for line in _BANNER:
            self._write(line)
        self._write("")
        # The active mode's notice, printed with the banner and reprinted at every switch.
        # A mode an operator reads once at startup is a mode they are wrong about an hour
        # later, which is the whole failure this console exists not to be.
        for line in _MODE_NOTICE[self.mode]:
            self._write(line)
        self._write("")
        self._write(self._identity_line())
        self._write("")

        while True:
            entered = self._read(self._prompt())
            if entered is None:
                self._write("")
                self._write("end of input - leaving.")
                return
            line = entered.strip()
            if not line:
                # Pressing enter must not spend money.
                continue
            if line == ":quit":
                self._write("leaving.")
                return
            await self._dispatch(line)

    async def _dispatch(self, line: str) -> None:
        """One command, or one turn. Never both, and never fatal.

        Every failure is reported and the loop survives it. A REPL that dies on a typo -
        or on a database that blipped - is a REPL an operator abandons mid-investigation,
        and the investigation is usually the reason the database blipped.
        """
        try:
            if not line.startswith(":"):
                await self._turn(line)
                return

            command, _, argument = line.partition(" ")
            argument = argument.strip()

            match command:
                case ":agents":
                    self._agents()
                case ":use":
                    self._use(argument)
                case ":new":
                    self._new(argument)
                case ":reload":
                    self._reload()
                case ":profile":
                    self._profile()
                case ":tools":
                    await self._tools_command()
                case ":policy":
                    await self._policy_command(argument or None)
                case ":audit":
                    self._audit_command()
                case ":pending":
                    self._pending_command()
                case ":approve":
                    await self._decide(argument, approved=True)
                case ":refuse":
                    await self._decide(argument, approved=False)
                case ":sessions":
                    await self._sessions()
                case ":resume":
                    self._resume(argument)
                case ":trace":
                    await self._trace(argument)
                case ":mode":
                    self._mode_command(argument)
                case ":help":
                    self._help()
                case _:
                    self._write(f"unknown command {command!r}. Try :help.")
        except Exception as error:
            # Reported, never fatal - see this method's docstring.
            self._write(f"! {type(error).__name__}: {error}")

    # -- navigation ---------------------------------------------------------------------

    def _agents(self) -> None:
        self._write(f"{len(self._profiles)} profile(s) loaded:")
        for profile_id in sorted(self._profiles):
            profile = self._profiles[profile_id]
            marker = "*" if profile_id == self.profile_id else " "
            toolsets = ", ".join(profile.toolsets) or "none"
            self._write(
                f" {marker} {profile_id}  v{profile.version}  model={profile.model}  "
                f"toolsets={toolsets}"
            )
        self._write("  (* is the current agent; :use <id> switches)")

    def _use(self, profile_id: str) -> None:
        if not profile_id:
            self._write("usage: :use <id>. :agents lists them.")
            return
        target = self._profiles.get(profile_id)
        if target is None:
            self._write(
                f"no profile is loaded under id {profile_id!r}. Staying on "
                f"{self.profile_id!r}: switching to a guess would attribute every answer "
                "after it to the wrong agent."
            )
            return

        previous = self._profiles[self.profile_id]
        self.profile_id = profile_id
        self._write(f"now talking to {profile_id} (v{target.version}).")
        self._write(f"  persona:   {_first_line(previous.persona)}")
        self._write(f"          -> {_first_line(target.persona)}")
        self._write(f"  model:     {previous.model} -> {target.model}")
        self._write(
            f"  toolsets:  {', '.join(previous.toolsets) or 'none'} -> "
            f"{', '.join(target.toolsets) or 'none'}"
        )
        self._write(
            f"  approvals: {len(previous.approval_rules)} rule(s) -> "
            f"{len(target.approval_rules)} rule(s)"
        )
        self._write(
            "  policy:    unchanged rules, different tool names - :tools and :policy now "
            "answer for this agent's surface."
        )

    # -- making and editing an agent (t-f11-09) -----------------------------------------

    def _new(self, profile_id: str) -> None:
        """Scaffold one profile file. It does NOT load it - `:reload` does.

        SCAFFOLDING AND LOADING ARE SEPARATE ON PURPOSE. A file written straight into the
        running set would be an agent nobody read, assembled from a template, holding
        whatever permissions the template happened to carry. Writing then reloading means
        the operator opens the file first, which is the only review this path has.
        """
        if not profile_id:
            self._write("usage: :new <id>. The id becomes the file name and the agent id.")
            return
        if self._profiles_dir is None:
            self._write(
                "cannot scaffold: this console has no profiles directory wired, so there "
                "is nowhere to write the file. Start it with AGENT_CORE_PROFILES_DIR set."
            )
            return
        if not _PROFILE_ID.fullmatch(profile_id):
            self._write(
                f"{profile_id!r} is not a usable profile id. Lower-case letters, digits "
                "and underscores, starting with a letter. The id becomes a FILE NAME, and "
                "a file name that can contain a path separator can be written outside the "
                "profiles directory."
            )
            return
        if profile_id in self._profiles:
            self._write(
                f"{profile_id!r} is already loaded. Edit its file and run :reload; "
                "scaffolding over a live agent would replace what it is allowed to do."
            )
            return

        path = self._profiles_dir / f"{profile_id}.yaml"
        if path.exists():
            self._write(
                f"{path.name} already exists and is NOT overwritten. This file decides "
                "what an agent may do, and a template is not a merge."
            )
            return

        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            PROFILE_SCAFFOLD.format(profile_id=profile_id), encoding="utf-8", newline="\n"
        )
        self._write(f"wrote {path}")
        self._write(
            "  it names no toolsets, no peers and no approval rules yet, which is the "
            "safe shape: every one of those is an allowlist where empty means nobody."
        )
        self._write("  read it, edit it, then :reload. It is not loaded until you do.")

    def _reload(self) -> None:
        """Re-read the whole directory, or change nothing at all.

        ALL OR NOTHING, AND THE REFUSAL IS THE FEATURE. An agent is edited far more often
        than it is invented, so this is the command that gets used - and a reload that
        half-applied would leave the operator reading one profile while the turn ran
        another. The loader answers with the WHOLE directory or it raises; on a raise the
        previously loaded profiles stay exactly as they were and this says so.

        IT MUTATES THE MAPPING IN PLACE. `StartTurn` holds the same object and it is the
        one that decides which profile a turn actually runs under. See the class docstring.
        """
        if self._load_profiles is None:
            self._write(
                "cannot reload: this console has no profile loader wired. The loader is "
                "what reads the directory and assigns versions (D20); without it the "
                "console could only guess at both."
            )
            return

        try:
            loaded = self._load_profiles()
        except Exception as error:
            self._write(f"! the profiles directory did not load: {type(error).__name__}: {error}")
            self._write(
                f"  NOTHING CHANGED. The {len(self._profiles)} profile(s) loaded before "
                "this command are still in force, and the turn you run next uses them. "
                "Fix the file and run :reload again."
            )
            return

        if not loaded:
            self._write(
                "! the directory loaded and holds no profiles at all. NOTHING CHANGED: an "
                "empty set would leave this console with no agent to talk to."
            )
            return
        if self.profile_id not in loaded:
            self._write(
                f"! {self.profile_id!r} is the agent in force and the reloaded directory "
                "no longer has it. NOTHING CHANGED, because applying this would either "
                "leave you talking to a profile that is gone or switch you to another "
                f"agent silently. :use one of {', '.join(sorted(loaded))} first."
            )
            return

        before = dict(self._profiles)
        self._profiles.clear()
        self._profiles.update(loaded)

        added = sorted(set(loaded) - set(before))
        removed = sorted(set(before) - set(loaded))
        changed = sorted(
            profile_id
            for profile_id in set(loaded) & set(before)
            if loaded[profile_id].content_hash != before[profile_id].content_hash
        )

        self._write(f"reloaded: {len(loaded)} profile(s) now in force.")
        for profile_id in added:
            self._write(f"  + {profile_id}  v{loaded[profile_id].version}  (new)")
        for profile_id in removed:
            self._write(f"  - {profile_id}  (gone from the directory)")
        for profile_id in changed:
            self._write(
                f"  ~ {profile_id}  v{before[profile_id].version} -> "
                f"v{loaded[profile_id].version}  (content changed, so the version moved)"
            )
        if not (added or removed or changed):
            self._write("  nothing changed - every file resolved to what was already loaded.")
        self._write(
            "  a version moves only when what the agent may DO changes (D20), so "
            "reformatting a file deliberately does not bump it."
        )

    # -- inspection ---------------------------------------------------------------------

    def _profile(self) -> None:
        """The RESOLVED configuration, which is the whole point - not the raw YAML.

        Every block a profile file may omit is printed with the value the loader settled
        on, because "does this agent have knowledge retrieval" must have an answer even
        when the file says nothing about it.
        """
        profile = self._profiles[self.profile_id]
        self._write(
            f"profile {profile.id}  v{profile.version}  content={profile.content_hash[:12]}"
        )
        self._write("  persona:")
        for line in profile.persona.splitlines():
            self._write(f"    {line}")
        self._write(f"  model:            {profile.model}")
        self._write(f"  max_iterations:   {profile.max_iterations}")
        self._write(f"  max_cost_usd:     {profile.max_cost_usd}")
        self._write(f"  toolsets:         {', '.join(profile.toolsets) or 'none'}")
        self._write(f"  skill_namespaces: {', '.join(profile.skill_namespaces) or 'none'}")

        if profile.mcp_servers:
            self._write("  mcp_servers:")
            for server in profile.mcp_servers:
                # NAME AND TRANSPORT ONLY. The url and the command line are where a token
                # lives in real configuration; see the module docstring.
                self._write(
                    f"    - {server.name} via {server.transport} "
                    f"(endpoint withheld)  budget={server.result_budget_chars} chars"
                )
        else:
            self._write("  mcp_servers:      none")

        if profile.approval_rules:
            self._write("  approval_rules:")
            for rule in profile.approval_rules:
                condition = rule.condition if rule.condition is not None else "always"
                self._write(f"    - {rule.tool_name} when {condition}")
                self._write(f"      {rule.reason}")
        else:
            self._write("  approval_rules:   none")

        compaction = profile.compaction
        self._write(
            f"  compaction:       trigger={compaction.trigger_fraction} "
            f"target={compaction.target_fraction} head={compaction.head_exchanges} "
            f"tail={compaction.tail_tokens}"
        )
        knowledge = profile.knowledge
        self._write(
            f"  knowledge:        enabled={knowledge.enabled} mode={knowledge.mode.value} "
            f"collections={', '.join(knowledge.collections) or 'none'} "
            f"top_k={knowledge.top_k}"
        )
        media = profile.media
        kinds = ", ".join(sorted(kind.value for kind in media.accepted_kinds)) or "none"
        self._write(
            f"  media:            kinds={kinds} delivery={media.delivery.value} "
            f"max_bytes={media.max_bytes} evidence={media.allow_evidence_requests}"
        )
        peers = profile.peers
        named = ", ".join(peer.agent_id for peer in peers.peers) or "NOBODY"
        self._write(
            f"  peers:            enabled={peers.enabled} max_hops={peers.max_hops} "
            f"visibility={peers.visibility.value} may_ask={named}"
        )

    async def _tools_command(self) -> None:
        """The profile -> toolset resolution, through the real port, plus the narrowing.

        Two facts, and both matter. `ToolProvider.tool_names_for` answers what this profile
        RESOLVES to - the thing `t-f1-21` exists to perform, `mcp_`-prefixed names included.
        `ToolPolicy` then answers which of those the model is ever SHOWN: a DENY tool is
        never advertised, because advertising a forbidden tool invites the model to keep
        trying it and burns the budget for nothing.
        """
        profile = self._profiles[self.profile_id]
        names = await self._tools.tool_names_for(profile)
        rules = await self._policy.load_rules(self._caller)

        if not names:
            self._write(f"{profile.id} resolves to no tools at all.")
            return

        self._write(f"{profile.id} resolves to {len(names)} tool(s):")
        for name in names:
            decision = self._policy.decide(rules, name, {})
            fence = " UNTRUSTED-WRAPPED" if is_untrusted(name) else ""
            self._write(f"  {name:<28} {_verdict(decision)}{fence}")

        advertised = self._policy.filter_toolset(rules, names)
        self._write(
            f"  advertised to the model: {len(advertised)} of {len(names)} "
            f"(a {Effect.DENY.value} tool is never shown to it)"
        )
        self._write(
            "  UNTRUSTED-WRAPPED: whatever that tool returns is fenced in "
            f"{UNTRUSTED_OPEN} delimiters and capped at the smaller budget before the "
            "model sees it (CLAUDE.md non-negotiables #4 and #10)."
        )

    async def _policy_command(self, tool_name: str | None) -> None:
        """Ask the live engine, for this caller, and name the rule that wins.

        Policy is one of CLAUDE.md's silent-bug areas: a hole never fails a test, it just
        never fires. A human able to ask the engine directly is worth more than another
        test - so this prints the `rule_id` an auditor would read and the `reason` the
        model or the human would be handed, not a yes/no.
        """
        profile = self._profiles[self.profile_id]
        rules = await self._policy.load_rules(self._caller)

        if tool_name is None:
            names = await self._tools.tool_names_for(profile)
            if not names:
                self._write(f"{profile.id} resolves to no tools, so there is nothing to ask.")
                return
            targets: tuple[str, ...] = names
        else:
            targets = (tool_name,)

        self._write(
            f"policy for subject={self._caller.subject_id} tenant={self._caller.tenant_id} "
            f"channel={self._caller.channel} "
            f"roles={', '.join(sorted(self._caller.roles)) or 'none'}"
        )
        self._write(
            f"  (a rule with no channel or role constraint applies to any; default effect "
            f"is {rules.default_effect.value})"
        )
        for name in targets:
            decision = self._policy.decide(rules, name, {})
            self._write(f"  {name:<28} {_verdict(decision)}")
            self._write(f"      {decision.reason}")
        self._write(
            "  arguments are NOT part of this answer: :policy asks with an empty argument "
            "map, and a rule can still decide differently on a real call."
        )

    def _audit_command(self) -> None:
        if self._last_turn_id is None:
            self._write("no turn has run yet in this session, so there is nothing to show.")
            return
        self._write(f"tool calls of turn {self._last_turn_id}:")
        self._render_tool_calls(self._last_calls)

    # -- the mode (t-f11-51) ------------------------------------------------------------

    def _mode_command(self, argument: str) -> None:
        """Report the active mode, or switch. Both print what that mode does NOT run.

        WITH NO ARGUMENT IT IS A QUESTION, NOT A TOGGLE. `:mode` alone answering "durable,
        and here is what durable skips" is the command an operator reaches for when they
        are about to trust a green turn. A bare `:mode` that flipped the switch would be a
        question that changed the answer.

        A REFUSAL NAMES WHAT IS MISSING AND WHAT FIXES IT. The durable path needs the DBOS
        engine running in THIS process, and that is a decision `main.run_console` makes
        from `--durable` - so a console started without it holds no durable seats and says
        which command it would have to be started with.
        """
        requested = argument.lower()
        if not requested:
            other = DIRECT if self.mode == DURABLE else DURABLE
            self._write(f"mode: {self.mode}  (the other one is {other}; :mode {other} switches)")
            for line in _MODE_NOTICE[self.mode]:
                self._write(f"  {line}")
            return
        if requested not in _MODE_NOTICE:
            self._write(
                f"usage: :mode [{DIRECT}|{DURABLE}]. {argument!r} is neither, and there is "
                "no third path a turn can take from here."
            )
            return
        if requested == DURABLE and (self._enqueue is None or self._poll is None):
            self._write(
                "cannot switch: this console has no durable path wired. Enqueueing a turn "
                "puts it on the DBOS queue, and that queue does not exist until the "
                "durable engine is launched in THIS process - which is what `python -m "
                "agent_core console --durable` does. Staying on direct rather than "
                "promising a mode that would fail at the enqueue."
            )
            return

        previous = self.mode
        self.mode = requested
        if previous == requested:
            self._write(f"already on {self.mode}. Nothing changed.")
        else:
            self._write(f"mode: {previous} -> {self.mode}. The prompt carries it from here.")
        for line in _MODE_NOTICE[self.mode]:
            self._write(f"  {line}")

    def _help(self) -> None:
        self._write("commands:")
        width = max(len(name) for name, _ in _COMMANDS)
        for name, description in _COMMANDS:
            self._write(f"  {name:<{width}}  {description}")
        self._write("  anything else     a turn: it goes to the current agent")

    # -- a turn -------------------------------------------------------------------------

    def _request_for(self, text: str) -> TurnRequest:
        """One typed sentence as a turn request. Identical in both modes, deliberately.

        The caller, the session and the profile are what policy, the audit trail and the
        `ask_peer` grant all key on - so a durable turn and a direct one are the SAME
        request taking a different route, and a verdict read in one mode is a verdict in
        the other. A durable path that built its own caller would make `:policy` a liar.
        """
        return TurnRequest(
            session=self.session,
            caller=self._caller,
            profile_id=self.profile_id,
            input=UserInput(text=text),
        )

    async def _turn(self, text: str) -> None:
        """Route one turn by the active mode. See `_MODE_NOTICE` for what each one runs."""
        if self.mode == DURABLE:
            await self._durable_turn(text)
            return
        await self._direct_turn(text)

    async def _direct_turn(self, text: str) -> None:
        turn_id = self._new_turn_id()
        request = self._request_for(text)
        outcome = await self._start_turn.execute(turn_id, request)

        self._last_turn_id = turn_id
        # Read the trail AFTER the turn, so what is rendered is what was actually
        # recorded - not what this process believes it did.
        #
        # ITS OWN try/except, INSIDE `_dispatch`'s. A trail that cannot be read is worth
        # saying out loud and is not worth the model's answer: the turn already ran, was
        # already paid for, and the text is the one thing this process will not be able to
        # reproduce. Letting the read's failure reach `_dispatch` would throw it away.
        trail_error: str | None = None
        try:
            self._last_calls = await self._audit.tool_calls_for_turn(self._admin, turn_id)
        except Exception as error:
            self._last_calls = ()
            trail_error = f"{type(error).__name__}: {error}"

        # SET BEFORE `_render_tool_calls` RUNS, NOT AFTER. t-f11-46 (1): a call the sink
        # recorded as ALLOW may still have DEFERRED rather than run to completion - see
        # that method's docstring - and telling the two apart means it must already know
        # which tool names this very turn suspended on.
        self._pending = outcome.pending if outcome.is_suspended else ()

        self._write(f"[turn {turn_id} / {self.profile_id}]")
        if trail_error is not None:
            self._write(f"  ! the audit trail for this turn could not be read: {trail_error}")
        else:
            self._render_tool_calls(self._last_calls)

        if outcome.is_suspended:
            self._render_suspension(outcome.pending)
            return

        result = outcome.result
        if result is None:  # pragma: no cover - TurnOutcome.__post_init__ refuses this
            self._write("the turn is neither suspended nor finished, which cannot happen.")
            return
        self._write(result.text)
        usage = result.usage
        self._write(
            f"  ({usage.input_tokens} in / {usage.output_tokens} out / "
            f"{usage.cached_tokens} cached, cost {usage.cost_usd})"
        )

    async def _durable_turn(self, text: str) -> None:
        """Enqueue the turn, then poll for it the way `GET /turns/{turn_id}` does. t-f11-51.

        WHY THIS IS A POLL AND NOT A WAIT ON THE WORKFLOW HANDLE
            `enqueue_turn` answers with a durable handle whose `get_result()` would block
            until the turn finished, and taking that shortcut would make this console the
            one caller in the deployment that reads a turn a way nothing else can. The
            stored `turns` row is what every other reader has - the HTTP route, another
            process, an operator tomorrow - so polling it is the only thing that proves the
            answer is actually WHERE a reader will look for it. `PullModeChannel`'s
            docstring makes the same argument for delivery, and the console's own `cli`
            channel entry is that class for exactly this reason.

        THE ID IS THE WORKFLOW'S AND THE AUDIT TRAIL'S AT ONCE. `_step_new_turn_id` reads
        the durable workflow id back rather than minting one, so the id printed here is the
        id `:audit` and `:trace` then answer for. There is no join to remember.

        A TURN THAT IS STILL `waiting` WHEN THE DEADLINE PASSES IS REPORTED, NOT ABANDONED.
        The workflow keeps running in this process; what ran out is this console's
        patience, and saying so is different from saying the turn failed.
        """
        if self._enqueue is None or self._poll is None:  # pragma: no cover - see __init__
            self._write(
                "cannot run a durable turn: this console has no durable path wired. "
                "`:mode durable` is refused for the same reason."
            )
            return

        request = self._request_for(text)
        turn_id = await self._enqueue(request)
        self._last_turn_id = turn_id
        self._last_calls = ()
        self._pending = ()
        self._write(f"[turn {turn_id} / {self.profile_id}] enqueued on the durable queue.")

        view = await self._poll_until_settled(turn_id)
        if view is None:
            self._write(
                f"  no row for this turn in this tenant after "
                f"{self._poll_timeout_seconds:.0f}s. The enqueue succeeded, so the turn "
                "exists on the queue and has not been dequeued yet - nothing was invented "
                "here to fill the gap. Is a worker running for this application version?"
            )
            return
        if view.status != "finished":
            self._write(
                f"  still {view.status} after {self._poll_timeout_seconds:.0f}s. The "
                "workflow is durable and is still running; this console stopped waiting. "
                "A delegated turn needs `python -m agent_core peer-worker` draining the "
                "peer queue, and a suspension for a HUMAN was published to the channel it "
                "was raised on - :approve answers it with the handle a human was given."
            )
            return

        trail_error: str | None = None
        try:
            self._last_calls = await self._audit.tool_calls_for_turn(self._admin, turn_id)
        except Exception as error:  # see `_direct_turn` for why this catch is its own
            self._last_calls = ()
            trail_error = f"{type(error).__name__}: {error}"
        if trail_error is not None:
            self._write(f"  ! the audit trail for this turn could not be read: {trail_error}")
        else:
            self._render_tool_calls(self._last_calls)

        # `TurnView.text` is None for anything unfinished, and this branch is finished -
        # but the type says `str | None` and inventing an answer for a null is the one
        # thing a console reading somebody else's row must never do.
        self._write(view.text if view.text is not None else "(the row carries no text)")
        self._write(
            "  (no usage line: the durable path reads the stored row, which carries the "
            "answer and not the token counts. :trace admin has the cost per entry.)"
        )

    async def _poll_until_settled(self, turn_id: TurnId) -> TurnSnapshot | None:
        """Poll until the turn finishes or this console's patience runs out.

        `waiting` is reported ONCE, the first time it is seen, and then polling continues:
        a turn that parked on a peer ask or on a human goes waiting and then finished, and
        a poll that stopped at the first `waiting` would report a delegated turn as stuck
        every single time.

        AND `None` IS "NOT YET", NOT "NEVER". The enqueue puts the turn on the queue; the
        `turns` row is written by `StartTurn` once a worker has dequeued it, which is a
        moment later. Treating the first empty read as a missing turn would report every
        durable turn as lost and then print its answer nowhere.
        """
        assert self._poll is not None  # guarded by `_durable_turn`
        deadline = self._poll_timeout_seconds
        waited = 0.0
        announced = False
        view = await self._poll(turn_id, self._caller)
        while (view is None or view.status != "finished") and waited < deadline:
            if view is not None and view.status == "waiting" and not announced:
                announced = True
                self._write(
                    "  waiting - the turn suspended on something outside itself (a peer "
                    "ask, a human, an upload) and is parked durably until it is answered."
                )
            await asyncio.sleep(self._poll_seconds)
            waited += self._poll_seconds
            view = await self._poll(turn_id, self._caller)
        return view

    def _render_tool_calls(self, calls: tuple[AuditedToolCall, ...]) -> None:
        """A tool call WHOLE: who, when, what it was passed, and what the rule SAID.

        t-f11-11. A name and an effect tell an operator that something was refused; they
        do not tell them why, and "why" is a sentence the policy engine wrote and the
        model or the human was handed. Rendering the name alone is how a refusal becomes
        folklore.

        "Whether it ran" is a SEPARATE line from the effect on purpose - and t-f11-46 (1)
        is exactly the day that pair stopped agreeing. `AuditedToolCall.executed` mirrors
        `effect is ALLOW` because that is what `before_tool_execute` could know AT THE
        TIME: the sink writes intent and verdict BEFORE the tool body runs (CLAUDE.md
        non-negotiable #6), never the outcome. For an ordinary tool "policy let it
        through" and "it ran to completion" are the same fact. For `ask_peer` they are
        not: policy ALLOWS it and the call still DEFERS to a peer instead of finishing, so
        `effect is ALLOW` alone would print "executed" on a call that never did.

        THE THIRD STATE COMES FROM `self._pending`, NOT FROM THE SINK. The trail has no
        column for "still waiting" - it was never told, and inventing one here would be
        the console pretending to know more than the append-only record does. What the
        console DOES already hold is this turn's own suspension list, set by `_turn`
        before this method runs, so a call whose name is in it is the one that deferred -
        distinct from "ran" (ALLOW, not deferred) and from "NOT EXECUTED" (DENY, which
        refuses in place and hands the model the rule's sentence). A NEEDS_APPROVAL
        verdict is now the DEFERRED case, not the refused one: `t-f11-45` made
        `PolicyEnforcement.before_tool_execute` raise `ApprovalRequired` rather than
        blocking, so an approval suspends through the same path a peer ask does and can
        be answered after a redeploy.
        """
        if not calls:
            self._write("  (no tool calls)")
            return
        deferred_tool_names = {request.tool_name for request in self._pending}
        for call in calls:
            if call.tool_name in deferred_tool_names:
                ran = "DEFERRED - suspended, awaiting an answer; it has not finished"
            elif call.executed:
                ran = "executed"
            else:
                ran = "NOT EXECUTED"
            rule = call.rule_id if call.rule_id is not None else "no matching rule"
            self._write(f"  tool {call.tool_name} -> {call.effect.value} [{rule}] - {ran}")
            self._write(
                f"      at {call.at.isoformat()}  by subject={call.caller_subject_id}"
            )
            if call.reason is None:
                # `None` is a row written before migration 0022 gave the table a column
                # for the sentence. It does NOT mean the rule said nothing, and rendering
                # it as an empty reason would put words in an auditor's mouth.
                self._write(
                    "      reason: NOT RECORDED - this row predates migration 0022, which "
                    "added the column. The rule was not silent; the table had nowhere to "
                    "keep what it said."
                )
            else:
                self._write(f"      reason: {call.reason}")
            self._render_arguments(call.arguments)

    def _render_arguments(self, arguments: Mapping[str, object]) -> None:
        """Every argument, whole, one per line - and no second redaction pass.

        t-f11-11. THE SINK ALREADY REDACTED THESE, through its per-tool allowlist, and
        nothing here re-redacts: two redaction paths drift, and the drifting one is always
        the one nobody reads until an incident. So an absent field means the tool never
        opted into recording it, not that the model passed nothing - which is the sentence
        below, printed every time, because an operator who guesses wrong about that guesses
        in the direction of "the model did not do it".
        """
        if not arguments:
            self._write(
                "      arguments: none recorded. The sink keeps only the fields a tool "
                "opted into, so this does not mean the model passed none."
            )
            return
        self._write("      arguments (as the sink stored them, already redacted):")
        for key in sorted(arguments):
            self._write(f"        {key} = {arguments[key]!r}")

    def _render_suspension(self, pending: tuple[PendingRequest, ...]) -> None:
        """ADMIN audience - see the module docstring.

        The tool name and the `tool_call_id` are shown because answering the suspension is
        the operator's job and neither can be guessed. A USER-audience view of the same
        turn shows THAT something is pending and nothing else - `:trace user` renders it.

        A DELEGATION's `reason` IS THE USER-FACING NOTICE, NOT THE ASK - AND THAT PART IS
        CORRECT. `application/start_turn.py::_notice_for` replaces it for every reader,
        admin included, because `StartTurn` has no audience to tell apart (CLAUDE.md
        non-negotiable #11 forbids a USER from learning which peer was asked, and D17
        requires the SAME sentence to reach whoever reads the outcome next - there is no
        seat here to answer differently for). t-f11-46 (2): that made this ADMIN surface a
        copy of the user's view for exactly the one kind t-f11-13 exists to tell apart. So
        this reconstructs the raw ask from `arguments`, which `_notice_for` never touches -
        the peer and the question are the RECORD a resumed call keys on, not the message,
        and were always there to read; see `_raw_delegation_ask`.
        """
        self._write(f"SUSPENDED - waiting on {len(pending)} request(s):")
        for request in pending:
            self._write(
                f"  {request.kind.value}  tool={request.tool_name}  "
                f"tool_call_id={request.tool_call_id}"
            )
            self._write(f"      {request.reason}")
            if request.kind is PendingKind.DELEGATION:
                self._write(
                    "      admin sees the raw ask too (t-f11-13): "
                    f"{_raw_delegation_ask(request.arguments)}"
                )
            self._render_arguments(request.arguments)
        self._write(
            "  (this view is ADMIN audience: a user is told only that something is "
            "pending, never which tool. `:trace user` shows you that projection.)"
        )
        self._write("  :pending repeats this list; :approve and :refuse answer one.")

    # -- the approval path (t-f11-10) ---------------------------------------------------

    def _pending_command(self) -> None:
        """What the last turn is waiting on, and the truth about answering it from here.

        THE HONEST PART IS THE SECOND HALF, AND IT IS DIFFERENT IN THE TWO MODES (t-f11-51)
            In DIRECT mode this console calls `StartTurn` directly, so `HumanGateway.publish`
            never ran and no correlation row exists for anything listed here - which means
            `:approve` has no handle to take. Printing a queue and leaving an operator to
            discover that by typing at it is exactly the "exercises less than the real path"
            failure `_MODE_NOTICE` exists to prevent.

            In DURABLE mode the workflow publishes the suspension, so a handle DOES exist -
            and it exists in the correlation table rather than in this console's memory,
            because a durable turn is answered by whoever was published to, from whatever
            process is holding it. So the durable answer here is not a list: it is where the
            list actually lives.
        """
        if not self._pending:
            if self.mode == DURABLE:
                self._write(
                    "nothing is held HERE. A durable turn publishes its suspension through "
                    "HumanGateway, so what is pending lives in the correlation table and on "
                    "the channel it was published to - not in this console's memory. "
                    ":approve <handle> takes the handle the human was given there, and D25 "
                    "still refuses an approver who is the requester."
                )
                return
            self._write(
                "nothing is pending: the last turn in this session finished, or no turn "
                "has run yet."
            )
            return
        self._write(f"pending in this session ({len(self._pending)}):")
        for request in self._pending:
            self._write(
                f"  {request.kind.value}  tool={request.tool_name}  "
                f"tool_call_id={request.tool_call_id}"
            )
            self._write(f"      {request.reason}")
            if request.kind is PendingKind.DELEGATION:
                # t-f11-46 (2) - see `_render_suspension`, which this list otherwise
                # duplicates: the reason is the user's notice, not the ask.
                self._write(
                    "      admin sees the raw ask too (t-f11-13): "
                    f"{_raw_delegation_ask(request.arguments)}"
                )
        self._write(
            "  These were raised by a turn this console started DIRECTLY, so "
            "HumanGateway.publish never ran and none of them has a correlation handle. "
            ":approve takes the handle a human was given on the channel the request was "
            "published on - a turn started through the workflow has one; these do not."
        )

    async def _decide(self, correlation_id: str, *, approved: bool) -> None:
        """Answer one pending request YES or NO, as THIS caller. docs/DECISIONS.md#d25.

        FOUR-EYES WILL USUALLY REFUSE HERE, AND THAT IS THE CONTROL WORKING
            The operator sitting at this console is usually the operator who started the
            turn, and D25's rule is that an approval must come from someone other than the
            human who started it. So the common outcome of `:approve` from a terminal is a
            refusal - and a refusal an operator cannot tell apart from a crash is a refusal
            that gets worked around, which is how a control becomes decoration.

            So this names the rule, names the two subjects, says which half of D25 fired,
            and says what actually resolves it. It never retries, never re-asks under a
            different identity, and offers no flag that would let one.

        `:refuse` IS NEVER GATED. Refusing your own request removes a permission rather
        than conferring one, and blocking it would leave the turn asleep with the one
        person who wants it stopped unable to stop it. D25 gates `approved=True` only.
        """
        verb = "approve" if approved else "refuse"
        if not correlation_id:
            self._write(f"usage: :{verb} <correlation-id>. :pending says where one comes from.")
            return
        if self._approvals is None:
            self._write(
                "cannot answer: this console has no approval path wired. Deciding an "
                "approval is the DecideApproval use case - it records the decision and "
                "wakes the waiting turn - and the console holds one or it holds nothing."
            )
            return

        try:
            turn_id, tool_call_id = await self._approvals.execute(
                correlation_id, self._caller.subject_id, approved, "answered from the console"
            )
        except FourEyesError as refused:
            self._write("REFUSED by the four-eyes rule (docs/DECISIONS.md#d25) - not a fault.")
            self._write(f"  grounds recorded: {refused}")
            self._write(
                f"  you are answering as subject={self._caller.subject_id!r}, which is "
                "the identity this console runs every turn under. An approval must come "
                "from someone other than the human who started the turn, and nothing here "
                "can show that it did."
            )
            self._write(
                "  the attempt IS on record: a rejected decision was appended before this "
                "refusal, so the control can be shown to have fired."
            )
            self._write(
                "  what resolves it: a SECOND person answers - another operator running "
                "this console under their own --subject, or the human the request was "
                "published to on the channel. :refuse is never blocked by this rule."
            )
            return
        except UnknownCorrelationError as unknown:
            self._write(f"no pending request answers to that handle: {unknown}")
            self._write(
                "  handles expire, and one that is not yours resolves to nothing on "
                "purpose. Nothing was recorded and no turn was woken."
            )
            return

        answer = "APPROVED" if approved else "REFUSED"
        self._write(f"{answer} - recorded, and the waiting turn was signalled.")
        self._write(f"  turn={turn_id}  tool_call_id={tool_call_id}")
        if self.mode == DURABLE:
            self._write(
                "  the turn resumes in whichever process holds it - which in durable mode "
                "may well be this one. Type the next line and the poll picks it up; "
                ":trace shows where it got to."
            )
        else:
            self._write(
                "  the turn resumes in whichever process holds it. This console started no "
                "workflow, so watch it there and not here."
            )

    # -- sessions and the trace (t-f11-12, t-f11-13) ------------------------------------

    async def _sessions(self) -> None:
        """The inbox, ADMIN audience, with the ones stuck on a human marked.

        A console that can only start fresh cannot reach a long conversation, and
        compaction only has anything to do on a long one - so this is the command that
        makes CLAUDE.md's compaction row in the silent-bug table reachable by a person.
        """
        if self._transcripts is None:
            self._write(
                "cannot list conversations: this console has no transcript reader wired. "
                "The conversation list is TranscriptReader's projection, and the console "
                "must not assemble a second one out of SQL."
            )
            return

        rows, cursor = await self._transcripts.list_conversations(
            self.session.tenant_id, Audience.ADMIN
        )
        if not rows:
            self._write(f"no conversations in tenant {self.session.tenant_id}.")
            return

        self._write(f"{len(rows)} conversation(s) in tenant {self.session.tenant_id}:")
        for row in rows:
            marker = "*" if row.session.session_id == self.session.session_id else " "
            waiting = (
                f"  WAITING ON A HUMAN since {row.waiting_since.isoformat()}"
                if row.is_suspended and row.waiting_since is not None
                else "  WAITING ON A HUMAN"
                if row.is_suspended
                else ""
            )
            self._write(
                f" {marker} {row.session.session_id}  agent={row.profile_id}  "
                f"messages={row.message_count}  last={row.last_activity_at.isoformat()}"
                f"{waiting}"
            )
            if row.total_cost_usd is not None:
                self._write(f"      cost so far: {row.total_cost_usd}")
        if cursor is not None:
            self._write(f"  more follow this page (cursor {cursor}).")
        self._write("  (* is this console's session; :resume <id> moves to another)")

    def _resume(self, session_id: str) -> None:
        """File the next turns under another session. t-f11-12.

        THE LAST TURN'S STATE IS DROPPED, AND DROPPING IT IS THE POINT. `:audit`,
        `:pending` and `:trace` all answer "the last turn"; keeping the old one after the
        conversation moved would answer a question about session A while the prompt said
        B, and an operator reading a trail attributed to the wrong conversation is worse
        off than one reading none.
        """
        if not session_id:
            self._write("usage: :resume <session-id>. :sessions lists them.")
            return

        previous = self.session
        self.session = SessionRef(
            session_id=SessionId(session_id),
            # THE TENANT IS THIS CONSOLE'S, never one typed at a prompt. `SessionRef`
            # carries the tenant so a session can never be named without one, and the
            # tenant predicate in every query comes from it - letting an operator type one
            # would turn this command into cross-tenant read access.
            tenant_id=previous.tenant_id,
        )
        self._last_turn_id = None
        self._last_calls = ()
        self._pending = ()
        self._write(
            f"now writing to session {self.session.session_id} in tenant "
            f"{self.session.tenant_id} (was {previous.session_id})."
        )
        self._write(
            "  history for that session is loaded by the next turn, from the store. The "
            "last turn's audit, pending list and trace were dropped: they belonged to the "
            "conversation you just left."
        )

    async def _trace(self, argument: str) -> None:
        """The last turn as `TranscriptReader` projects it, in EITHER audience. t-f11-13.

        THIS IS THE ONLY PRACTICAL CHECK ON NON-NEGOTIABLE #11, whose two halves pull
        against each other: a user must see THAT something is pending and must never learn
        WHICH tool. Nothing fails a test when a projection gets that wrong; an operator
        looking at the user's own view does see it.

        THE GRID IS INDEXED, NOT CONSULTED WITH A FALLBACK. `VISIBILITY` is declared TOTAL
        over EntryKind x Audience for exactly this reason, so `VISIBILITY[kind][audience]`
        raises on a kind nobody decided instead of defaulting - and a default here is how a
        new EntryKind becomes user-visible with nobody having chosen that.
        """
        audience = _AUDIENCES.get(argument.lower()) if argument else Audience.ADMIN
        if audience is None:
            self._write(
                f"usage: :trace [user|admin]. {argument!r} is neither, and there is no "
                "third audience - domain/transcript.py declares exactly two."
            )
            return
        if self._transcripts is None:
            self._write(
                "cannot trace: this console has no transcript reader wired. The trace is "
                "TranscriptReader's projection or it is nothing - a view the console "
                "rendered from its own memory would agree with itself by construction."
            )
            return

        page = await self._transcripts.page(self.session, audience)
        entries = tuple(
            entry
            for entry in page.entries
            if self._last_turn_id is None or entry.turn_id == self._last_turn_id
        )
        scope = (
            f"turn {self._last_turn_id}"
            if self._last_turn_id is not None
            else "the whole page - no turn has run in this console since :resume"
        )
        self._write(
            f"trace of {scope}, session {self.session.session_id}, "
            f"audience={audience.value}:"
        )
        if not entries:
            self._write(
                "  (the projection returned nothing for this scope. transcript_entries is "
                "written by the transcript projection, not by a turn this console ran.)"
            )
        for entry in entries:
            self._render_entry(entry, audience)

        withheld = sorted(
            kind.value for kind in EntryKind if not VISIBILITY[kind][audience]
        )
        self._write(
            f"  withheld from audience={audience.value}: {', '.join(withheld) or 'nothing'}"
        )
        if audience is Audience.USER:
            self._write(
                "  a user sees a pending_placeholder and never a pending_request: THAT "
                "something is pending, never WHICH tool (CLAUDE.md non-negotiable #11). "
                "If a tool name appears above, the projection is leaking and the grid is "
                "not what stopped it."
            )

    def _render_entry(self, entry: TranscriptEntry, audience: Audience) -> None:
        """One timeline row, checked against the grid before a single byte of it prints.

        The check is not belt and braces. `VISIBLE_TO` is what the READER filters with, and
        an operator asking for the user's view is asking whether that filter is right - so
        re-deriving the answer from the grid here is the whole assertion. A row that should
        not be in this projection is reported, loudly, and its payload is NOT printed.
        """
        if not VISIBILITY[entry.kind][audience]:
            self._write(
                f"  ! LEAK: the projection returned a {entry.kind.value} for "
                f"audience={audience.value}, which VISIBILITY forbids. Its payload is not "
                "printed here. Non-negotiable #11 is not holding."
            )
            return

        head = f"  {entry.at.isoformat()}  {entry.kind.value}"
        if entry.cost_usd is not None:
            head = f"{head}  cost={entry.cost_usd}"
        self._write(head)

        payload = entry.payload
        match entry.kind:
            case EntryKind.USER_MESSAGE | EntryKind.AGENT_MESSAGE | EntryKind.REASONING:
                for line in str(payload.get("text", "")).splitlines() or [""]:
                    self._write(f"      {line}")
            case EntryKind.TOOL_CALL:
                self._write(f"      tool {payload.get('tool_name')}")
                self._render_arguments(_as_arguments(payload.get("arguments")))
            case EntryKind.TOOL_RESULT:
                self._render_result(
                    str(payload.get("tool_name", "")), str(payload.get("result", ""))
                )
            case EntryKind.PENDING_PLACEHOLDER:
                self._write(
                    "      something is pending. The tool is deliberately not named: this "
                    "is the substitute a USER is handed instead of the request."
                )
            case _:
                for key in sorted(payload):
                    self._write(f"      {key} = {payload[key]!r}")

    def _render_result(self, tool_name: str, text: str) -> None:
        """A tool's RESULT, and the untrusted boundary drawn as a boundary. t-f11-11.

        UNTRUSTED-CONTENT WRAPPING IS IN CLAUDE.md's SILENT-BUG TABLE: it surfaces only
        under an injection attempt, which means a human looking at the fence is the only
        check it will ever get. So the fence is rendered AS a fence rather than as two
        stray tags inside a wall of text, and the three cases are kept apart:

          - fenced, from an untrusted tool           the boundary is drawn and named
          - NOT fenced, from an untrusted tool       reported as a defect, loudly. Either
                                                     the result skipped the wrapping or a
                                                     new prefix was added without it, and
                                                     both are non-negotiables #4 and #10
                                                     failing silently
          - not fenced, from a local tool            ordinary, and said to be ordinary

        A peer agent's answer is the same case as an MCP result (non-negotiable #10), and
        it reaches here through the same `ask_peer` tool name, so it takes the same path.
        """
        untrusted_tool = is_untrusted(tool_name)
        opened = text.find(UNTRUSTED_OPEN)
        closed = text.rfind(UNTRUSTED_CLOSE)

        if opened == -1 or closed == -1 or closed < opened:
            if untrusted_tool:
                self._write(
                    f"      result from {tool_name} - UNTRUSTED TOOL, AND NO DELIMITERS "
                    "ARE PRESENT. The model was handed this without the boundary "
                    "non-negotiables #4 and #10 require. Treat it as a defect, not as "
                    "formatting."
                )
            else:
                self._write(f"      result from {tool_name} (local tool, not fenced):")
            for line in text.splitlines() or [""]:
                self._write(f"        {line}")
            return

        body = text[opened + len(UNTRUSTED_OPEN) : closed]
        self._write(
            f"      result from {tool_name} - fenced as untrusted before the model saw "
            "it. Everything between the rules below is DATA, never instructions:"
        )
        self._write(f"      +-- {UNTRUSTED_OPEN} " + "-" * 28)
        for line in body.strip("\n").splitlines() or [""]:
            self._write(f"      | {line}")
        self._write(f"      +-- {UNTRUSTED_CLOSE} " + "-" * 28)
        trailing = text[closed + len(UNTRUSTED_CLOSE) :].strip()
        if trailing:
            self._write(f"      (after the fence: {trailing})")

    # -- helpers ------------------------------------------------------------------------

    def _prompt(self) -> str:
        """The agent, the mode, and the caret. t-f11-51.

        THE MODE RIDES ON THE PROMPT BECAUSE THE PROMPT IS THE ONE LINE ALWAYS ON SCREEN.
        A banner is read once; a session is hours long; and the difference between the two
        modes is whether durability, publication and delegation were exercised at all. An
        operator who has to remember which mode they are in will eventually remember wrong,
        and will report a green turn that proved less than they think.
        """
        return f"{self.profile_id} [{self.mode}]> "

    def _identity_line(self) -> str:
        """Who the turns are attributed to. Printed because policy branches on all of it.

        A rule scoped to a channel or a role this identity does not carry simply will not
        match, and "the policy engine is broken" is what that looks like from the outside
        unless the identity is on screen.
        """
        roles = ", ".join(sorted(self._caller.roles)) or "none"
        return (
            f"caller: subject={self._caller.subject_id} tenant={self._caller.tenant_id} "
            f"channel={self._caller.channel} roles={roles}  |  session="
            f"{self.session.session_id}  |  agent={self.profile_id}"
        )


# The two audiences, by the name an operator types. Derived from the enum so a third one
# would appear here the moment it existed - but `:trace` still refuses anything not in it,
# which is the same refusal `VISIBILITY` makes one layer down.
_AUDIENCES: Final[dict[str, Audience]] = {audience.value: audience for audience in Audience}


def _raw_delegation_ask(arguments: Mapping[str, object]) -> str:
    """Rebuild the sentence a peer was actually asked, from a DELEGATION's `arguments`.

    t-f11-46 (2). `PendingRequest.reason` for a DELEGATION is the D17 notice
    (`application/start_turn.py::PEER_SUSPENSION_NOTICE`) for every reader, and that is
    correct there - `StartTurn` has no audience to answer differently for. This console
    does, though: it is the ADMIN surface t-f11-13 built precisely so an operator can read
    what actually happened. The peer and the question are never rewritten - they are the
    RECORD a resumed call keys on, not the message (see `_notice_for`'s docstring) - so
    they are read back here exactly as `adapters/driven/agent_pydantic/runner.py
    ::_pending_reason` first built them from the same two keys, `target` and `question`.

    A MISSING KEY IS NAMED, NOT HIDDEN. `arguments` is a plain `dict[str, object]`
    (I4 - `domain/turn.py` imports nothing external), so nothing here can assume `ask_peer`
    put exactly these two keys there. Printing "an unnamed peer" for a key that is absent
    says the record is short a fact, not that no peer was asked.
    """
    target = arguments.get("target")
    question = arguments.get("question")
    peer = str(target) if target is not None else "an unnamed peer"
    if not question:
        return f"asked {peer} (no question recorded)"
    return f"asked {peer}: {question}"


def _as_arguments(value: object) -> Mapping[str, object]:
    """A payload's `arguments` as a mapping, or an empty one.

    `TranscriptEntry.payload` is deliberately loose (`dict[str, object]`), so what comes
    back under a key is whatever the projection put there. Narrowing it here rather than
    casting keeps a malformed row from crashing a rendering loop an operator is reading.
    """
    return value if isinstance(value, Mapping) else {}


def _verdict(decision: PolicyDecision) -> str:
    rule = decision.rule_id if decision.rule_id is not None else "no matching rule"
    return f"{decision.effect.value:<15} [{rule}]"


def _first_line(text: str) -> str:
    """The first non-empty line of a persona, for a switch summary.

    A persona is a paragraph and a `:use` summary is one screen, so the switch shows enough
    to see that the agent changed. `:profile` prints the whole thing.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= 72 else f"{stripped[:72]}..."
    return "(empty)"
