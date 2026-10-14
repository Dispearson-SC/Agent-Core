"""An addon's prompt fragment may use the addon's own settings (Phase 3, G4, D89).

Subject: adapters/driven/profiles_fs/fragments.py (`render_fragment`, `AddonFragment`), the way
`fragment_reader` reads an addon fragment, and `register_addons` building them.

THE LANGUAGE IS TWO FORMS AND NOTHING ELSE
    {{setting:name}}                     inline, replaced by the setting's value
    {{if:name}} ... {{endif}}            a block, each marker alone on its line; kept when the
                                         setting is true (a non-empty string, a non-zero number)
A name the addon does not declare, a block left open, a nested block and a stray `{{endif}}` stop
startup naming the addon. Core's own fragments are never rendered.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.profiles_fs.fragments import AddonFragment, render_fragment
from agent_core.adapters.driven.profiles_fs.loader import fragment_reader, parse_profile
from agent_core.composition import TOOL_PACKAGES

SETTINGS = {"audience": "a shop manager", "voice": True, "quiet": False, "limit": 3, "empty": ""}


def test_a_setting_replaces_its_placeholder_inline() -> None:
    text = "Write for {{setting:audience}}, at most {{setting:limit}} lines."

    assert render_fragment(text, SETTINGS) == "Write for a shop manager, at most 3 lines."


def test_text_without_markers_comes_back_unchanged() -> None:
    text = "Plain.\n\n  - keep {braces} and {{slot:x}} alone\n"

    assert render_fragment(text, SETTINGS) == text


def test_a_block_is_kept_when_its_setting_is_true_and_dropped_with_its_markers_when_not() -> None:
    text = "A\n{{if:voice}}\nspoken\n{{endif}}\n{{if:quiet}}\nhidden\n{{endif}}\nZ"

    assert render_fragment(text, SETTINGS) == "A\nspoken\nZ"


@pytest.mark.parametrize(("name", "kept"), [("empty", False), ("limit", True), ("audience", True)])
def test_truthiness_is_the_settings_own(name: str, kept: bool) -> None:
    rendered = render_fragment("{{if:" + name + "}}\nX\n{{endif}}", SETTINGS)

    assert (rendered == "X") is kept


def test_a_placeholder_in_a_value_is_not_expanded_again() -> None:
    assert render_fragment("{{setting:a}}", {"a": "{{setting:a}}"}) == "{{setting:a}}"


@pytest.mark.parametrize(
    "text",
    [
        "{{setting:nope}}",
        "{{if:nope}}\nx\n{{endif}}",
        "{{if:voice}}\nx",
        "x\n{{endif}}",
        "{{if:voice}}\n{{if:quiet}}\nx\n{{endif}}\n{{endif}}",
        "text {{if:voice}} inline {{endif}}",
    ],
)
def test_a_malformed_template_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"\{\{"):
        render_fragment(text, SETTINGS)


def test_an_addon_fragment_reads_like_a_file_and_renders_with_its_settings(tmp_path: Path) -> None:
    path = tmp_path / "style.md"
    path.write_text("For {{setting:audience}}.\n", encoding="utf-8")
    fragment = AddonFragment(path, {"audience": "everyone"})

    assert fragment.is_file() and fragment.read_text(encoding="utf-8") == "For everyone.\n"
    assert fragment.stem == "style" and fragment.name == "style.md"


def test_the_profile_loader_renders_an_addon_fragment_and_leaves_a_core_one_alone(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    core.mkdir()
    (core / "own.md").write_text("Core says {{setting:audience}}.", encoding="utf-8")
    addon_file = tmp_path / "style.md"
    addon_file.write_text("Addon says {{setting:audience}}.", encoding="utf-8")

    persona = parse_profile(
        "id: p\nmodel: minimax/MiniMax-M3\npersona: BASE\ninclude: [own, style]\n",
        fragments_dir=core,
        addon_fragments={"style": AddonFragment(addon_file, {"audience": "kids"})},
    ).persona

    assert persona == "BASE\n\nCore says {{setting:audience}}.\n\nAddon says kids."
    assert (
        fragment_reader(core, {"style": AddonFragment(addon_file, {"audience": "x"})})("style")
        == "Addon says x."
    )


# --- registration ----------------------------------------------------------------------------


def _addon(
    root: Path, fragment: str, settings: str = "  audience: {type: str, default: people}\n"
) -> None:
    directory = root / "shop"
    (directory / "fragments").mkdir(parents=True)
    (directory / "addon.yaml").write_text(
        "name: shop\nversion: 1.0.0\ncore: '>=0'\nprovides:\n  fragments: fragments/\n"
        f"settings:\n{settings}",
        encoding="utf-8",
    )
    (directory / "fragments" / "style.md").write_text(dedent(fragment), encoding="utf-8")


def _register(root: Path, **settings: object) -> Any:
    addons = load_addons(
        enabled=["shop"], addons_root=root, core_version="0.0.0", config={"shop": settings}
    )
    return register_addons(
        addons, builtin_toolsets=TOOL_PACKAGES, builtin_fragments_dir=None, reserved_channels=()
    )


def test_registration_hands_the_fragment_the_addons_validated_settings(tmp_path: Path) -> None:
    _addon(tmp_path, "For {{setting:audience}}.")

    contributions = _register(tmp_path)

    assert contributions.fragments["style"].read_text(encoding="utf-8") == "For people."


@pytest.mark.parametrize(
    "fragment", ["{{setting:missing}}", "{{if:audience}}\nunclosed", "{{endif}}"]
)
def test_a_fragment_that_cannot_render_stops_startup_naming_the_addon(
    tmp_path: Path, fragment: str
) -> None:
    _addon(tmp_path, fragment)

    with pytest.raises(AddonRegistrationError) as caught:
        _register(tmp_path)

    assert caught.value.addon == "shop" and caught.value.field == "provides.fragments"
    assert "style.md" in str(caught.value)
