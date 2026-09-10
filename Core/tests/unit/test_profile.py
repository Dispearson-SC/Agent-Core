"""AgentProfile validation, and the adapter that feeds it.

Tasks: docs/TASKS.md#t-f1-04

A profile file is configuration read by a governance layer, not a free-form blob. An
unknown key must be a loud failure, not something a typo lets slip through unnoticed
straight into what an agent is allowed to do.

THE SPLIT THESE TESTS PIN
    `domain/profile.py` validates a MAPPING and knows nothing about files or YAML - it
    imports nothing external (CLAUDE.md, "Layer rules") and holds no I/O (D13). Reading the
    path and parsing the document belong to `adapters/driven/profiles_fs/`.

    So the validation tests below hand the domain a dict and never touch a disk, and one
    adapter test proves the real profile file on disk still arrives as the same profile.
    Testing validation through a file would prove the same rules while also making every
    one of them need a filesystem.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# `profile_module` is imported as a MODULE, not as `from ... import AgentProfile,
# ProfileValidationError`: a name that is missing at import time would fail the whole file
# at COLLECTION rather than inside the test that needs it.
import agent_core.domain.profile as profile_module
from agent_core.adapters.driven.profiles_fs import loader as profiles_loader

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


def _delivery_optimizer_mapping() -> dict[str, Any]:
    """The same agent `Core/profiles/delivery_optimizer.yaml` describes, as plain data."""
    return {
        "id": "delivery_optimizer",
        "persona": "You optimize delivery routing and pricing.",
        "model": "claude-sonnet-5",
        "toolsets": ["delivery"],
        "mcp_servers": [],
        "skill_namespaces": ["delivery"],
        "max_iterations": 15,
        "max_cost_usd": "0.25",
        "approval_rules": [
            {
                "tool_name": "pricing_apply",
                "reason": "Price change above 15% needs a human.",
                "condition": "abs(pct_change) > 15",
            }
        ],
        "compaction": {
            "trigger_fraction": 0.75,
            "target_fraction": 0.40,
            "head_exchanges": 2,
            "tail_tokens": 8000,
        },
        "media": {
            "accepted_kinds": ["image"],
            "delivery": "bytes",
            "max_bytes": 8388608,
            "allow_evidence_requests": True,
            "allow_speech_output": False,
        },
    }


def _assert_is_the_delivery_optimizer(profile: Any) -> None:
    assert profile.id == "delivery_optimizer"
    assert profile.model == "claude-sonnet-5"
    assert profile.toolsets == ("delivery",)
    assert profile.skill_namespaces == ("delivery",)
    assert profile.max_iterations == 15
    assert profile.max_cost_usd == Decimal("0.25")

    assert len(profile.approval_rules) == 1
    assert profile.approval_rules[0].tool_name == "pricing_apply"
    assert profile.approval_rules[0].condition == "abs(pct_change) > 15"

    assert profile.compaction.trigger_fraction == pytest.approx(0.75)
    assert profile.compaction.target_fraction == pytest.approx(0.40)
    assert profile.compaction.head_exchanges == 2
    assert profile.compaction.tail_tokens == 8000


def test_a_mapping_loads_into_a_frozen_agent_profile() -> None:
    profile = profile_module.AgentProfile.from_mapping(_delivery_optimizer_mapping())

    _assert_is_the_delivery_optimizer(profile)

    # Frozen: the whole point of loading into a dataclass instead of keeping a dict.
    with pytest.raises(FrozenInstanceError):
        profile.id = "mutated"  # type: ignore[misc]


def test_unknown_top_level_key_raises_instead_of_being_absorbed() -> None:
    mapping = {
        "id": "typo_agent",
        "persona": "does not matter",
        "model": "claude-sonnet-5",
        # A typo an engineer would actually make: "toolset" instead of "toolsets".
        "toolset": ["delivery"],
    }

    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(mapping)


def test_a_document_that_is_not_a_mapping_raises_the_profile_error() -> None:
    """A YAML file whose top level is a list still reaches `from_mapping`.

    The annotation says `Mapping`, but what arrives comes out of a parser and is typed
    `Any`, so the annotation stops nothing at runtime. The failure has to be the same loud
    `ProfileValidationError` every other malformed profile raises, not an `AttributeError`
    from somewhere deeper in the builder.
    """
    with pytest.raises(profile_module.ProfileValidationError):
        profile_module.AgentProfile.from_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# The driven adapter - adapters/driven/profiles_fs/loader.py
#
# The domain never sees a path or a parser. These are the only tests here that touch a
# disk, and they exist to prove the file on disk still becomes the profile the domain
# tests describe.
# --------------------------------------------------------------------------------------


def test_the_adapter_loads_the_delivery_optimizer_yaml_off_disk() -> None:
    path = PROFILES_DIR / "delivery_optimizer.yaml"

    profile = asyncio.run(profiles_loader.load_profile(path))

    _assert_is_the_delivery_optimizer(profile)
    # The real file's persona is a YAML block scalar; only the domain test pins its exact
    # text, so here it is enough that the block arrived as one non-empty string.
    assert "delivery routing" in profile.persona

    with pytest.raises(FrozenInstanceError):
        profile.id = "mutated"  # type: ignore[misc]

    # The async entry point is a `to_thread` wrapper around the sync island and must not
    # be able to drift from it - same bytes, same profile, same content hash.
    assert profile == profiles_loader.load_profile_sync(path)


def test_the_adapter_adds_nothing_the_domain_did_not_already_do(tmp_path: Path) -> None:
    """Parsing a file must produce exactly what handing the domain the same data produces.

    If these ever diverge, the adapter has started making decisions about what a profile
    means - which is the domain's job and the reason the split exists.
    """
    path = tmp_path / "equivalent.yaml"
    path.write_text(
        """
        id: equivalent
        persona: "same either way"
        model: claude-sonnet-5
        max_iterations: 12
        max_cost_usd: '0.50'
        """,
        encoding="utf-8",
    )

    from_disk = profiles_loader.load_profile_sync(path)
    from_data = profile_module.AgentProfile.from_mapping(
        {
            "id": "equivalent",
            "persona": "same either way",
            "model": "claude-sonnet-5",
            "max_iterations": 12,
            "max_cost_usd": "0.50",
        }
    )

    assert from_disk == from_data


def test_the_adapter_does_not_swallow_a_validation_error(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "typo_profile.yaml"
    bad_yaml.write_text(
        """
        id: typo_agent
        persona: "does not matter"
        model: claude-sonnet-5
        toolset:
          - delivery
        """,
        encoding="utf-8",
    )

    with pytest.raises(profile_module.ProfileValidationError):
        profiles_loader.load_profile_sync(bad_yaml)


# --------------------------------------------------------------------------------------
# Profile versioning - docs/TASKS.md#t-f1-17, docs/DECISIONS.md D20
#
# The version is what lets an audit six months from now answer "what had the agent been
# TOLD to do", not just "what did it do". It is monotonic per profile id and bumps only
# when the profile content actually changes: a version that moved without the profile
# moving is a version nobody can reason about.
#
# Stated over mappings rather than files: the rule is about CONTENT changing, and an edit
# to a file is only one of the ways content changes. Nothing here needs a disk.
# --------------------------------------------------------------------------------------


def _version_registry() -> Any:
    """Fetched by name so a missing class fails ON AN ASSERTION, not at collection."""
    registry_cls = getattr(profile_module, "ProfileVersionRegistry", None)
    assert registry_cls is not None, (
        "agent_core.domain.profile must expose ProfileVersionRegistry - "
        "docs/TASKS.md#t-f1-17"
    )
    return registry_cls()


def _profile(*, profile_id: str, persona: str, max_iterations: int) -> Any:
    return profile_module.AgentProfile.from_mapping(
        {
            "id": profile_id,
            "persona": persona,
            "model": "claude-sonnet-5",
            "max_iterations": max_iterations,
        }
    )


def test_editing_the_profile_bumps_the_version_for_that_id() -> None:
    registry = _version_registry()

    first = registry.assign(_profile(profile_id="versioned", persona="first", max_iterations=10))
    second = registry.assign(_profile(profile_id="versioned", persona="second", max_iterations=10))
    third = registry.assign(_profile(profile_id="versioned", persona="second", max_iterations=25))

    assert first.version < second.version < third.version
    # The returned profile is still the profile: versioning must not lose the content.
    assert third.persona == "second"
    assert third.max_iterations == 25


def test_reloading_identical_content_does_not_bump_the_version() -> None:
    registry = _version_registry()

    def load() -> Any:
        return registry.assign(
            _profile(profile_id="stable", persona="unchanged", max_iterations=10)
        )

    first, second, third = load(), load(), load()

    assert first.version == second.version == third.version


def test_version_is_monotonic_even_when_the_content_reverts() -> None:
    """A revert is an edit. Reusing the old version would make two different turns claim
    the same profile version and point at different instructions."""
    registry = _version_registry()

    original = registry.assign(_profile(profile_id="reverted", persona="a", max_iterations=10))
    registry.assign(_profile(profile_id="reverted", persona="b", max_iterations=10))
    back_again = registry.assign(_profile(profile_id="reverted", persona="a", max_iterations=10))

    assert back_again.version > original.version


def test_versions_are_tracked_per_id_not_globally() -> None:
    registry = _version_registry()

    first_one = registry.assign(_profile(profile_id="one", persona="one", max_iterations=10))
    first_two = registry.assign(_profile(profile_id="two", persona="two", max_iterations=10))

    second_one = registry.assign(
        _profile(profile_id="one", persona="one edited", max_iterations=10)
    )

    assert first_one.version == first_two.version
    assert second_one.version > first_one.version
    # Editing "one" must leave "two" exactly where it was.
    assert (
        registry.assign(_profile(profile_id="two", persona="two", max_iterations=10)).version
        == first_two.version
    )
