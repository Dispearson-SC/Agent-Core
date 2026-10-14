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
from agent_core.adapters.driven.profiles_fs.fragments import (
    AddonFragment,
    FragmentFile,
    render_fragment,
)
from agent_core.adapters.driven.profiles_fs.loader import fragment_reader, parse_profile
from agent_core.composition import TOOL_PACKAGES, load_profiles

SETTINGS = {
    "audience": "a shop manager",
    "voice": True,
    "quiet": False,
    "limit": 3,
    "empty": "",
    "terms": ["alpha", "beta"],
    "none": [],
}


def test_a_setting_replaces_its_placeholder_inline() -> None:
    text = "Write for {{setting:audience}}, at most {{setting:limit}} lines."

    assert render_fragment(text, SETTINGS) == "Write for a shop manager, at most 3 lines."


def test_a_list_setting_reads_as_a_comma_separated_enumeration_and_empty_is_false() -> None:
    assert render_fragment("Avoid: {{setting:terms}}.", SETTINGS) == "Avoid: alpha, beta."
    assert render_fragment("{{if:none}}\nx\n{{endif}}\ny", SETTINGS) == "y"
    assert render_fragment("{{if:terms}}\nx\n{{endif}}", SETTINGS) == "x"


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


def test_the_text_served_is_the_text_registration_checked(tmp_path: Path) -> None:
    """Slice 29 advisory (registration.py:372-376): the check rendered the file and threw the text
    away, so every later read rendered the file AGAIN. A fragment edited or removed after startup
    then reached a profile unchecked, or failed at profile load with a bare `ValueError` /
    `OSError` naming no addon. The text registration validated is the text served."""
    _addon(tmp_path, "For {{setting:audience}}.")
    contributions = _register(tmp_path)
    path = tmp_path / "shop" / "fragments" / "style.md"

    path.write_text("Now {{setting:missing}}.", encoding="utf-8")
    assert contributions.fragments["style"].read_text(encoding="utf-8") == "For people."
    path.unlink()
    assert contributions.fragments["style"].is_file()
    assert contributions.fragments["style"].read_text(encoding="utf-8") == "For people."


def _addon_with_profile(root: Path) -> None:
    """`shop` with a fragment, a profile that includes it and an insert into a Core profile."""
    _addon(root, "For {{setting:audience}}.")
    directory = root / "shop"
    (directory / "addon.yaml").write_text(
        "name: shop\nversion: 1.0.0\ncore: '>=0'\nprovides:\n  fragments: fragments/\n"
        "  profiles: profiles/\n"
        "  inserts:\n    - {profile: core_helper, slot: style, fragment: style}\n"
        "settings:\n  audience: {type: str, default: people}\n",
        encoding="utf-8",
    )
    (directory / "profiles").mkdir()
    (directory / "profiles" / "shop_helper.yaml").write_text(
        "id: shop_helper\nmodel: minimax/MiniMax-M3\npersona: SHOP\ninclude: [style]\n",
        encoding="utf-8",
    )


def test_the_checked_fragment_is_declared_a_fragment_file_and_nothing_wider(
    tmp_path: Path,
) -> None:
    """Slice 30 advisory (registration.py:381-404): the checked fragment duck-typed the loader's
    interface, so nothing tied it to what consumers read. It now declares `FragmentFile` itself:
    mypy checks it against the protocol where it is defined, and every Core consumer of an addon
    fragment reads it only through that protocol (the tests below run each one)."""
    _addon(tmp_path, "For {{setting:audience}}.")

    fragment = _register(tmp_path).fragments["style"]

    assert FragmentFile in type(fragment).__mro__
    assert not hasattr(fragment, "settings"), "not an AddonFragment: it never renders again"


def test_the_checked_text_is_utf8_and_another_encoding_is_refused(tmp_path: Path) -> None:
    """The text was decoded once, as UTF-8. Answering a request for another encoding with it
    would be a silent lie, so it is refused; `None` and any spelling of UTF-8 are served."""
    _addon(tmp_path, "For {{setting:audience}}.")
    fragment = _register(tmp_path).fragments["style"]

    assert fragment.read_text() == fragment.read_text(encoding="UTF-8") == "For people."
    with pytest.raises(ValueError, match="latin-1"):
        fragment.read_text(encoding="latin-1")


def test_every_consumer_of_an_addon_fragment_is_served_the_checked_text(tmp_path: Path) -> None:
    """The two Core consumers: a profile's `include` and a slot insert, through `load_profiles`."""
    _addon_with_profile(tmp_path / "addons")
    contributions = _register(tmp_path / "addons")
    core = tmp_path / "core_profiles"
    core.mkdir()
    (core / "core_helper.yaml").write_text(
        "id: core_helper\nmodel: minimax/MiniMax-M3\nslots: {style: []}\n"
        "persona: |-\n  CORE\n  {{slot:style}}\n",
        encoding="utf-8",
    )
    (tmp_path / "addons" / "shop" / "fragments" / "style.md").unlink()

    served = load_profiles(core, None, contributions)

    assert served["shop_helper"].persona == "SHOP\n\nFor people."
    assert "For people." in served["core_helper"].persona
