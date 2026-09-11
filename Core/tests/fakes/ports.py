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

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from agent_core.domain.compaction import CompactionCheckpoint
from agent_core.domain.media import MediaId, MediaKind, MediaRef
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
from agent_core.ports.embedder import Embedding
from agent_core.ports.media_store import SignedMedia
from agent_core.ports.model_gateway import ModelAttempt, RecoveryStrategy


class FakeAgentRunner:
    """Returns a scripted `TurnOutcome` for every `run`/`resume` call; records inputs.

    One fixed outcome is enough for F1: use cases assert on what they SENT (the recorded
    calls), not on the runner producing varied behaviour - that belongs to an integration
    test against the real adapter.
    """

    def __init__(self, outcome: TurnOutcome) -> None:
        self._outcome = outcome
        self.run_calls: list[tuple[TurnId, TurnRequest, AgentProfile, object]] = []
        # SIX fields, not four: `caller` and `session` are the t-f3-16 seats, and they are
        # recorded in the SAME tuple as the rest rather than in a parallel list. A second
        # list indexed by position is one append away from disagreeing with this one, and
        # the whole question these seats exist to answer is "which identity was this call
        # made under" - which a desynchronised pair answers confidently and wrongly.
        self.resume_calls: list[
            tuple[
                TurnId,
                AgentProfile,
                object,
                tuple[ToolResolution, ...],
                CallerIdentity,
                SessionRef,
            ]
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
        *,
        caller: CallerIdentity,
        session: SessionRef,
    ) -> TurnOutcome:
        """The port's settled arity, seats included (docs/TASKS.md#t-f3-16).

        A fake that kept the pre-widening signature would still satisfy every caller that
        had not been updated, so the port would have moved and nothing would say so - which
        is the shape `test_fakes.py`'s type check exists to refuse.
        """
        self.resume_calls.append((turn_id, profile, history, resolutions, caller, session))
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

    async def record_rejected_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        reason: str,
    ) -> None:
        """Its OWN kind, never folded into `human_decision` (docs/DECISIONS.md#d25).

        A test that asserted on `human_decision` alone would pass whether a refusal was
        recorded as a refusal or as a decision-not-to-approve, which is the one
        distinction the two members exist to keep.
        """
        self.calls.append(
            AuditCall("rejected_decision", (turn_id, tool_call_id, subject_id, reason))
        )

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        self.calls.append(AuditCall("media", (turn_id, media, direction)))

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        self.calls.append(AuditCall("turn_end", (turn_id, usage, cost_usd)))


class FakeModelGateway:
    """Day-1 shape: no proxy, an identity model map unless overridden, and a scripted
    `classify_error` verdict - the real classification is evidence-driven, so a fake just
    returns whatever the test wired in.

    `classify_error` takes the `ModelAttempt` t-later-03 added to the port. The attempt is
    recorded ALONGSIDE the error rather than discarded: it is the only thing that
    distinguishes ROTATE_KEY from FALLBACK_MODEL, so a fake that swallowed it would let a
    caller forget to pass the evidence and still look correct."""

    def __init__(
        self,
        base_url: str | None = None,
        model_map: dict[str, str] | None = None,
        classification: RecoveryStrategy = RecoveryStrategy.ABORT,
    ) -> None:
        self._base_url = base_url
        self._model_map = model_map or {}
        self._classification = classification
        self.classify_error_calls: list[tuple[Exception, ModelAttempt]] = []

    def model_id_for(self, profile_model: str) -> str:
        return self._model_map.get(profile_model, profile_model)

    def base_url(self) -> str | None:
        return self._base_url

    def classify_error(self, error: Exception, attempt: ModelAttempt) -> RecoveryStrategy:
        self.classify_error_calls.append((error, attempt))
        return self._classification


class FakeEmbedder:
    """`Embedder` (t-d2-07): a deterministic vector, and it records what it was asked to
    embed.

    NO RANDOMNESS AND NO HASHING THAT MOVES BETWEEN RUNS
        The axis is derived from the text's code points, so the same text always embeds to
        the same point and two different texts usually do not. `hash()` would have been
        shorter and is salted per process for `str`, which would make a ranking assertion
        pass or fail depending on the interpreter that ran it - the flakiest possible shape
        for a test double.

    IT DOES NOT UNDERSTAND ANYTHING, AND NO TEST MAY PRETEND IT DOES
        Nearness here is an arithmetic coincidence of the code points, not meaning. A test
        proving "SEMANTIC finds documents by meaning" must place the vectors itself; what
        this fake proves is the property the ADAPTER owns - that a vector was asked for,
        that it reached the query, and that the model id travelled with it.

    `failure` makes the model call raise, which is the case the write side has to survive:
    a document stored and never embedded is invisible to SEMANTIC retrieval, so
    `EmbeddingKnowledgeAdmin` must refuse loudly rather than report success.
    """

    def __init__(
        self,
        model: str = "fake/embedding-1",
        dimensions: int = 4,
        failure: Exception | None = None,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.failure = failure
        self.embedded: list[str] = []

    async def embed(self, text: str) -> Embedding:
        self.embedded.append(text)
        if self.failure is not None:
            raise self.failure
        values = [0.0] * self.dimensions
        values[sum(ord(character) for character in text) % self.dimensions] = 1.0
        return Embedding(model=self.model, vector=tuple(values))


@dataclass
class MediaPutCall:
    """One recorded `put`, kept separate from `get`/`signed_url` calls (below) because the
    anchor assertion this fake exists for - `t-f7-03`'s size-before-sniff-before-store
    ordering - is entirely about whether `put` was ever reached. `calls == []` is the
    externally visible difference between checking size first and checking it after the
    bytes are already on disk; folding every member into one list would blur that."""

    data: bytes
    kind: MediaKind
    mime_type: str
    filename: str | None


class FakeMediaStore:
    """The one `MediaStore` stand-in for the whole suite (docs/TASKS.md#t-f7-02,
    tests/unit/test_ports_media_store.py's `test_exactly_one_media_store_fake_exists_...`).

    Four modules each hand-rolled a look-alike before this existed, and `signed_url`
    widening from `str` to `SignedMedia` (t-f7-10) broke every one of them separately -
    docs/WAVES.md rule 4. This is the promoted survivor: `put` computes a real sha256 and
    remembers the ref it issued, so a later `get`/`signed_url` on that same id resolves
    without a caller having to pre-register anything; `blobs`/`refs` let a caller preload
    media a resumed turn expects to already exist, without ever having called `put` for it
    inside this fake.

    Three call lists, not one: `calls` (put), `get_calls`, `signed_url_calls`. Collapsing
    them would hide exactly the distinction `ResumeTurn`'s tests need - which resolution
    path a delivery mode actually took, BYTES or SIGNED_URL.
    """

    def __init__(
        self,
        blobs: dict[MediaId, bytes] | None = None,
        refs: dict[MediaId, MediaRef] | None = None,
    ) -> None:
        self.calls: list[MediaPutCall] = []
        self.get_calls: list[MediaId] = []
        self.signed_url_calls: list[MediaId] = []
        self._blobs: dict[MediaId, bytes] = dict(blobs) if blobs else {}
        self._refs: dict[MediaId, MediaRef] = dict(refs) if refs else {}

    async def put(
        self,
        data: bytes,
        *,
        kind: MediaKind,
        mime_type: str,
        filename: str | None = None,
    ) -> MediaRef:
        self.calls.append(MediaPutCall(data, kind, mime_type, filename))
        digest = hashlib.sha256(data).hexdigest()
        media_id = MediaId(f"m-{digest[:12]}")
        ref = MediaRef(
            media_id=media_id,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=digest,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            filename=filename,
        )
        self._blobs[media_id] = data
        self._refs[media_id] = ref
        return ref

    async def get(self, media_id: MediaId) -> bytes:
        self.get_calls.append(media_id)
        return self._blobs[media_id]

    async def signed_url(self, media_id: MediaId, *, ttl_seconds: int = 300) -> SignedMedia:
        """URL AND REF together (t-f7-10) - the ref this fake already held for `media_id`,
        never one reconstructed from the string, which would be a guess."""
        self.signed_url_calls.append(media_id)
        return SignedMedia(
            url=f"https://example.invalid/{media_id}?ttl={ttl_seconds}",
            ref=self._refs[media_id],
        )


# TODO(F3): class FakeHumanGateway      - captures published asks, replays answers
# TODO(F5): class FakeContextEngine     - scripted should_compress/compress
# TODO(F6): class FakeSkillRegistry     - dict of SkillMeta
