"""Driving adapter: the operator console - a REPL for inspecting and exercising an agent.

Phase:   F0
Tasks:   docs/TASKS.md#t-f0-07
Status:  IMPLEMENTED - commands, turn rendering and the tool-call view; wired in main.py

WHAT IT IS FOR
    Move between agents, read the configuration the system RESOLVED for each one, see the
    toolset it actually gets, talk to it - and watch how it uses those tools. The last one
    is the point; the rest is navigation.

IT IS A DRIVING ADAPTER, NOT A SECOND COMPOSITION ROOT
    It lives beside `http/`, `channels/`, `workflow/` and `scheduler/` and it calls use
    cases through collaborators handed to it. It imports no concrete adapter: every seat
    below is a port protocol or a domain type, and `main.py` fills them from the ONE
    container `composition.py` built. A console that assembled its own policy engine or
    its own audit sink would be inspecting a system nobody deploys.

IT CALLS `StartTurn` DIRECTLY, NOT THE DBOS WORKFLOW - AND IT SAYS SO OUT LOUD
    A REPL wants an answer now. The 202-plus-poll and the durable per-session partitioned
    queue exist for channels, and going through them here would mean typing a sentence and
    then polling for it. So this path skips them, which means two guarantees are NOT on:

      - durability: a crash mid-turn is not recovered and not replayed (t-f2-02)
      - coalescing: the per-session partition and its window are never exercised

    `_BANNER` prints that at startup. A tool that silently exercises less than the real
    path is how somebody concludes the system works when they have not tested the part
    that breaks - and an operator judging this system from a console deserves to know
    which half of it they are judging.

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

IDENTITY: NOTHING HERE IS AN ADMINISTRATOR
    The console builds a `CallerIdentity` from flags. Non-negotiable #9: `AdminIdentity`
    is never derived from it, and there is no parameter here that one could arrive
    through. Running locally does not widen a chat client into an administrator, so the
    console exposes no knowledge-write, no admin route and no admin anything: those take a
    separate identity by a separate route (`main._env_admin_authenticator`) or they do not
    exist.

CREDENTIALS ARE NEVER PRINTED
    `:profile` renders an MCP server by name and transport and deliberately NOT by url or
    command line. An MCP url is exactly where a token lives in real configuration, and a
    console is a thing people run over a shoulder and paste into tickets.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingRequest,
    SessionRef,
    TurnId,
    TurnOutcome,
    TurnRequest,
    UserInput,
)
from agent_core.ports.tool_policy import ToolPolicy
from agent_core.ports.tool_provider import ToolProvider

__all__ = [
    "Console",
    "ObservedToolCall",
    "ToolCallLog",
    "TurnRunner",
    "new_turn_id",
]


@dataclass(frozen=True, slots=True)
class ObservedToolCall:
    """One tool call a turn made, as the append-only trail recorded it.

    THE RECORDED VERDICT, NOT A RE-DECIDED ONE. `effect` and `rule_id` are what the policy
    engine answered AT THE TIME the call was attempted, which is the only answer worth
    showing: asking the engine again now would quietly report today's rules against
    yesterday's call, and the two differ precisely on the day somebody edits a rule to
    explain an incident.

    THERE IS NO `reason` FIELD, AND ITS ABSENCE IS A FINDING RATHER THAN A CHOICE.
        `PolicyDecision.reason` is the sentence the model is handed on DENY and the human
        is asked on NEEDS_APPROVAL - and `audit_tool_calls` has no column for it
        (migration `0005_audit_tool_calls`: turn_id, caller, tool, arguments, effect,
        rule_id, at). So the trail can tell an auditor WHICH rule fired and never WHAT it
        said. `:policy <tool>` asks the live engine for the sentence instead, which is
        honest about being a question about the rules as they read now.

    `arguments` are whatever the sink stored, i.e. already redacted by
    `PgAuditSink._redact`. The console does not re-redact: two redaction paths drift, and
    the drifting one is always the one nobody reads until an incident.
    """

    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    effect: Effect = Effect.DENY
    rule_id: str | None = None

    @property
    def executed(self) -> bool:
        """Whether the tool body actually ran.

        `PolicyEnforcement.before_tool_execute` raises `SkipToolExecution` whenever the
        decision is not ALLOW, so this mirrors `PolicyDecision.blocks_execution` rather
        than restating a list of blocking effects - an effect added to the enum later must
        read as "did not run" here, not fall through to "ran" unnoticed.
        """
        return self.effect is Effect.ALLOW


class ToolCallLog(Protocol):
    """The audit READ the console renders a turn from.

    A protocol rather than a concrete reader because there is no audit-read PORT: `AuditSink`
    is write-only by design and `TranscriptReader` projects a `transcript_entries` table
    nothing in the tree writes to yet. So the console declares the one question it needs
    answered, and `main.py` binds it to a read of the same `audit_tool_calls` rows
    `PgAuditSink` wrote - the same shape `composition._pg_requester_lookup` already uses
    for the same table.
    """

    async def for_turn(self, turn_id: TurnId) -> tuple[ObservedToolCall, ...]:
        """Every tool call filed under `turn_id`, oldest first. Empty is a normal answer."""
        ...


class TurnRunner(Protocol):
    """What the console needs from `StartTurn`, and nothing else.

    Narrowed on purpose: a console that held the whole use case could reach its store or
    its audit sink and start answering questions from the side rather than from the turn.
    """

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome: ...


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
    (":profile", "the RESOLVED configuration of the current agent"),
    (":tools", "the toolset this profile gets, and what policy does to each name"),
    (":policy [tool]", "what the engine decides for this caller, and which rule wins"),
    (":audit", "the tool calls of the last turn, with decision and rule_id"),
    (":help", "this list"),
    (":quit", "leave"),
)

_BANNER: Final[tuple[str, ...]] = (
    "agent-core console - an ADMIN-audience operator surface.",
    "",
    "This mode calls StartTurn DIRECTLY, not the DBOS workflow. Two guarantees are",
    "therefore NOT exercised here, and a turn that works here proves nothing about them:",
    "  - durability: a crash mid-turn is not recovered and not replayed",
    "  - coalescing: the per-session partitioned queue and its window never run",
    "Everything that decides a refusal IS real: the profile, the tool provider, the policy",
    "engine and the audit sink are the ones this deployment was built with.",
    "",
    "ADMIN audience: a suspension is shown with its tool name and tool_call_id. A USER",
    "sees only THAT something is pending (CLAUDE.md non-negotiable #11).",
    "",
    "Type :help for commands. Plain text is a turn.",
)

# How much of one argument value to show inline. The full payload belongs to the audit
# table; this line exists so an operator can tell two calls apart at a glance.
_ARGUMENT_CHARS: Final[int] = 48


class UnknownConsoleProfileError(KeyError):
    """`--profile` names no loaded profile.

    A `KeyError` subclass for the same reason `StartTurn.UnknownProfileError` is one, and
    refused for the same reason: a session that silently falls back to some other agent
    attributes every answer in it to the wrong profile.
    """


class Console:
    """The REPL. One agent at a time, one session for the whole run.

    INPUT AND OUTPUT ARE SEAMS, WHICH IS WHAT MAKES IT TESTABLE
        A REPL that can only be driven by a human typing at it is a REPL nobody tests, and
        this is the surface an operator forms their opinion of the system from.
        `read_line` returns `None` at end of input - a piped script, or ctrl-D.

    THE SESSION IS FIXED FOR THE RUN so history accumulates across turns exactly as it does
    for a real conversation. Switching agent with `:use` deliberately does NOT start a new
    session: watching one agent pick up the thread another one left is an operator question
    worth being able to ask, and `TurnRequest` carries the profile per turn anyway.
    """

    def __init__(
        self,
        *,
        profiles: Mapping[str, AgentProfile],
        tools: ToolProvider,
        policy: ToolPolicy,
        start_turn: TurnRunner,
        tool_calls: ToolCallLog,
        caller: CallerIdentity,
        session: SessionRef,
        write_line: Callable[[str], None],
        read_line: Callable[[str], str | None],
        profile_id: str | None = None,
        new_turn_id: Callable[[], TurnId] = new_turn_id,
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
        self._tool_calls = tool_calls
        self._caller = caller
        self._session = session
        self._write = write_line
        self._read = read_line
        self._new_turn_id = new_turn_id

        self.profile_id: str = resolved
        self._last_turn_id: TurnId | None = None
        self._last_calls: tuple[ObservedToolCall, ...] = ()

    # -- the loop -----------------------------------------------------------------------

    async def run(self) -> None:
        """Print the banner, then read-eval-print until `:quit` or end of input.

        `read_line` is called directly rather than through `asyncio.to_thread`. Blocking
        the loop while a person types is correct HERE and nowhere else in this codebase:
        console mode starts no DBOS worker and serves no HTTP, so there is no other turn in
        flight for the wait to starve. The moment this process grows a background task, the
        read has to move off the loop.
        """
        for line in _BANNER:
            self._write(line)
        self._write(self._identity_line())
        self._write("")

        while True:
            entered = self._read(f"{self.profile_id}> ")
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
                case ":profile":
                    self._profile()
                case ":tools":
                    await self._tools_command()
                case ":policy":
                    await self._policy_command(argument or None)
                case ":audit":
                    self._audit()
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
        self._write(
            f"  model:     {previous.model} -> {target.model}"
        )
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

    # -- inspection ---------------------------------------------------------------------

    def _profile(self) -> None:
        """The RESOLVED configuration, which is the whole point - not the raw YAML.

        Every block a profile file may omit is printed with the value the loader settled
        on, because "does this agent have knowledge retrieval" must have an answer even
        when the file says nothing about it.
        """
        profile = self._profiles[self.profile_id]
        self._write(
            f"profile {profile.id}  v{profile.version}  "
            f"content={profile.content_hash[:12]}"
        )
        self._write("  persona:")
        for line in profile.persona.splitlines():
            self._write(f"    {line}")
        self._write(f"  model:            {profile.model}")
        self._write(f"  max_iterations:   {profile.max_iterations}")
        self._write(f"  max_cost_usd:     {profile.max_cost_usd}")
        self._write(f"  toolsets:         {', '.join(profile.toolsets) or 'none'}")
        self._write(
            f"  skill_namespaces: {', '.join(profile.skill_namespaces) or 'none'}"
        )

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
        self._write(
            f"  peers:            enabled={peers.enabled} max_hops={peers.max_hops} "
            f"visibility={peers.visibility.value}"
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
            self._write(f"  {name:<28} {_verdict(decision)}")

        advertised = self._policy.filter_toolset(rules, names)
        self._write(
            f"  advertised to the model: {len(advertised)} of {len(names)} "
            f"(a {Effect.DENY.value} tool is never shown to it)"
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

    def _audit(self) -> None:
        if self._last_turn_id is None:
            self._write("no turn has run yet in this session, so there is nothing to show.")
            return
        self._write(f"tool calls of turn {self._last_turn_id}:")
        self._render_tool_calls(self._last_calls)
        self._write(
            "  (arguments are the redacted copy the sink stored - PgAuditSink keeps only "
            "the fields a tool opted into - so an empty argument list does not mean the "
            "model passed none)"
        )

    def _help(self) -> None:
        self._write("commands:")
        width = max(len(name) for name, _ in _COMMANDS)
        for name, description in _COMMANDS:
            self._write(f"  {name:<{width}}  {description}")
        self._write("  anything else     a turn: it goes to the current agent")

    # -- a turn -------------------------------------------------------------------------

    async def _turn(self, text: str) -> None:
        turn_id = self._new_turn_id()
        request = TurnRequest(
            session=self._session,
            caller=self._caller,
            profile_id=self.profile_id,
            input=UserInput(text=text),
        )
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
            self._last_calls = await self._tool_calls.for_turn(turn_id)
        except Exception as error:
            self._last_calls = ()
            trail_error = f"{type(error).__name__}: {error}"

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

    def _render_tool_calls(self, calls: tuple[ObservedToolCall, ...]) -> None:
        """Name, arguments in brief, the decision, and whether it ran.

        "Whether it ran" is a SEPARATE line from the effect on purpose. They are two facts
        and today they agree; the day `NEEDS_APPROVAL` stops refusing and starts suspending
        (`runner.py`'s TODO(F3)) they stop agreeing, and an operator reading only the effect
        would keep believing the old story.
        """
        if not calls:
            self._write("  (no tool calls)")
            return
        for call in calls:
            ran = "executed" if call.executed else "NOT EXECUTED"
            rule = call.rule_id if call.rule_id is not None else "no matching rule"
            self._write(
                f"  tool {call.tool_name}({_brief(call.arguments)}) "
                f"-> {call.effect.value} [{rule}] - {ran}"
            )
            if call.effect is Effect.NEEDS_APPROVAL:
                self._write(
                    "       needs_approval blocks the call this turn; suspending on it "
                    "instead is not wired yet (F3)."
                )

    def _render_suspension(self, pending: tuple[PendingRequest, ...]) -> None:
        """ADMIN audience - see the module docstring.

        The tool name and the `tool_call_id` are shown because answering the suspension is
        the operator's job and neither can be guessed. A USER-audience view of the same
        turn shows THAT something is pending and nothing else.
        """
        self._write(f"SUSPENDED - waiting on {len(pending)} request(s):")
        for request in pending:
            self._write(
                f"  {request.kind.value}  tool={request.tool_name}  "
                f"tool_call_id={request.tool_call_id}"
            )
            self._write(f"      {request.reason}")
            self._write(f"      arguments: {_brief(request.arguments)}")
        self._write(
            "  (this view is ADMIN audience: a user is told only that something is "
            "pending, never which tool)"
        )
        self._write(
            "  answering it is not a console command: an approval is decided through "
            "POST /decisions/{corr_id} and evidence through POST /evidence/{corr_id}."
        )

    # -- helpers ------------------------------------------------------------------------

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
            f"{self._session.session_id}  |  agent={self.profile_id}"
        )


def _verdict(decision: PolicyDecision) -> str:
    rule = decision.rule_id if decision.rule_id is not None else "no matching rule"
    return f"{decision.effect.value:<15} [{rule}]"


def _brief(arguments: Mapping[str, object]) -> str:
    """Arguments, short enough to read in a terminal and long enough to tell two apart.

    The full payload is in `audit_tool_calls`; this is a glance, not a record.
    """
    if not arguments:
        return ""
    parts = []
    for key in sorted(arguments):
        value = repr(arguments[key])
        if len(value) > _ARGUMENT_CHARS:
            value = f"{value[:_ARGUMENT_CHARS]}..."
        parts.append(f"{key}={value}")
    return ", ".join(parts)


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
