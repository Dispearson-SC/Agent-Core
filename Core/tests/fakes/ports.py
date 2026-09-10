"""In-memory fakes — one per port.

Deliberately no count in this docstring. The port count has already drifted once (eleven
to fifteen) and left four stale strings behind it across config, docs and this file.
docs/ARCHITECTURE.md section 3 is the single source of truth; everywhere else states the
invariant instead: only two ports change when a vertical is added.


Hermes shipped ~3,821 test files and NOT ONE reusable fake model provider - every test
hand-rolled a MagicMock over the client. The Hermes-Core extraction had to build one
before anything could be tested at all.

We build the fakes FIRST, in F0, for exactly that reason. A fake that records calls is
worth more than a mock that asserts them: tests read better and break less.

Status (t-f0-01): FakeAgentRunner, FakeToolProvider, FakeToolPolicy, FakeConversationStore,
FakeAuditSink and FakeModelGateway are implemented below - enough for every F1 use-case
and adapter test. The remaining fakes stay TODO until their port lands.

FakeAuditSink is the one that matters most: it records ORDER, not merely content. A
compliance record read back in the wrong order tells a different story than the one that
actually happened, and a dict keyed by method name would silently collapse two calls to
the same method into one.
"""

from __future__ import annotations

from decimal import Decimal

from agent_core.domain.compaction import CompactionCheckpoint
from agent_core.domain.media import MediaRef
from agent_core.domain.policy import EFFECT_PRECEDENCE, Effect, PolicyDecision, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    Usage,
)
from agent_core.ports.agent_runner import ToolResolution
from agent_core.ports.model_gateway import RecoveryStrategy


class FakeAgentRunner:
    """Returns a scripted `TurnOutcome` for every `run`/`resume` call; records inputs.

    One fixed outcome is enough for F1: use cases assert on what they SENT (the recorded
    calls), not on the runner producing varied behaviour - that belongs to an integration
    test against the real adapter.
    """

    def __init__(self, outcome: TurnOutcome) -> None:
        self._outcome = outcome
        self.run_calls: list[tuple[TurnId, TurnRequest, AgentProfile, object]] = []
        self.resume_calls: list[
            tuple[TurnId, AgentProfile, object, tuple[ToolResolution, ...]]
        ] = []

    async def run(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        history: object,
    ) -> TurnOutcome:
        self.run_calls.append((turn_id, request, profile, history))
        return self._outcome

    async def resume(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        history: object,
        resolutions: tuple[ToolResolution, ...],
    ) -> TurnOutcome:
        self.resume_calls.append((turn_id, profile, history, resolutions))
        return self._outcome


class FakeToolProvider:
    """A fixed toolset - no discovery, no MCP, exactly what F0 needs."""

    def __init__(self, toolset: object, tool_names: tuple[str, ...]) -> None:
        self._toolset = toolset
        self._tool_names = tool_names
        self.toolset_for_calls: list[AgentProfile] = []
        self.tool_names_for_calls: list[AgentProfile] = []

    async def toolset_for(self, profile: AgentProfile) -> object:
        self.toolset_for_calls.append(profile)
        return self._toolset

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        self.tool_names_for_calls.append(profile)
        return self._tool_names


class FakeToolPolicy:
    """Rules injected in the constructor; `decide`/`filter_toolset` reduce them the same
    way a real adapter must (DENY beats NEEDS_APPROVAL beats ALLOW), so tests exercise
    the real precedence rule against fixtures instead of a hand-rolled shortcut."""

    def __init__(self, rules: RuleSet) -> None:
        self._rules = rules
        self.load_rules_calls: list[CallerIdentity] = []

    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        self.load_rules_calls.append(caller)
        return self._rules

    def filter_toolset(self, rules: RuleSet, tool_names: tuple[str, ...]) -> tuple[str, ...]:
        # Keep everything except DENY - NEEDS_APPROVAL tools stay advertised so the model
        # may still ask for them, per the port's own docstring.
        return tuple(
            name for name in tool_names if self.decide(rules, name, {}).effect is not Effect.DENY
        )

    def decide(
        self,
        rules: RuleSet,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PolicyDecision:
        applicable = rules.applicable(tool_name)
        for effect in EFFECT_PRECEDENCE:
            for rule in applicable:
                if rule.effect is effect:
                    return PolicyDecision(
                        effect=rule.effect, reason=rule.reason, rule_id=rule.rule_id
                    )
        return PolicyDecision(effect=rules.default_effect, reason="no matching rule")


class FakeConversationStore:
    """A list. History is reconstructed from what was appended; checkpoints are kept
    append-only, matching the real port's contract."""

    def __init__(self) -> None:
        self.requests: list[tuple[TurnId, TurnRequest]] = []
        self.outcomes: list[tuple[TurnId, TurnOutcome]] = []
        self.checkpoints: list[CompactionCheckpoint] = []

    async def load_history(self, session: SessionRef) -> object:
        return [request for _, request in self.requests if request.session == session]

    async def append_request(self, turn_id: TurnId, request: TurnRequest) -> None:
        self.requests.append((turn_id, request))

    async def append_outcome(self, turn_id: TurnId, outcome: TurnOutcome) -> None:
        self.outcomes.append((turn_id, outcome))

    async def save_checkpoint(self, checkpoint: CompactionCheckpoint) -> None:
        self.checkpoints.append(checkpoint)

    async def latest_checkpoint(self, session: SessionRef) -> CompactionCheckpoint | None:
        matches = [c for c in self.checkpoints if c.session == session]
        return matches[-1] if matches else None


class AuditCall:
    """One recorded call, tagged by `kind` so order can be asserted without caring about
    the rest of the payload. Deliberately not a dict keyed by kind: a dict would collapse
    two calls of the same kind into one and silently destroy the order this fake exists
    to prove."""

    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: tuple[object, ...]) -> None:
        self.kind = kind
        self.payload = payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"AuditCall({self.kind!r}, {self.payload!r})"


class FakeAuditSink:
    """A list; asserts ORDER, not just content (CLAUDE.md non-negotiable #6, and the
    silent-bug table: untrusted-content and audit ordering never fail a test on their
    own). Every call is appended, never merged or overwritten - the whole point of an
    append-only sink."""

    def __init__(self) -> None:
        self.calls: list[AuditCall] = []

    async def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        self.calls.append(
            AuditCall("tool_call", (turn_id, caller, tool_name, arguments, decision))
        )

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None,
    ) -> None:
        self.calls.append(
            AuditCall("human_decision", (turn_id, tool_call_id, subject_id, approved, note))
        )

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        self.calls.append(AuditCall("media", (turn_id, media, direction)))

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        self.calls.append(AuditCall("turn_end", (turn_id, usage, cost_usd)))


class FakeModelGateway:
    """Day-1 shape: no proxy, an identity model map unless overridden, and a scripted
    `classify_error` verdict - `classify_error` is scheduled for F1 late per the port
    docstring, so a fake just returns whatever the test wired in."""

    def __init__(
        self,
        base_url: str | None = None,
        model_map: dict[str, str] | None = None,
        classification: RecoveryStrategy = RecoveryStrategy.ABORT,
    ) -> None:
        self._base_url = base_url
        self._model_map = model_map or {}
        self._classification = classification
        self.classify_error_calls: list[Exception] = []

    def model_id_for(self, profile_model: str) -> str:
        return self._model_map.get(profile_model, profile_model)

    def base_url(self) -> str | None:
        return self._base_url

    def classify_error(self, error: Exception) -> RecoveryStrategy:
        self.classify_error_calls.append(error)
        return self._classification


# TODO(F3): class FakeHumanGateway      - captures published asks, replays answers
# TODO(F5): class FakeContextEngine     - scripted should_compress/compress
# TODO(F6): class FakeSkillRegistry     - dict of SkillMeta
# TODO(F7): class FakeMediaStore        - dict keyed by sha256
