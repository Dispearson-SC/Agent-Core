"""AgentProfile - how a generic core becomes a specific agent.

Phase:   F1 (shape) / F4 (first real profile) / F6 (skills, mcp) / F7 (media)
Tasks:   docs/TASKS.md#t-f1-04
Status:  TYPES DEFINED / BEHAVIOUR PENDING

THE CONTRACT THIS FILE EXISTS TO SERVE
    Adding a vertical is ONE profile file plus ONE tools package.
    Zero changes to domain/, application/ or ports/.

    An agent is DATA, not a subclass. If you ever find yourself writing
    `class FraudAgent(Agent)`, stop: the port cut is wrong and CLAUDE.md says to revisit
    it before continuing.

WHERE PROFILES LIVE
    Core/profiles/<id>.yaml, loaded by the composition root. YAML rather than Python so a
    non-engineer can read a profile and see what an agent is allowed to do - which is the
    whole point of having a governance layer.

HOW TO ADD A VERTICAL (the only procedure anyone should need)
    1. Write Core/profiles/<id>.yaml.
    2. Write Core/src/agent_core/adapters/driven/tools/<vertical>/.
    3. Add policy rows for the new tool names.
    4. Run the contract test. If it reports a diff under domain/, application/ or ports/,
       something is wrong with the design, not with the test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from agent_core.domain.compaction import CompactionPolicy
from agent_core.domain.knowledge import KnowledgePolicy
from agent_core.domain.media import MediaPolicy
from agent_core.domain.peers import PeerPolicy


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

    TODO(F4): `condition` is a mini-expression over the tool arguments. Keep it tiny and
    declarative (field, operator, value). Do NOT evaluate arbitrary Python here: a profile
    file is configuration, and configuration that executes code is a remote-code-execution
    vector the moment profiles become editable by anyone but us.
    """

    tool_name: str
    reason: str
    condition: str | None = None


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

    def requires_approval_for(self, tool_name: str, arguments: dict[str, object]) -> ApprovalRule | None:
        """PSEUDO-CODE - implement in F4.

        1. Find rules whose `tool_name` matches (exact or trailing wildcard).
        2. For each, if `condition` is None -> it matches; return it.
        3. Otherwise evaluate the declarative condition against `arguments`.
        4. Return the FIRST match, or None.

        Failure mode to guard: an unparseable or unknown condition must be treated as
        MATCHING (require approval), never as not matching. A typo in a profile file must
        make the system more cautious, not less. Test that explicitly.
        """
        raise NotImplementedError("F4 - docs/TASKS.md#t-f4-02")
