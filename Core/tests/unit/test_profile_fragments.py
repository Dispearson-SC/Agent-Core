"""Profiles share instruction fragments (`include:`) and may carry a `public_alias`.

Subject: adapters/driven/profiles_fs/loader.py (`parse_profile`, `load_profile_sync`),
domain/profile.py (`public_alias`).

Several agents of one vertical repeat the same rules (tone, formatting, safety). A profile
lists fragment names under `include:`; the loader reads `<profiles>/fragments/<name>.md` and
appends them to the persona in order. The fragment text is part of the RESOLVED persona, so
editing a fragment moves the content hash and earns a new profile version (D20).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync, parse_profile
from agent_core.domain.profile import AgentProfile, ProfileValidationError

_HEAD = "id: p\nmodel: minimax/MiniMax-M3\n"


def _fragments(tmp_path: Path, **texts: str) -> Path:
    directory = tmp_path / "fragments"
    directory.mkdir()
    for name, text in texts.items():
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    return directory


def test_includes_are_appended_to_the_persona_in_order(tmp_path: Path) -> None:
    directory = _fragments(tmp_path, tone="BE KIND.", format="USE BULLETS.")

    profile = parse_profile(
        _HEAD + "persona: BASE\ninclude: [format, tone]\n", fragments_dir=directory
    )

    assert profile.persona == "BASE\n\nUSE BULLETS.\n\nBE KIND."


def test_a_profile_without_include_is_unchanged() -> None:
    profile = parse_profile(_HEAD + "persona: BASE\n")

    assert profile.persona == "BASE"


def test_a_missing_fragment_is_a_load_time_error(tmp_path: Path) -> None:
    directory = _fragments(tmp_path, tone="BE KIND.")

    with pytest.raises(ProfileValidationError, match="nope"):
        parse_profile(_HEAD + "persona: BASE\ninclude: [tone, nope]\n", fragments_dir=directory)


def test_include_without_a_fragments_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileValidationError, match="tone"):
        parse_profile(_HEAD + "persona: BASE\ninclude: [tone]\n", fragments_dir=tmp_path / "x")


@pytest.mark.parametrize("name", ["../secret", "a/b", ""])
def test_a_fragment_name_cannot_escape_the_directory(tmp_path: Path, name: str) -> None:
    directory = _fragments(tmp_path, tone="x")
    (tmp_path / "secret.md").write_text("LEAK", encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="fragment name"):
        parse_profile(_HEAD + f'persona: BASE\ninclude: ["{name}"]\n', fragments_dir=directory)


def test_include_must_be_a_list_of_names(tmp_path: Path) -> None:
    with pytest.raises(ProfileValidationError, match="include"):
        parse_profile(_HEAD + "persona: BASE\ninclude: tone\n", fragments_dir=tmp_path)


def test_editing_a_fragment_moves_the_content_hash(tmp_path: Path) -> None:
    directory = _fragments(tmp_path, tone="BE KIND.")
    text = _HEAD + "persona: BASE\ninclude: [tone]\n"
    before = parse_profile(text, fragments_dir=directory)

    (directory / "tone.md").write_text("BE BRIEF.", encoding="utf-8")
    after = parse_profile(text, fragments_dir=directory)

    assert before.content_hash != after.content_hash


def test_load_profile_sync_finds_fragments_next_to_the_profile(tmp_path: Path) -> None:
    _fragments(tmp_path, tone="BE KIND.")
    path = tmp_path / "p.yaml"
    path.write_text(_HEAD + "persona: BASE\ninclude: [tone]\n", encoding="utf-8")

    assert load_profile_sync(path).persona == "BASE\n\nBE KIND."


def test_public_alias_defaults_to_the_id() -> None:
    profile = AgentProfile.from_mapping({"id": "internal_x", "persona": "p", "model": "m"})

    assert profile.public_alias == "internal_x"


def test_public_alias_can_be_set() -> None:
    profile = AgentProfile.from_mapping(
        {"id": "internal_x", "persona": "p", "model": "m", "public_alias": "Ana"}
    )

    assert profile.public_alias == "Ana"
    assert profile.id == "internal_x"


def test_a_blank_public_alias_is_refused() -> None:
    with pytest.raises(ProfileValidationError, match="public_alias"):
        AgentProfile.from_mapping({"id": "x", "persona": "p", "model": "m", "public_alias": " "})
