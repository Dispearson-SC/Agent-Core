"""Media policy invariants - the empty set means NOTHING here.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-01

The asymmetry with `PolicyRule.subject_roles` is the whole point of this module, and it
is deliberate: there an empty set means "any role", here an empty set means "no kind".
Both defaults are pinned below, in the same file, so a reader who finds one surprising
sees the other one line away and does not "fix" either into the other.

A permissive `PolicyRule` is inconvenient. Permissive media handling is a cost incident
and a privacy incident: an agent that silently accepts video because a field was left at
its default uploads and forwards binaries nobody authorised.
"""

import pytest

from agent_core.domain.media import (
    MediaDelivery,
    MediaKind,
    MediaPolicy,
)
from agent_core.domain.policy import Effect, PolicyRule


def test_empty_accepted_kinds_accepts_nothing() -> None:
    """The anchor assertion: empty is deny-all, not allow-all."""
    policy = MediaPolicy()

    assert policy.accepted_kinds == frozenset()
    for kind in MediaKind:
        assert policy.accepts(kind) is False, f"empty policy accepted {kind}"


def test_populated_accepted_kinds_accepts_only_those_kinds() -> None:
    policy = MediaPolicy(accepted_kinds=frozenset({MediaKind.IMAGE, MediaKind.DOCUMENT}))

    assert policy.accepts(MediaKind.IMAGE) is True
    assert policy.accepts(MediaKind.DOCUMENT) is True
    assert policy.accepts(MediaKind.AUDIO) is False
    assert policy.accepts(MediaKind.VIDEO) is False


def test_accepts_is_membership_not_truthiness_of_the_set() -> None:
    """`if not self.accepted_kinds: return True` is the bug this test exists to catch.

    An implementation that treats the empty set as a wildcard passes every test that only
    ever populates the set, so the empty case is asserted per kind above and the populated
    case is asserted to still exclude here."""
    policy = MediaPolicy(accepted_kinds=frozenset({MediaKind.AUDIO}))

    assert policy.accepts(MediaKind.AUDIO) is True
    assert policy.accepts(MediaKind.IMAGE) is False


def test_defaults_are_the_safe_ones() -> None:
    """BYTES delivery keeps the binary away from the provider's fetcher; evidence requests
    and speech output are opt-in. All three fail closed by omission."""
    policy = MediaPolicy()

    assert policy.delivery is MediaDelivery.BYTES
    assert policy.allow_evidence_requests is False
    assert policy.allow_speech_output is False


@pytest.mark.parametrize("kind", list(MediaKind))
def test_a_kind_is_never_accepted_by_a_disjoint_policy(kind: MediaKind) -> None:
    others = frozenset(k for k in MediaKind if k is not kind)
    policy = MediaPolicy(accepted_kinds=others)

    assert policy.accepts(kind) is False


def test_the_asymmetry_with_policy_rule_is_intentional() -> None:
    """Two empties, two meanings. Documented in both docstrings; pinned here.

    `PolicyRule.subject_roles` empty means ANY role, so an empty-role rule still matches.
    `MediaPolicy.accepted_kinds` empty means NO kind. Making these agree would either
    break every role-agnostic rule or open media to everything."""
    rule = PolicyRule(
        rule_id="r-any",
        tool_pattern="request_evidence",
        effect=Effect.ALLOW,
        reason="any role may ask",
    )

    assert rule.subject_roles == frozenset()
    assert rule.matches("request_evidence", frozenset({"operator"}), "http") is True

    assert MediaPolicy().accepts(MediaKind.IMAGE) is False
