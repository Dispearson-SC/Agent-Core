"""A1: the `addon.yaml` manifest (roadmap 5.3) is parsed and validated, and a bad one is refused
by naming the addon and the field.

Subject: adapters/driven/addons_fs/manifest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.adapters.driven.addons_fs.errors import ManifestError
from agent_core.adapters.driven.addons_fs.manifest import load_manifest, parse_manifest

FULL = """
name: decision_desk
version: 1.2.0
core: ">=0.0,<1"
depends: [http_tool_bridge]
auto_install: false
provides:
  toolsets: [decisions]
  profiles: profiles/
  fragments: fragments/
  policy: policy/rules.yaml
  audit:
    allow:
      propose_action: [action_id, tier]
    presence_only:
      propose_action: [notes]
  channels: [backend]
settings:
  n1_max_cost: 150
hooks:
  post_init: hooks:post_init
migrations: migrations/
"""

MINIMAL = "name: tiny\nversion: 0.1.0\ncore: '>=0'\n"


def _refused(text: str, field: str, *, addon: str | None = "tiny") -> ManifestError:
    with pytest.raises(ManifestError) as caught:
        parse_manifest(text)
    error = caught.value
    assert error.field == field, str(error)
    assert error.addon == addon, str(error)
    return error


def test_the_roadmap_manifest_parses_into_a_typed_model() -> None:
    manifest = parse_manifest(FULL)

    assert manifest.name == "decision_desk"
    assert manifest.version == "1.2.0"
    assert manifest.core == ">=0.0,<1"
    assert manifest.depends == ("http_tool_bridge",)
    assert manifest.auto_install is False
    assert manifest.provides.toolsets == ("decisions",)
    assert manifest.provides.profiles == "profiles/"
    assert manifest.provides.fragments == "fragments/"
    assert manifest.provides.policy == "policy/rules.yaml"
    assert manifest.provides.audit.allow == {"propose_action": ("action_id", "tier")}
    assert manifest.provides.audit.presence_only == {"propose_action": ("notes",)}
    assert manifest.provides.channels == ("backend",)
    assert manifest.settings == {"n1_max_cost": 150}
    assert manifest.hooks.post_init == "hooks:post_init"
    assert manifest.migrations == "migrations/"


def test_a_minimal_manifest_provides_nothing_and_depends_on_nothing() -> None:
    manifest = parse_manifest(MINIMAL)

    assert manifest.depends == () and manifest.auto_install is False
    assert manifest.provides.toolsets == () and manifest.provides.channels == ()
    assert manifest.provides.profiles is None and manifest.provides.policy is None
    assert manifest.provides.audit.allow == {} and manifest.settings == {}
    assert manifest.hooks.post_init is None and manifest.migrations is None


def test_the_core_range_is_a_checkable_specifier() -> None:
    manifest = parse_manifest(FULL)

    assert manifest.core_spec.contains("0.0.0") and not manifest.core_spec.contains("1.0.0")
    assert str(manifest.parsed_version) == "1.2.0"


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("version: 1.0.0\ncore: '>=0'\n", "name"),
        ("name: tiny\ncore: '>=0'\n", "version"),
        ("name: tiny\nversion: 1.0.0\n", "core"),
        ("name: Tiny\nversion: 1.0.0\ncore: '>=0'\n", "name"),
        ("name: a__b\nversion: 1.0.0\ncore: '>=0'\n", "name"),
        ("name: tiny\nversion: one\ncore: '>=0'\n", "version"),
        ("name: tiny\nversion: 1.0.0\ncore: 'newer than 1'\n", "core"),
        (MINIMAL + "dependz: [x]\n", "dependz"),
        (MINIMAL + "provides:\n  toolsetz: [x]\n", "provides.toolsetz"),
        (MINIMAL + "depends: [x, x]\n", "depends"),
        (MINIMAL + "depends: [tiny]\n", "depends"),
        (MINIMAL + "depends: x\n", "depends"),
        (MINIMAL + "auto_install: true\n", "auto_install"),
        (MINIMAL + "auto_install: maybe\n", "auto_install"),
        (MINIMAL + "provides:\n  toolsets: [Bad Name]\n", "provides.toolsets.0"),
        (MINIMAL + "provides:\n  audit:\n    allow: [a, b]\n", "provides.audit.allow"),
        (MINIMAL + "provides:\n  audit:\n    allow: {t: [1]}\n", "provides.audit.allow.t.0"),
        (MINIMAL + "provides:\n  profiles: ../outside\n", "provides.profiles"),
        (MINIMAL + "provides:\n  policy: /etc/rules.yaml\n", "provides.policy"),
        (MINIMAL + "migrations: a/../../b\n", "migrations"),
        (MINIMAL + "hooks:\n  post_init: not-a-reference\n", "hooks.post_init"),
        (MINIMAL + "hooks:\n  pre_init: m:f\n", "hooks.pre_init"),
        (MINIMAL + "settings: [a]\n", "settings"),
    ],
)
def test_a_bad_manifest_is_refused_naming_the_addon_and_the_field(text: str, field: str) -> None:
    error = _refused(text, field, addon=None if field == "name" else "tiny")

    assert field in str(error)
    if field != "name":
        assert "'tiny'" in str(error)


def test_an_unnamed_addon_is_reported_by_where_it_was_read_from() -> None:
    with pytest.raises(ManifestError, match="somewhere/addon.yaml"):
        parse_manifest("version: 1.0.0\ncore: '>=0'\n", source="somewhere/addon.yaml")


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just text", "name: [unclosed"])
def test_a_document_that_is_not_a_mapping_is_refused(text: str) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(text)


def test_a_manifest_never_echoes_a_value_back() -> None:
    with pytest.raises(ManifestError) as caught:
        parse_manifest(MINIMAL + "auto_install: hunter2-secret\n")

    assert "hunter2-secret" not in str(caught.value)


def test_loading_reads_addon_yaml_from_the_addons_directory(tmp_path: Path) -> None:
    (tmp_path / "tiny").mkdir()
    (tmp_path / "tiny" / "addon.yaml").write_text(MINIMAL)

    assert load_manifest(tmp_path / "tiny").name == "tiny"


def test_a_directory_must_carry_the_name_of_the_addon_it_holds(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "addon.yaml").write_text(MINIMAL)

    with pytest.raises(ManifestError, match="directory 'other'") as caught:
        load_manifest(tmp_path / "other")
    assert caught.value.addon == "tiny" and caught.value.field == "name"


def test_a_directory_without_a_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "tiny").mkdir()

    with pytest.raises(ManifestError, match="no addon.yaml") as caught:
        load_manifest(tmp_path / "tiny")
    assert caught.value.addon == "tiny"


# --- an insert aimed at every profile that declares the slot (G4, D89) ---------------------------


def test_an_insert_may_name_every_profile_with_a_star() -> None:
    manifest = parse_manifest(
        MINIMAL
        + "provides:\n  fragments: fragments/\n"
        + "  inserts: [{profile: '*', slot: plain_style, fragment: style}]\n"
    )

    (insert,) = manifest.provides.inserts
    assert (insert.profile, insert.slot, insert.fragment) == ("*", "plain_style", "style")


@pytest.mark.parametrize("profile", ["'a*'", "'**'", "'*x'", "''"])
def test_a_pattern_other_than_a_lone_star_is_not_a_profile(profile: str) -> None:
    _refused(
        MINIMAL
        + "provides:\n  fragments: fragments/\n"
        + f"  inserts: [{{profile: {profile}, slot: s, fragment: style}}]\n",
        "provides.inserts.0.profile",
    )
