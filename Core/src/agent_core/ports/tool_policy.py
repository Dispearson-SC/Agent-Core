"""Port: ToolPolicy - may this caller run this tool, and does a human decide?

Phase:      F1
Tasks:      docs/TASKS.md#t-f1-06
Adapter:    adapters/driven/persistence_pg/policy_repository.py
Per-vertical: YES (data only - rows change, code does not)

SILENT-BUG AREA. A hole here never fails a test; it just never fires. Verify by hand.

TWO CALL SITES, TWO PURPOSES - DO NOT COLLAPSE THEM
    `filter_toolset` runs ONCE per turn, before the model sees anything, and removes
    tools the caller may never use. This is what keeps a webhook-triggered agent from
    even KNOWING that a shell tool exists. Hermes does the same via toolsets: its
    webhook-safe bundle is four read-only tools, because webhook payloads are untrusted
    third-party content.

    `decide` runs per tool CALL, inside the before_tool_execute hook, with the actual
    arguments available. This is what catches "transfer $10" vs "transfer $10,000,000".

    Filtering alone is not enough (arguments matter) and deciding alone is not enough
    (advertising a forbidden tool invites the model to keep trying it and wastes the
    budget). You need both.

ORDERING: DENY beats NEEDS_APPROVAL beats ALLOW - see domain/policy.py.

THE ASYNC SPLIT (D13) - WHY THIS PORT HAS THREE METHODS AND NOT TWO
    `decide()` runs inside `before_tool_execute`, i.e. on EVERY tool call. A database round
    trip per call would add network latency to every tool the agent uses.

    So I/O and decision are separated:
        load_rules()    async, ONCE per turn      -> RuleSet snapshot
        filter_toolset() sync, pure over the snapshot
        decide()         sync, pure over the snapshot, per call

    A frozen snapshot also makes a turn's decisions internally consistent: a rule edited
    mid-turn cannot change the verdict between the third tool call and the fourth.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.policy import PolicyDecision, RuleSet
from agent_core.domain.turn import CallerIdentity


class ToolPolicy(Protocol):
    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        """PSEUDO-CODE - F1. The ONLY method here that touches I/O.

        Called once at turn start. Returns a frozen snapshot of every rule applicable to
        this caller.

        FAILURE MODE: if the rule store is unreachable, FAIL CLOSED - return a RuleSet with
        no rules and `default_effect=DENY`, and surface the error. An agent that runs
        unrestricted because Postgres blipped is worse than an agent that stops.
        """
        ...

    def filter_toolset(self, rules: RuleSet, tool_names: tuple[str, ...]) -> tuple[str, ...]:
        """PSEUDO-CODE - F1. Sync and pure.

        Return only the names whose decision is not DENY. Keep NEEDS_APPROVAL tools: the
        model may ask for them, a human just has to agree.
        """
        ...

    def decide(
        self, rules: RuleSet, tool_name: str, arguments: dict[str, object]
    ) -> PolicyDecision:
        """PSEUDO-CODE - F1. Sync and pure - NO I/O, this is the hot path.

        1. rules.applicable(tool_name, roles, channel)
        2. Reduce by EFFECT_PRECEDENCE - DENY wins, then NEEDS_APPROVAL, then ALLOW.
        3. No match at all -> return `rules.default_effect`.

        THE DEFAULT MUST BE DENY. An unknown tool name is either a typo or a tool someone
        added without adding a rule; both deserve refusal. A default of ALLOW means every
        newly registered tool is silently world-usable the moment it is imported - and
        nothing in the test suite will tell you.

        FAILURE MODE: if the rule store is unreachable, FAIL CLOSED (deny) and surface the
        error. An agent that runs unrestricted because Postgres blipped is worse than an
        agent that stops.
        """
        ...
