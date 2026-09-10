"""Knowledge access rules.

Phase:   F8 - Knowledge retrieval
Tasks:   docs/TASKS.md#t-f8-01
Status:  t-f8-01 PINNED (KnowledgePolicy.can_read, and the tenant narrowing). t-f8-07 and
         t-f8-08 still owe this module their tests - do not fold them into the cases below.

WHY THIS FILE EXISTS AT ALL
    `collections` is a permission boundary, not an organising convenience, so the empty
    case is the case that matters: an agent that was never granted a collection must
    retrieve NOTHING, not everything. The asymmetry against `PolicyRule.subject_roles`,
    where empty means "any subject", is deliberate - a permissive default is acceptable
    for a convenience feature and unacceptable for data access - and an asymmetry that is
    only written down in a docstring is one refactor away from being read the other way.

    The domain is sync and imports nothing external (CLAUDE.md, "Layer rules"), so these
    cases construct a policy and call a method. No database, no fixtures, no I/O.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

# Imported as a MODULE so a missing name fails inside the test that needs it rather than
# at collection, taking the whole file down with it.
import agent_core.domain.knowledge as knowledge_module
from agent_core.domain.turn import CallerIdentity, TenantId

# Names an agent might plausibly be handed, including the ones a caller would guess.
CANDIDATE_COLLECTIONS = (
    "delivery",
    "fraud",
    "pricing",
    "public",
    "",
    "*",
    "default",
)


def _collection(name: str) -> knowledge_module.CollectionId:
    return knowledge_module.CollectionId(name)


def test_can_read_denies_every_collection_when_none_are_granted() -> None:
    """Empty `collections` means NOTHING, not everything.

    The default-constructed policy is the shape an `AgentProfile` gets when its file says
    nothing about knowledge, which is the shape most profiles will have.
    """
    policy = knowledge_module.KnowledgePolicy()

    assert policy.collections == ()

    for name in CANDIDATE_COLLECTIONS:
        assert policy.can_read(_collection(name)) is False, (
            f"empty `collections` granted read on {name!r}; "
            "empty must mean nothing, not everything"
        )


def test_can_read_denies_every_collection_when_enabled_but_none_are_granted() -> None:
    """`enabled=True` is not a grant.

    Turning the feature on says the agent may retrieve; `collections` says from where.
    Collapsing the two would make `enabled=True` a wildcard, which is exactly the
    permissive default this port refuses.
    """
    policy = knowledge_module.KnowledgePolicy(enabled=True)

    for name in CANDIDATE_COLLECTIONS:
        assert policy.can_read(_collection(name)) is False, (
            f"`enabled=True` alone granted read on {name!r}; "
            "enabling retrieval is not granting a collection"
        )


def test_can_read_grants_only_the_named_collections() -> None:
    """The counterpart: without this, `can_read` returning False always would pass above.

    A deny-everything implementation satisfies the two cases before it, so the grant case
    has to be pinned in the same file or the guard proves nothing.
    """
    granted = _collection("delivery")
    policy = knowledge_module.KnowledgePolicy(enabled=True, collections=(granted,))

    assert policy.can_read(granted) is True
    assert policy.can_read(_collection("fraud")) is False


def test_knowledge_policy_is_frozen() -> None:
    """A permission boundary that can be reassigned at runtime is not a boundary."""
    policy = knowledge_module.KnowledgePolicy()

    with pytest.raises(FrozenInstanceError):
        policy.collections = (_collection("fraud"),)  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# The tenant narrowing. See the t-f8-01 correction note in docs/TASKS.md.
# --------------------------------------------------------------------------------------
#
# `ports/knowledge_base.py` instructs its adapter to "filter by tenant IN THE QUERY", and
# for as long as the whole contract was `(policy, query, collections)` no adapter could
# obey it: there was no tenant anywhere to put in the predicate. The fix is the one
# `RuleSet` already uses for roles and channel - a value narrowed for exactly one tenant,
# so the predicate has a source and no call site can supply a different one.


def _caller(tenant: str) -> CallerIdentity:
    return CallerIdentity(subject_id="u-1", channel="whatsapp", tenant_id=TenantId(tenant))


def test_the_profile_policy_still_carries_no_tenant() -> None:
    """The CONFIG type stays tenant-free, and that is why the narrowing is its own type.

    `KnowledgePolicy` is loaded from a profile YAML file by `_build_knowledge_policy`,
    which derives the accepted keys from `fields(KnowledgePolicy)`. A `tenant_id` field
    here would therefore become a YAML key, and a profile that names its own tenant is the
    breach wearing a config file's clothes.
    """
    names = {f.name for f in fields(knowledge_module.KnowledgePolicy)}

    assert "tenant_id" not in names, (
        "KnowledgePolicy grew a tenant field. It is profile configuration, so the field "
        "would become an authorable YAML key - the tenant must come from the caller, "
        "never from the file that describes the agent."
    )


def test_a_narrowed_policy_cannot_be_built_without_naming_a_tenant() -> None:
    """The omission is a construction error, not a review comment.

    Every other field of a `KnowledgePolicy` has a default, so a narrowing whose tenant
    could be defaulted would be a narrowing nobody has to perform.
    """
    with pytest.raises(TypeError):
        knowledge_module.TenantKnowledgePolicy()  # type: ignore[call-arg]


def test_the_narrowing_takes_the_tenant_from_the_caller() -> None:
    """`for_caller` is the one intended construction path, exactly like `RuleSet.for_caller`.

    The tenant that scopes the query then always comes from the identity the turn is being
    served for, rather than being copied by hand at a call site where it can be copied
    wrong.
    """
    policy = knowledge_module.KnowledgePolicy(
        enabled=True, collections=(_collection("pricing"),)
    )

    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(_caller("tenant-a"), policy)

    assert narrowed.tenant_id == TenantId("tenant-a")


def test_the_narrowing_copies_every_setting_of_the_policy() -> None:
    """A drift lock: a field added to `KnowledgePolicy` must survive `for_caller`.

    `for_caller` copies the settings one by one, which is readable but is exactly the shape
    that silently drops a field added later. A dropped `min_score` would mean retrieval
    quietly falls back to the default floor for every tenant - no exception, no failing
    test anywhere else.
    """
    policy = knowledge_module.KnowledgePolicy(
        enabled=True,
        collections=(_collection("pricing"), _collection("delivery")),
        mode=knowledge_module.RetrievalMode.HYBRID,
        top_k=11,
        min_score=0.83,
        max_context_chars=1234,
        inject_into_prompt=True,
    )

    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(_caller("tenant-a"), policy)

    for f in fields(knowledge_module.KnowledgePolicy):
        assert getattr(narrowed, f.name) == getattr(policy, f.name), (
            f"`for_caller` did not carry {f.name} across; the narrowed policy is not the "
            f"profile's policy any more."
        )


def test_a_narrowed_policy_cannot_be_repointed_at_another_tenant() -> None:
    """Frozen, like every other permission boundary in this package.

    Without this, an adapter holding tenant A's policy could set the field before building
    its query, and the whole narrowing would be advisory.
    """
    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(
        _caller("tenant-a"), knowledge_module.KnowledgePolicy(enabled=True)
    )

    with pytest.raises(FrozenInstanceError):
        narrowed.tenant_id = TenantId("tenant-b")  # type: ignore[misc]


def test_a_narrowed_policy_is_still_a_knowledge_policy() -> None:
    """The positive control. A narrowing that broke `can_read` would be no use to anyone.

    It is a subclass on purpose: the collection grant and the tenant are ONE value, so
    there is no second object a call site could pair with the wrong first one - which is
    the failure mode a separate `tenant` parameter would have had.
    """
    granted = _collection("pricing")
    narrowed = knowledge_module.TenantKnowledgePolicy.for_caller(
        _caller("tenant-a"),
        knowledge_module.KnowledgePolicy(enabled=True, collections=(granted,)),
    )

    assert isinstance(narrowed, knowledge_module.KnowledgePolicy)
    assert narrowed.can_read(granted) is True
    assert narrowed.can_read(_collection("fraud")) is False
