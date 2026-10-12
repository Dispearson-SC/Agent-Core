"""Startup check of the declared peer topology (C17, D42).

Phase:   Agent-comms v2, C17
Tests:   Core/tests/unit/test_topology.py

WHY THIS EXISTS
    Who may ask whom was written in three places that nothing compared: each profile's allowlist
    (`peers.peers`), its hop limit (`peers.max_hops`) and the policy rules for `ask_peer`. They
    drifted silently. A specialist allowed another specialist, an allowlist entry was one-sided,
    a deep ask met a limit of 1, a rule denied for everybody an edge the profiles promised: each
    shows up only as a refused ask, on a customer's turn, with the cause three files away.

    ONE profile (the hub of a star, the root of a tree, any member of a mesh) declares
    `peers.topology` and `validate_topology` compares everything reachable from it with that
    declaration. It is opt-in: nothing declared, nothing checked, so every profile that exists
    today starts as before.

THE GATE IS TWO-SIDED, SO EVERY ENTRY IS MUTUAL
    `hop_limit.authorise_hop` lets A ask B only if A lists B AND B lists A, and refuses a hop at or
    beyond the SMALLER of the two `max_hops`. So a spoke lists its hub (else the hub's asks to it
    are refused), and "a spoke allowing another spoke" is an entry the star forbids.

WHAT IS CHECKED, per shape (A -> B is "A lists B")
    all     exactly one profile declares; its peers are enabled; every listed peer has a profile
            and has peers enabled; every asking edge is mutual; the hop limit at the depth the edge
            is made (0 for a star hub or a mesh, the parent's depth in a tree) lets it through.
    star    the spokes are what the hub lists; a spoke lists the hub and nobody else.
    tree    breadth first from the root: a profile has one parent (the one that reached it), lists
            its parent and its own children only, and no entry loops back.
    mesh    nothing beyond "all".
    policy  when the policy rules are known (a policy directory is managed from files): an asking
            edge needs some rule that can allow `ask_peer` to its target, and no rule may deny it
            for EVERY caller (a rule limited to roles, channels or a tenant is a narrowing, not
            drift).

PURE: no I/O. The composition root reads the files and calls this once at startup.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence

from agent_core.domain.peers import Topology
from agent_core.domain.policy import Effect, PolicyRule, pattern_matches
from agent_core.domain.profile import AgentProfile

__all__ = ["TopologyDriftError", "topology_violations", "validate_topology"]

_ASK_PEER = "ask_peer"


class TopologyDriftError(ValueError):
    """The profiles, hop limits or policy rules contradict the declared topology.

    Raised at startup and takes the process down, because the alternative is a deployment that
    runs and refuses asks. The message lists EVERY violation, so one restart fixes them all.
    """


def validate_topology(
    profiles: Mapping[str, AgentProfile], rules: Sequence[PolicyRule] | None = None
) -> None:
    """Raise `TopologyDriftError` listing every violation, or return when there is none."""
    violations = topology_violations(profiles, rules)
    if violations:
        listed = "\n".join(f"  - {violation}" for violation in violations)
        raise TopologyDriftError(
            "the peer topology declared in the profiles does not match what they, their hop "
            f"limits and the policy rules actually allow:\n{listed}"
        )


def topology_violations(
    profiles: Mapping[str, AgentProfile], rules: Sequence[PolicyRule] | None = None
) -> tuple[str, ...]:
    """Every way the loaded profiles (and the policy rules, when known) disagree with the declared
    topology, in a stable order. Empty when none is declared or all agree."""
    declaring = sorted(pid for pid, profile in profiles.items() if profile.peers.topology)
    if not declaring:
        return ()
    if len(declaring) > 1:
        return (
            f"topology is declared by {', '.join(declaring)}; declare it once, on the hub or "
            f"root of the group (peers.topology), and remove it from the others.",
        )
    root = declaring[0]
    shape = profiles[root].peers.topology
    assert shape is not None
    checker = _Check(profiles, rules, root, shape)
    checker.run()
    return tuple(dict.fromkeys(checker.violations))  # stable and without repeats


class _Check:
    def __init__(
        self,
        profiles: Mapping[str, AgentProfile],
        rules: Sequence[PolicyRule] | None,
        root: str,
        shape: Topology,
    ) -> None:
        self.profiles = profiles
        self.rules = rules
        self.root = root
        self.shape = shape
        self.violations: list[str] = []
        self.scope: dict[str, None] = {}  # insertion-ordered set of profiles reachable from root
        self.edges: list[tuple[str, str, int]] = []  # asking edges: (asker, target, depth)

    # --- helpers ---------------------------------------------------------------------------

    def _lists(self, profile_id: str) -> list[str]:
        return [str(ref.agent_id) for ref in self.profiles[profile_id].peers.peers]

    def _flag(self, message: str) -> None:
        self.violations.append(message)

    # --- the checks ------------------------------------------------------------------------

    def run(self) -> None:
        self._reach()
        if not self.profiles[self.root].peers.enabled:
            self._flag(
                f"{self.root} declares topology {self.shape.value} but has peers.enabled: false, "
                "so it can ask nobody."
            )
        {Topology.STAR: self._star, Topology.TREE: self._tree, Topology.MESH: self._mesh}[
            self.shape
        ]()
        self._edges_agree()
        if self.rules is not None:
            self._policy()

    def _reach(self) -> None:
        """Everything reachable from the root over allowlist entries; unknown ids are drift."""
        queue = deque([self.root])
        self.scope[self.root] = None
        while queue:
            current = queue.popleft()
            for listed in self._lists(current):
                if listed not in self.profiles:
                    self._flag(
                        f"{current} lists {listed} in peers.peers but no profile {listed!r} is "
                        "loaded, so nothing can ever answer it."
                    )
                elif listed not in self.scope:
                    self.scope[listed] = None
                    queue.append(listed)

    def _star(self) -> None:
        hub = self.root
        spokes = [s for s in self._lists(hub) if s in self.profiles]
        for spoke in spokes:
            self.edges.append((hub, spoke, 0))
            for other in self._lists(spoke):
                if other != hub:
                    self._flag(
                        f"star topology: spoke {spoke} allows {other}, but in a star the spokes "
                        f"may only talk to the hub {hub}. Remove {other} from {spoke}'s "
                        "peers.peers, or declare tree or mesh."
                    )
            if hub not in self._lists(spoke):
                self._flag(self._one_sided(hub, spoke))

    def _tree(self) -> None:
        parent: dict[str, str | None] = {self.root: None}
        depth: dict[str, int] = {self.root: 0}
        queue = deque([self.root])
        while queue:
            current = queue.popleft()
            above = parent[current]
            for child in self._lists(current):
                if child == above or child not in self.profiles:
                    continue
                if child == self.root:
                    self._flag(
                        f"tree topology: {current} lists the root {self.root}, but the root is "
                        f"not {current}'s parent ({above}); that closes a loop. Remove it from "
                        f"{current}'s peers.peers."
                    )
                    continue
                if child in parent:
                    self._flag(
                        f"tree topology: {child} has two parents, {parent[child]} and {current} "
                        f"(both list it). Keep one, or declare mesh."
                    )
                    continue
                parent[child] = current
                depth[child] = depth[current] + 1
                self.edges.append((current, child, depth[current]))
                queue.append(child)
                if current not in self._lists(child):
                    self._flag(self._one_sided(current, child))

    def _mesh(self) -> None:
        for asker in self.scope:
            for target in self._lists(asker):
                if target not in self.profiles:
                    continue
                self.edges.append((asker, target, 0))
                if asker not in self._lists(target):
                    self._flag(self._one_sided(asker, target))

    def _one_sided(self, asker: str, target: str) -> str:
        return (
            f"{asker} lists {target} but {target} does not list {asker}: the gate is two-sided, "
            f"so every {asker} -> {target} ask is refused (callee_does_not_allow_caller). Add "
            f"{asker} to {target}'s peers.peers, or remove {target} from {asker}'s."
        )

    def _edges_agree(self) -> None:
        """Peers switched on and a hop limit that lets each asking edge through."""
        for asker, target, depth in self.edges:
            for member in (asker, target):
                if not self.profiles[member].peers.enabled:
                    self._flag(
                        f"{asker} -> {target} is declared but {member} has peers.enabled: false, "
                        "so the gate refuses it."
                    )
            limit = min(self.profiles[asker].peers.max_hops, self.profiles[target].peers.max_hops)
            if depth >= limit:
                self._flag(
                    f"{asker} -> {target} is asked at hop {depth} but the smaller max_hops is "
                    f"{limit} ({asker}: {self.profiles[asker].peers.max_hops}, {target}: "
                    f"{self.profiles[target].peers.max_hops}), so the gate refuses it with "
                    "hop_limit_reached. Raise max_hops on both sides."
                )

    def _policy(self) -> None:
        assert self.rules is not None
        about_peers = [r for r in self.rules if pattern_matches(r.tool_pattern, _ASK_PEER)]
        for asker, target, _ in self.edges:
            covering = [
                r
                for r in about_peers
                if r.resource_pattern is None or pattern_matches(r.resource_pattern, target)
            ]
            for rule in covering:
                if (
                    rule.effect is Effect.DENY
                    and not rule.subject_roles
                    and not rule.channels
                    and rule.tenant_id is None
                ):
                    self._flag(
                        f"policy rule {rule.rule_id!r} denies ask_peer to {target} for every "
                        f"caller, but the topology declares {asker} -> {target}. Narrow or "
                        "remove the rule, or drop the edge."
                    )
            if not any(r.effect is not Effect.DENY for r in covering):
                self._flag(
                    f"no policy rule allows ask_peer to {target}, so the declared {asker} -> "
                    f"{target} is refused by default. Add an allow (or ask) rule for it in the "
                    "policy files."
                )
