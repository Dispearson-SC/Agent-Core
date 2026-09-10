"""AgentProfile - how a generic core becomes a specific agent.

Phase:   F1 (shape) / F4 (first real profile) / F6 (skills, mcp) / F7 (media)
Tasks:   docs/TASKS.md#t-f1-04, docs/TASKS.md#t-f4-02
Status:  TYPES DEFINED / APPROVAL RULES EVALUATED (F4) / F6 AND F7 FIELDS SHAPE-ONLY

THE CONTRACT THIS FILE EXISTS TO SERVE
    Adding a vertical is ONE profile file plus ONE tools package.
    Zero changes to domain/, application/ or ports/.

    An agent is DATA, not a subclass. If you ever find yourself writing
    `class FraudAgent(Agent)`, stop: the port cut is wrong and CLAUDE.md says to revisit
    it before continuing.

WHERE PROFILES LIVE
    Core/profiles/<id>.yaml, read by adapters/driven/profiles_fs/ and wired by the
    composition root. YAML rather than Python so a non-engineer can read a profile and see
    what an agent is allowed to do - which is the whole point of having a governance layer.

    THE FILE AND THE PARSER ARE NOT THIS MODULE'S BUSINESS. `domain/` imports nothing
    external (CLAUDE.md, "Layer rules") and has no I/O to await (D13), so it never sees a
    path and never sees YAML: the adapter reads the bytes, parses them, and hands
    `from_mapping` a plain mapping. Every rule about what a profile MAY SAY still lives
    here, which is what keeps the validation testable without a filesystem.

HOW TO ADD A VERTICAL (the only procedure anyone should need)
    1. Write Core/profiles/<id>.yaml.
    2. Write Core/src/agent_core/adapters/driven/tools/<vertical>/.
    3. Add policy rows for the new tool names.
    4. Run the contract test. If it reports a diff under domain/, application/ or ports/,
       something is wrong with the design, not with the test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any

from agent_core.domain.compaction import CompactionPolicy
from agent_core.domain.knowledge import KnowledgePolicy, RetrievalMode
from agent_core.domain.media import MediaDelivery, MediaKind, MediaPolicy
from agent_core.domain.peers import PeerPolicy, PeerVisibility


class ProfileValidationError(ValueError):
    """A profile YAML file is malformed, or carries a key nothing declared.

    Raised instead of silently absorbing the key: `AgentProfile` deliberately has no
    free-form `extra: dict` (see the class docstring), so an unrecognised key is either a
    typo or an attempt to smuggle behaviour through configuration - both are bugs, and
    both must be loud."""


def _require_mapping(data: object, *, where: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProfileValidationError(f"{where} must be a mapping, got {type(data).__name__}")
    return data


def _reject_unknown_keys(data: dict[str, Any], allowed: set[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ProfileValidationError(f"Unknown key(s) in {where}: {joined}")


@dataclass(frozen=True, slots=True)
class MCPServerRef:
    """One MCP server this profile may borrow tools from.

    `transport` is one of stdio | http | sse - the three Pydantic AI's `MCPToolset`
    speaks.

    `tool_include` / `tool_exclude` are trailing-wildcard patterns applied at DISCOVERY
    time. They are a scope reducer, NOT a security boundary: the security boundary is
    `ToolPolicy`, which every MCP tool passes through exactly like a local one.
    CLAUDE.md non-negotiable #4.

    `result_budget_chars` defaults lower than local tools on purpose. Hermes uses 50K for
    mcp_* against 100K for local, with the stated reason that MCP servers routinely
    return un-paginated 20-50K payloads."""

    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    tool_include: tuple[str, ...] = ()
    tool_exclude: tuple[str, ...] = ()
    result_budget_chars: int = 50_000


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """A profile-level rule that forces a human decision.

    Distinct from `PolicyRule` on purpose. `PolicyRule` answers "may this caller use this
    tool at all" and is stored per tenant. `ApprovalRule` answers "does this SPECIFIC
    invocation need a human", and it can inspect ARGUMENTS - a price change above 15%, a
    transfer above a threshold.

    `condition` is a mini-expression over the tool arguments, and it is deliberately tiny:

        <field> <operator> <literal>          e.g.  amount > 1000
        abs(<field>) <operator> <literal>     e.g.  abs(pct_change) > 15

    operators   == != >= <= > <
    literals    numbers, 'quoted strings', true, false, null

    There is no `and`, no `or`, no arithmetic and no function but `abs`. That is not an
    unfinished grammar, it is the grammar: a profile file is configuration, and
    configuration that executes code is a remote-code-execution vector the moment profiles
    become editable by anyone but us. Nothing here reaches `eval`, and no operand is ever
    handed to Python's own comparison machinery unless it is a number, a string, a boolean
    or `None` (see `_evaluate_condition`).

    Anything this grammar cannot decide - a syntax error, a field the call never passed, a
    comparison with no answer - means APPROVE, never "no match". `requires_approval_for`
    owns that rule and `tests/unit/test_approval_rules.py` holds it there.

    `tool_name` matches like `PolicyRule.tool_pattern`: exact, or a trailing `*`, both
    case-insensitive.
    """

    tool_name: str
    reason: str
    condition: str | None = None

    def matches_tool(self, tool_name: str) -> bool:
        pattern = self.tool_name.lower()
        name = tool_name.lower()
        if pattern.endswith("*"):
            return name.startswith(pattern[:-1])
        return name == pattern


class _UnreadableCondition(Exception):
    """This condition cannot be decided, so the rule that carries it matches.

    Private, and never leaves the module: `requires_approval_for` turns it into a returned
    `ApprovalRule`. It is an internal control signal, not an error a caller handles - a
    profile with a typo in it must still serve turns, just more cautiously."""


# Two-character operators are listed before the one-character ones they start with, and
# `_split_condition` tries them in this order at each position. Reversing the two lines
# would read `>=` as `>` followed by a literal `= 15`, which happens to fail closed here
# but only by luck.
_COMPARISONS: tuple[str, ...] = ("==", "!=", ">=", "<=", ">", "<")

_ABS_PREFIX = "abs("


def _split_condition(condition: str) -> tuple[str, str, str]:
    """`"abs(x) > 15"` -> `("abs(x)", ">", "15")`. Leftmost operator wins."""
    for index in range(len(condition)):
        for operator in _COMPARISONS:
            if not condition.startswith(operator, index):
                continue
            left = condition[:index].strip()
            right = condition[index + len(operator) :].strip()
            if not left or not right:
                raise _UnreadableCondition(condition)
            return left, operator, right
    raise _UnreadableCondition(condition)


def _read_field(text: str) -> tuple[str, bool]:
    """Return the argument name and whether `abs()` wraps it."""
    absolute = text.lower().startswith(_ABS_PREFIX)
    if absolute:
        if not text.endswith(")"):
            raise _UnreadableCondition(text)
        text = text[len(_ABS_PREFIX) : -1].strip()
    if not text.isidentifier():
        raise _UnreadableCondition(text)
    return text, absolute


def _read_literal(text: str) -> object:
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return _decimal(text)


def _decimal(text: str) -> Decimal:
    """Parse a numeric LITERAL - the right-hand side, which is text by nature.

    `Decimal` rather than `float` because a threshold in a profile is written in decimal
    ("15", "0.15", a currency amount) and a binary float does not hold those exactly.

    Non-finite values are rejected because EVERY comparison against `NaN` is `False` - a
    condition that silently never matches is precisely the fail-open this module exists to
    prevent, and `Decimal("nan")` parses happily."""
    try:
        number = Decimal(text)
    except (ValueError, ArithmeticError):
        raise _UnreadableCondition(text) from None
    if not number.is_finite():
        raise _UnreadableCondition(text)
    return number


def _as_number(value: object) -> Decimal:
    """A runtime ARGUMENT as a number, or a refusal to decide.

    A numeric string is refused rather than coerced. `{"pct_change": "3"}` against
    `abs(pct_change) > 15` would coerce to a confident "no approval needed", and the whole
    point of this module is that it does not guess in that direction. A caller passing a
    number as text gets an approval prompt, which a human can resolve; the alternative is
    an unattended tool call decided by a coercion nobody wrote down.

    `bool` is excluded too, even though it is an `int` in Python: `dry_run > 0` is not a
    question this grammar answers, and letting `True` become `1` would answer it anyway."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise _UnreadableCondition(value)
    return _decimal(str(value))


def _category(value: object) -> str:
    """The literal shape of a value, or `_UnreadableCondition` if it has none.

    This is what keeps an arbitrary object out of `==`. An argument the grammar has no
    literal for is undecidable, so it means approve - it is never compared."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    raise _UnreadableCondition(value)


def _evaluate_condition(condition: str, arguments: Mapping[str, object]) -> bool:
    """Decide one condition against one call's arguments, or refuse to decide it."""
    left_text, operator, right_text = _split_condition(condition.strip())
    field, absolute = _read_field(left_text)
    expected = _read_literal(right_text)

    if field not in arguments:
        # The rule names an argument this call did not pass. Reading that as "absent,
        # therefore not above the threshold" is the permissive default t-f4-02 forbids.
        raise _UnreadableCondition(field)
    actual = arguments[field]

    if absolute:
        return _compare(abs(_as_number(actual)), operator, _as_number(expected))

    if operator in {"==", "!="}:
        # Comparing across shapes ("north" == 15) has no useful answer, so it gets the
        # cautious one rather than a plain False.
        if _category(actual) != _category(expected):
            raise _UnreadableCondition(condition)
        equal = actual == expected
        return equal if operator == "==" else not equal

    return _compare(_as_number(actual), operator, _as_number(expected))


def _compare(actual: Decimal, operator: str, expected: Decimal) -> bool:
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    raise _UnreadableCondition(operator)


def _build_mcp_server_ref(data: object) -> MCPServerRef:
    mapping = _require_mapping(data, where="mcp_servers[]")
    _reject_unknown_keys(mapping, {f.name for f in fields(MCPServerRef)}, where="mcp_servers[]")
    return MCPServerRef(
        name=mapping["name"],
        transport=mapping["transport"],
        command=mapping.get("command"),
        args=tuple(mapping.get("args", ())),
        url=mapping.get("url"),
        tool_include=tuple(mapping.get("tool_include", ())),
        tool_exclude=tuple(mapping.get("tool_exclude", ())),
        result_budget_chars=mapping.get("result_budget_chars", 50_000),
    )


def _build_approval_rule(data: object) -> ApprovalRule:
    mapping = _require_mapping(data, where="approval_rules[]")
    _reject_unknown_keys(mapping, {f.name for f in fields(ApprovalRule)}, where="approval_rules[]")
    return ApprovalRule(
        tool_name=mapping["tool_name"],
        reason=mapping["reason"],
        condition=mapping.get("condition"),
    )


def _build_compaction_policy(data: object) -> CompactionPolicy:
    mapping = _require_mapping(data, where="compaction")
    # `enabled_rungs` has no loader yet - it needs a Rung-by-name lookup that belongs
    # with the F5 compaction work, not this loading task. Excluding it from `allowed`
    # means a profile that sets it fails loudly instead of the setting being dropped.
    allowed = {f.name for f in fields(CompactionPolicy)} - {"enabled_rungs"}
    _reject_unknown_keys(mapping, allowed, where="compaction")
    defaults = CompactionPolicy()
    return CompactionPolicy(
        trigger_fraction=float(mapping.get("trigger_fraction", defaults.trigger_fraction)),
        target_fraction=float(mapping.get("target_fraction", defaults.target_fraction)),
        head_exchanges=int(mapping.get("head_exchanges", defaults.head_exchanges)),
        tail_tokens=int(mapping.get("tail_tokens", defaults.tail_tokens)),
        summariser_model=mapping.get("summariser_model", defaults.summariser_model),
    )


def _build_media_policy(data: object) -> MediaPolicy:
    mapping = _require_mapping(data, where="media")
    _reject_unknown_keys(mapping, {f.name for f in fields(MediaPolicy)}, where="media")
    defaults = MediaPolicy()
    accepted_kinds = mapping.get("accepted_kinds")
    return MediaPolicy(
        accepted_kinds=(
            frozenset(MediaKind(kind) for kind in accepted_kinds)
            if accepted_kinds is not None
            else defaults.accepted_kinds
        ),
        delivery=MediaDelivery(mapping.get("delivery", defaults.delivery)),
        max_bytes=int(mapping.get("max_bytes", defaults.max_bytes)),
        allow_evidence_requests=bool(
            mapping.get("allow_evidence_requests", defaults.allow_evidence_requests)
        ),
        allow_speech_output=bool(mapping.get("allow_speech_output", defaults.allow_speech_output)),
    )


def _build_knowledge_policy(data: object) -> KnowledgePolicy:
    mapping = _require_mapping(data, where="knowledge")
    allowed = {f.name for f in fields(KnowledgePolicy)}
    _reject_unknown_keys(mapping, allowed, where="knowledge")
    defaults = KnowledgePolicy()
    return KnowledgePolicy(
        enabled=bool(mapping.get("enabled", defaults.enabled)),
        collections=tuple(mapping.get("collections", defaults.collections)),
        mode=RetrievalMode(mapping.get("mode", defaults.mode)),
        top_k=int(mapping.get("top_k", defaults.top_k)),
        min_score=float(mapping.get("min_score", defaults.min_score)),
        max_context_chars=int(mapping.get("max_context_chars", defaults.max_context_chars)),
        inject_into_prompt=bool(mapping.get("inject_into_prompt", defaults.inject_into_prompt)),
    )


def _build_peer_policy(data: object) -> PeerPolicy:
    mapping = _require_mapping(data, where="peers")
    # `peers` (the list of AgentRef) has no loader yet - F9, docs/TASKS.md#t-f9-01.
    # Excluding it from `allowed` means a profile that sets it fails loudly instead of
    # the setting being dropped.
    allowed = {f.name for f in fields(PeerPolicy)} - {"peers"}
    _reject_unknown_keys(mapping, allowed, where="peers")
    defaults = PeerPolicy()
    return PeerPolicy(
        enabled=bool(mapping.get("enabled", defaults.enabled)),
        max_hops=int(mapping.get("max_hops", defaults.max_hops)),
        visibility=PeerVisibility(mapping.get("visibility", defaults.visibility)),
        reply_timeout_seconds=int(
            mapping.get("reply_timeout_seconds", defaults.reply_timeout_seconds)
        ),
    )


# `version` and `content_hash` are DERIVED at load time, never authored. Excluding them
# from the accepted YAML keys means a profile that tries to pin its own version fails
# loudly, exactly like any other unknown key - a hand-written version is a lie the audit
# trail would then have to carry forever.
_DERIVED_FIELDS = frozenset({"version", "content_hash"})


def _canonical(value: object) -> str:
    """A stable textual form of a resolved profile value.

    Hashing this rather than the raw file means reformatting, reordering or commenting a
    YAML file does not bump the version, while any change to what the agent is actually
    allowed to do does. D20 allows hashing the file; hashing the RESOLVED profile is the
    same guarantee without the false positives.

    Order matters and is preserved for tuples, because `toolsets` order is meaningful.
    Sets are sorted, because theirs is not."""
    if is_dataclass(value) and not isinstance(value, type):
        parts = [
            f"{f.name}={_canonical(getattr(value, f.name))}"
            for f in fields(value)
            if f.name not in _DERIVED_FIELDS
        ]
        return f"{type(value).__name__}({','.join(parts)})"
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.value}"
    if isinstance(value, (frozenset, set)):
        return "{" + ",".join(sorted(_canonical(item) for item in value)) + "}"
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return f"{type(value).__name__}:{value!r}"


def profile_content_hash(profile: AgentProfile) -> str:
    """The identity of a profile's CONTENT, ignoring whatever version it carries."""
    return hashlib.sha256(_canonical(profile).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """The complete definition of one agent.

    Every field is either a capability grant or a limit. There is deliberately no
    free-form `extra: dict` - the moment one exists, verticals start smuggling behaviour
    through it and the contract quietly dies."""

    id: str
    persona: str
    model: str

    toolsets: tuple[str, ...] = ()
    mcp_servers: tuple[MCPServerRef, ...] = ()
    skill_namespaces: tuple[str, ...] = ()

    max_iterations: int = 25
    max_cost_usd: Decimal = Decimal("1.00")

    approval_rules: tuple[ApprovalRule, ...] = ()
    compaction: CompactionPolicy = field(default_factory=CompactionPolicy)
    media: MediaPolicy = field(default_factory=MediaPolicy)
    # Both default to disabled. "Only certain agents get this" is a line in a YAML file,
    # never a branch in core code.
    knowledge: KnowledgePolicy = field(default_factory=KnowledgePolicy)
    peers: PeerPolicy = field(default_factory=PeerPolicy)

    # D20, docs/TASKS.md#t-f1-17. Derived at load time, never read from the YAML.
    # `version` is 0 until a `ProfileVersionRegistry` assigns one: a profile that never
    # passed through a registry must not be able to claim it is version 1, because a turn
    # persisting that number would be recording an unverifiable fact.
    version: int = 0
    content_hash: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> AgentProfile:
        """Build a profile from an already-parsed mapping. The only constructor from data.

        The caller does the reading and the parsing - see the module docstring, WHERE
        PROFILES LIVE. This method is pure: given the same mapping it returns the same
        profile, on any machine, with no filesystem involved.

        Every unrecognised key - at this level and in each nested block - raises
        `ProfileValidationError` rather than being silently absorbed. See the class
        docstring: there is deliberately no free-form `extra: dict`.

        The runtime mapping check below is NOT redundant with the annotation. What arrives
        here comes out of a parser, so its static type is `Any` and the annotation proves
        nothing: a YAML file whose top level is a list, a string or `null` reaches this
        line, and it has to fail as a `ProfileValidationError` like any other malformed
        profile rather than as an `AttributeError` somewhere further down."""
        mapping = _require_mapping(data, where="profile")
        allowed = {f.name for f in fields(cls)} - _DERIVED_FIELDS
        _reject_unknown_keys(mapping, allowed, where="profile")

        defaults = cls(id="", persona="", model="")

        if "id" not in mapping or "persona" not in mapping or "model" not in mapping:
            missing = {"id", "persona", "model"} - set(mapping)
            joined = ", ".join(sorted(missing))
            raise ProfileValidationError(f"profile is missing required key(s): {joined}")

        profile = cls(
            id=mapping["id"],
            persona=mapping["persona"],
            model=mapping["model"],
            toolsets=tuple(mapping.get("toolsets", defaults.toolsets)),
            mcp_servers=tuple(
                _build_mcp_server_ref(item) for item in mapping.get("mcp_servers", ())
            ),
            skill_namespaces=tuple(mapping.get("skill_namespaces", defaults.skill_namespaces)),
            max_iterations=int(mapping.get("max_iterations", defaults.max_iterations)),
            max_cost_usd=Decimal(str(mapping.get("max_cost_usd", defaults.max_cost_usd))),
            approval_rules=tuple(
                _build_approval_rule(item) for item in mapping.get("approval_rules", ())
            ),
            compaction=(
                _build_compaction_policy(mapping["compaction"])
                if "compaction" in mapping
                else defaults.compaction
            ),
            media=(
                _build_media_policy(mapping["media"]) if "media" in mapping else defaults.media
            ),
            knowledge=(
                _build_knowledge_policy(mapping["knowledge"])
                if "knowledge" in mapping
                else defaults.knowledge
            ),
            peers=(
                _build_peer_policy(mapping["peers"]) if "peers" in mapping else defaults.peers
            ),
        )
        return replace(profile, content_hash=profile_content_hash(profile))

    def requires_approval_for(
        self, tool_name: str, arguments: dict[str, object]
    ) -> ApprovalRule | None:
        """The first approval rule this call trips, or `None` if it trips none.

        1. Rules whose `tool_name` matches, in declaration order - so a profile reads top
           to bottom like the file it is.
        2. `condition is None` means every call to that tool needs a human.
        3. Otherwise the condition is evaluated against `arguments` by the tiny grammar
           documented on `ApprovalRule`.

        THE ONE RULE THIS METHOD IS ALLOWED TO BE WRONG ABOUT, AND THE DIRECTION
            An unparseable condition, an unknown operator, a field the call never passed
            or a comparison with no answer all MATCH. They never fall through to `None`.

            The failure being bought off is the quiet one: a rule the evaluator cannot
            read gets skipped, this returns `None`, and a tool call somebody meant a human
            to see executes unattended. Nothing raises and nothing is logged - the profile
            still visibly has an approval rule in it. A typo makes the system more
            cautious, never less. `tests/unit/test_approval_rules.py` pins it, including
            the contrast case: a condition that IS readable and IS false still returns
            `None`, otherwise a method that always approves would satisfy the fail-safe
            and the rule set would stop being configuration.

        Sync and pure, like everything in `domain/` - no I/O to await (D13)."""
        for rule in self.approval_rules:
            if not rule.matches_tool(tool_name):
                continue
            if rule.condition is None:
                return rule
            try:
                matched = _evaluate_condition(rule.condition, arguments)
            except _UnreadableCondition:
                return rule
            if matched:
                return rule
        return None


class ProfileVersionRegistry:
    """Assigns the monotonic per-id `version` a turn records. D20, docs/TASKS.md#t-f1-17.

    Deliberately NOT a port. It answers one pure question - "is this content the same
    content I last saw under this id, and if not what is the next number" - and holds no
    I/O. Where the mapping is DURABLY kept is a persistence concern
    (docs/TASKS.md#t-f1-18); this class is the rule that persistence has to obey.

    Two properties the audit trail depends on:

    - **Monotonic per id.** A version never goes backwards, including when a YAML edit is
      reverted. Reverting reuses the old CONTENT but must not reuse the old NUMBER: two
      turns claiming the same version must always be able to show the same instructions.
    - **Idempotent for identical content.** Restarting the process, or loading the same
      file ten times, must not manufacture ten versions. A version that moves without the
      profile moving is a number nobody can reason about later.

    Sync, like everything in `domain/` - there is no I/O here to await."""

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        # profile id -> (content hash last seen, version assigned to it)
        self._seen: dict[str, tuple[str, int]] = {}

    def assign(self, profile: AgentProfile) -> AgentProfile:
        """Return `profile` carrying the version its content earns under its id."""
        content_hash = profile.content_hash or profile_content_hash(profile)
        known = self._seen.get(profile.id)

        if known is None:
            version = 1
        elif known[0] == content_hash:
            version = known[1]
        else:
            version = known[1] + 1

        self._seen[profile.id] = (content_hash, version)
        return replace(profile, version=version, content_hash=content_hash)

    def restore(self, profile_id: str, content_hash: str, version: int) -> None:
        """Seed what a previous process already assigned, before assigning anything new.

        Persistence (docs/TASKS.md#t-f1-18) calls this at startup. Without it a restart
        resets every id to version 1 and the numbers stop meaning anything across
        deployments - the exact failure D20 exists to prevent."""
        known = self._seen.get(profile_id)
        if known is not None and known[1] > version:
            return
        self._seen[profile_id] = (content_hash, version)
