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
        load_rules(caller)              async, ONCE per turn -> RuleSet snapshot
        filter_toolset(rules, names)    sync, pure over the snapshot
        decide(rules, name, arguments)  sync, pure over the snapshot, per call

    A frozen snapshot also makes a turn's decisions internally consistent: a rule edited
    mid-turn cannot change the verdict between the third tool call and the fourth.

    The two sync methods stay sync deliberately, and this is NOT an exception to D13. D13's
    rule is "no I/O to await", not "sync means domain": both are pure functions over an
    already-loaded snapshot, and `decide` runs on every tool call. Making them coroutines
    would buy an await point with nothing behind it and re-open the per-call latency this
    split exists to close.

THE CALLER IS NAMED EXACTLY ONCE, AT THE NARROWING POINT
    `load_rules(caller)` narrows the I/O: it fetches only the rows that could apply to this
    caller's tenant, roles and channel. The snapshot it returns REMEMBERS that narrowing -
    `domain.policy.RuleSet` carries `subject_roles` and `channel` alongside the rules, and
    `RuleSet.applicable(tool_name)` asks for nothing else. So the sync methods below need
    only the snapshot and the question; the subject is already settled.

    THIS IS A SAFETY PROPERTY, NOT A TIDINESS ONE. Passing the caller alongside the
    snapshot type-checks perfectly while asking caller A's rules about caller B, and in a
    policy engine that means a tool gets allowed for someone who should not have it -
    no exception, no failing test, discovered the day it matters. A snapshot IS the answer
    for exactly one caller's narrowing and cannot be pointed at another one, so there is
    no second subject to disagree with the first. `RuleSet.for_caller` is the one intended
    construction path; an adapter that hand-builds a snapshot with someone else's roles is
    reintroducing the hole this shape closes.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.policy import PolicyDecision, RuleSet
from agent_core.domain.turn import CallerIdentity


class ToolPolicy(Protocol):
    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        """PSEUDO-CODE - F1. The ONLY method here that touches I/O.

        ASYNC (D13): the only awaitable method on this port, and the reason the other two
        can stay sync. It runs once at turn start, so its round trip is paid once per turn
        instead of once per tool call.

        Called once at turn start. Returns a frozen snapshot of every rule applicable to
        this caller, narrowed for that caller and remembering it - build it through
        `RuleSet.for_caller(caller, rules)` so the stored narrowing always comes from the
        identity the query was actually run for.

        FAILURE MODE: if the rule store is unreachable, FAIL CLOSED - return a snapshot
        with no rules and `default_effect=DENY`, and surface the error. An agent that runs
        unrestricted because Postgres blipped is worse than an agent that stops.
        """
        ...

    def filter_toolset(self, rules: RuleSet, tool_names: tuple[str, ...]) -> tuple[str, ...]:
        """PSEUDO-CODE - F1. SYNC and pure - no I/O to await, so D13 keeps it sync.

        `rules` is the snapshot `load_rules` returned, and it carries the caller it was
        narrowed for. There is no caller parameter: there is no second identity that could
        disagree with the one the snapshot already holds.

        Return only the names whose decision is not DENY. Keep NEEDS_APPROVAL tools: the
        model may ask for them, a human just has to agree.

        Definitionally this is `decide` applied per name with empty arguments, and it must
        stay consistent with it: a name this method keeps and `decide` then denies for
        every possible argument is a rule that only ever wastes budget.
        """
        ...

    def decide(
        self,
        rules: RuleSet,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PolicyDecision:
        """PSEUDO-CODE - F1. SYNC and pure - NO I/O, this is the hot path. D13 keeps it
        sync for exactly that reason: it runs inside `before_tool_execute`, on every call.

        `rules` is the snapshot `load_rules` returned; it already knows whose rules these
        are, which is why the verdict needs only the tool name and its arguments.

        1. rules.applicable(tool_name) - the snapshot matches against the roles and the
           channel it was narrowed for.
        2. Reduce by EFFECT_PRECEDENCE - DENY wins, then NEEDS_APPROVAL, then ALLOW.
        3. No match at all -> return `rules.default_effect`.

        THE DEFAULT MUST BE DENY. An unknown tool name is either a typo or a tool someone
        added without adding a rule; both deserve refusal. A default of ALLOW means every
        newly registered tool is silently world-usable the moment it is imported - and
        nothing in the test suite will tell you.

        FAILURE MODE: none of its own. This method cannot reach the rule store, so it
        cannot fail on it - unreachability is handled once, in `load_rules`, by returning an
        empty RuleSet with `default_effect=DENY`. Step 3 above then denies everything from
        that snapshot alone. The fail-closed behaviour is inherited, not re-implemented
        here; a second unreachability branch in this method would be dead code pretending
        to be a safeguard.
        """
        ...
