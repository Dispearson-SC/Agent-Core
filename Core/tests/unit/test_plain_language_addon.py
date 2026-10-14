"""`plain_language`: a generic addon that makes an agent talk to a non-expert in plain words (G4).

Subject: addons/plain_language, enabled through the real loader and registration and read back
through `load_profiles` with a consumer profile that declares the slot `plain_language` (D89).

A port of the part of `test_glazed_prompts.py` of vertical/glazed that concerns the "How to talk to
the manager" text (`test_orchestrator_has_the_manager_communication_section`), in its generic form:
the audience, the language, the terms to avoid and the voice marker are settings, and the text names
no vertical.

DEFERRED TO G5 (the glazed profiles keep their own wording and the tool docstrings are G2's): the
other 32 of the 33 tests, i.e. the display-name, verbatim-number, option, proposal-id, thin-persona,
Spanish-only, reasoning-rule, tool-budget, snapshot-first, briefing-first, internal-names,
daily-granularity, one-final-answer, Spanish-role, history, proposal-timing, cover, compliance-cause
and language-glitch assertions on the eight personas, plus the docstring and signature tests of the
tools (already covered by the description/schema tests of `test_glazed_tools_contract.py`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.registration import AddonContributions, register_addons
from agent_core.composition import TOOL_PACKAGES, load_profiles
from agent_core.domain.profile import ProfileVersionRegistry

ADDON = Path(__file__).resolve().parents[3] / "addons" / "plain_language"
ADDONS_DIR = ADDON.parent

CONSUMER = """\
id: desk
description: A consumer that opts in by declaring the slot.
persona: |
  You help with accounts.

  {{slot:plain_language}}

  Be brief.
slots:
  plain_language: []
model: minimax/MiniMax-M3
max_iterations: 5
max_cost_usd: "0.10"
"""
BYSTANDER = (
    CONSUMER.replace("id: desk", "id: bystander")
    .replace("\n  {{slot:plain_language}}\n", "")
    .replace("slots:\n  plain_language: []\n", "")
)


def _contributions(**settings: Any) -> AddonContributions:
    addons = load_addons(
        enabled=["plain_language"],
        addons_root=ADDONS_DIR,
        core_version="0.0.0",
        config={"plain_language": settings} if settings else None,
    )
    return register_addons(
        addons, builtin_toolsets=TOOL_PACKAGES, builtin_fragments_dir=None, reserved_channels=()
    )


def _personas(tmp_path: Path, **settings: Any) -> dict[str, str]:
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "desk.yaml").write_text(CONSUMER, encoding="utf-8")
    (profiles / "bystander.yaml").write_text(BYSTANDER, encoding="utf-8")
    loaded = load_profiles(
        profiles, ProfileVersionRegistry(), contributions=_contributions(**settings)
    )
    return {profile_id: profile.persona for profile_id, profile in loaded.items()}


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def test_the_guidance_lands_in_a_profile_that_declares_the_slot_and_only_there(
    tmp_path: Path,
) -> None:
    personas = _personas(tmp_path)

    assert "You help with accounts." in personas["desk"] and "Be brief." in personas["desk"]
    assert "How to talk to" in personas["desk"]
    assert "How to talk to" not in personas["bystander"]
    assert "{{" not in personas["desk"], "no marker or template is left in the persona"


def test_the_addon_provides_one_fragment_inserted_by_a_wildcard() -> None:
    contributions = _contributions()

    assert set(contributions.fragments) == {"plain_language"}
    assert [(a, p, s) for a, p, s, _ in contributions.inserts] == [
        ("plain_language", "*", "plain_language")
    ]


def test_the_defaults_name_a_non_expert_and_leave_voice_off(tmp_path: Path) -> None:
    text = _flat(_personas(tmp_path)["desk"])

    assert "someone without a technical background" in text
    assert "voice" not in text and "read aloud" not in text and "markdown" not in text
    assert "write every answer in" not in text
    assert "never say" not in text


def test_the_communication_section_keeps_the_rules_the_vertical_branch_tested(
    tmp_path: Path,
) -> None:
    """Port of `test_orchestrator_has_the_manager_communication_section`, generic form."""
    persona = _personas(
        tmp_path,
        voice_style=True,
        voice_marker="[voice]",
        language="Spanish",
        avoided_terms=["quartiles", "percentile", "interval", "bias", "WAPE", "newsvendor", "SKU"],
    )["desk"]
    lowered = _flat(persona)

    assert "How to talk to" in persona
    # voice channel
    assert '"[voice]"' in persona
    assert "never mention or repeat the tag" in lowered
    assert "no markdown at all" in lowered
    assert "apply only to the voice channel" in lowered
    # plain vocabulary, adaptive depth; text keeps its format
    assert "plain vocabulary in every channel" in lowered
    assert "keep the depth, detail, structure, tables and lists" in lowered
    assert "adaptive depth" in lowered
    assert "no markdown tables by default" not in lowered
    for jargon in ("quartiles", "percentile", "interval", "bias", "WAPE", "newsvendor", "SKU"):
        assert jargon in persona  # listed as terms to avoid
    # what the rules above require stays intact, and so does the language rule
    assert "work exactly as described above" in lowered
    assert "write every answer in spanish" in lowered


def test_the_voice_section_follows_the_voice_style_setting(tmp_path: Path) -> None:
    spoken = _flat(_personas(tmp_path / "on", voice_style=True)["desk"])
    written = _flat(_personas(tmp_path / "off", voice_style=False)["desk"])

    assert '"[voice]"' in spoken and "2 to 4 sentences" in spoken
    assert "[voice]" not in written and "2 to 4 sentences" not in written


def test_the_voice_marker_is_the_one_the_deployment_sets(tmp_path: Path) -> None:
    persona = _personas(tmp_path, voice_style=True, voice_marker="[canal: voz]")["desk"]

    assert '"[canal: voz]"' in persona and "[voice]" not in persona


def test_the_audience_and_the_terms_to_avoid_are_the_deployments(tmp_path: Path) -> None:
    persona = _personas(
        tmp_path, audience="a night-shift nurse", avoided_terms=["triage score", "etiology"]
    )["desk"]

    assert "a night-shift nurse" in persona
    assert "Never say: triage score, etiology" in persona


def test_a_setting_that_is_not_a_value_of_its_type_is_refused() -> None:
    with pytest.raises(Exception, match="voice_style"):
        _contributions(voice_style="loud")


# --- the addon is generic ------------------------------------------------------------------------

VERTICAL_WORDS = re.compile(
    r"glazed|retail|store|shop|sku|bakery|inventory|sales|tienda|sucursal|canal|spanish|español",
    re.IGNORECASE,
)


def test_nothing_in_the_addon_names_a_vertical_or_a_language() -> None:
    offenders = [
        f"{path.relative_to(ADDON)}: {match.group(0)}"
        for path in sorted(ADDON.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for match in VERTICAL_WORDS.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_addon_ships_no_code() -> None:
    assert [p.name for p in ADDON.rglob("*.py")] == []


def test_a_deployment_that_enables_the_addon_without_any_slot_is_refused_by_name(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "bystander.yaml").write_text(BYSTANDER, encoding="utf-8")

    with pytest.raises(AddonRegistrationError) as caught:
        load_profiles(profiles, ProfileVersionRegistry(), contributions=_contributions())

    assert caught.value.addon == "plain_language" and "no profile declares" in str(caught.value)
