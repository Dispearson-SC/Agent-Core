"""Phase 2 exit criterion (A8, D82), part 1: what the samples prove, and what addons cannot do.

  * a bridge (`auto_install`) activates when ALL its dependencies are enabled, and not otherwise;
  * the sample addons TOGETHER exercise every extension point and are loaded through the real
    loader, registration and composition root;
  * an addon cannot grant or allow a tool outside the extension points it declares: a policy
    `allow` reaching a tool it does not own, an audit entry for a foreign tool, a wrapper of an
    unrelated addon, an insert of a foreign fragment, a name Core already owns, and a manifest key
    that is not an extension point are all refused at startup, naming the addon;
  * a file an addon ships but does not declare is inert: it is never imported.

The static "no addon touches domain/application/ports" check is `test_addon_boundary.py`; the
install state against a real database is `test_addon_phase2_install.py`.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest
import yaml

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError, ManifestError
from agent_core.adapters.driven.addons_fs.loader import load_addons, load_addons_from_environment
from agent_core.adapters.driven.addons_fs.manifest import load_manifest
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.composition import TOOL_PACKAGES, Settings, build_container

pytestmark = [pytest.mark.contract, pytest.mark.phase("2")]

ADDONS_ROOT = Path(__file__).resolve().parents[3] / "addons"
# What an operator would put in AGENT_CORE_ADDONS to run every sample. `sample_bridge` is NOT
# listed: it activates by itself.
ENABLED = ("sample_base", "sample_extra", "sample_shout", "sample_install")


def _loaded(enabled: tuple[str, ...] = ENABLED):  # type: ignore[no-untyped-def]
    return load_addons_from_environment({"AGENT_CORE_ADDONS": ",".join(enabled)})


# --- the bridge ----------------------------------------------------------------------------------


def test_a_bridge_activates_when_all_its_dependencies_are_enabled() -> None:
    loaded = _loaded()

    bridge = next(a for a in loaded if a.manifest.name == "sample_bridge")
    assert bridge.source == "auto_install" and bridge.manifest.auto_install
    names = [a.manifest.name for a in loaded]
    for dependency in bridge.manifest.depends:  # dependencies are activated before the bridge
        assert names.index(dependency) < names.index("sample_bridge")


def test_a_bridge_does_not_activate_unless_every_dependency_is_enabled() -> None:
    dependencies = load_manifest(ADDONS_ROOT / "sample_bridge").depends
    assert len(dependencies) >= 2, "the sample bridge must join at least two addons"

    for size in range(len(dependencies)):
        for subset in itertools.combinations(dependencies, size):
            names = [a.manifest.name for a in _loaded(subset)] if subset else []
            assert "sample_bridge" not in names, subset


def test_nothing_is_enabled_by_default_and_a_bridge_cannot_enable_itself() -> None:
    assert load_addons_from_environment({}) == ()
    assert load_addons_from_environment({"AGENT_CORE_ADDONS": ""}) == ()


# --- the samples cover every extension point -----------------------------------------------------


def test_the_sample_addons_together_declare_every_extension_point() -> None:
    manifests = [load_manifest(ADDONS_ROOT / a.manifest.name) for a in _loaded()]
    declared: set[str] = set()
    for manifest in manifests:
        provides = manifest.provides
        for key in (
            "toolsets",
            "profiles",
            "fragments",
            "policy",
            "channels",
            "inserts",
            "wrappers",
        ):
            if getattr(provides, key):
                declared.add(f"provides.{key}")
        if provides.audit.allow:
            declared.add("provides.audit.allow")
        if provides.audit.presence_only:
            declared.add("provides.audit.presence_only")
        if manifest.settings:
            declared.add("settings")
        if manifest.migrations:
            declared.add("migrations")
        if manifest.hooks.post_init:
            declared.add("hooks.post_init")
        if manifest.auto_install:
            declared.add("auto_install")
        if manifest.depends:
            declared.add("depends")

    assert declared == {
        "provides.toolsets",
        "provides.profiles",
        "provides.fragments",
        "provides.policy",
        "provides.channels",
        "provides.inserts",
        "provides.wrappers",
        "provides.audit.allow",
        "provides.audit.presence_only",
        "settings",
        "migrations",
        "hooks.post_init",
        "auto_install",
        "depends",
    }


def test_the_sample_profiles_exercise_inheritance_and_slots() -> None:
    profile_files = sorted(ADDONS_ROOT.glob("*/profiles/*.yaml"))
    documents = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in profile_files]

    assert any("inherit" in document for document in documents)
    assert any(document.get("slots") for document in documents)
    assert any("{{slot:" in document.get("persona", "") for document in documents)


def test_the_samples_register_into_every_seam_of_the_composition_root() -> None:
    from agent_core.composition import addon_contributions

    settings = Settings(addons=_loaded())
    contributions = addon_contributions(settings)

    assert set(contributions.toolsets) == {"sample_echo", "sample_notes"}
    assert {"sample_base", "sample_install"} <= set(contributions.audit)
    by_value, by_presence = contributions.audit["sample_install"]
    assert by_value and by_presence
    assert contributions.policy_rules and contributions.fragments and contributions.profile_files
    assert contributions.pull_channels == ("sample_channel",)
    assert [i[0] for i in contributions.inserts] == ["sample_bridge"]
    assert [w.addon for w in contributions.wrappers] == ["sample_shout"]


def test_the_composition_root_serves_the_samples_through_one_container() -> None:
    addons = _loaded()
    container = build_container(Settings(addons=addons))
    try:
        specialist = container.profiles["sample_specialist"]
        assistant = container.profiles["sample_assistant"]
        assert specialist.max_iterations == 3 and assistant.max_iterations == 5  # inherit + patch
        assert "Inserted by the sample_bridge addon" in assistant.persona  # slot insert
        assert "{{slot:" not in assistant.persona
        assert container.profiles["sample_notekeeper"].toolsets == ("sample_notes",)
        values = {a.manifest.name: a.settings for a in addons}
        assert values["sample_base"]["greeting"] == "hello"  # settings defaults validated
        for profile, tool in (
            (assistant, "sample_echo"),
            (container.profiles["sample_notekeeper"], "sample_note_add"),
        ):
            toolset = asyncio.run(container.tools.toolset_for(profile))
            assert tool in asyncio.run(_tool_names(toolset))
    finally:
        container.domain_pool.close()
        container.audit_pool.close()


async def _tool_names(toolset: object) -> set[str]:
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    context = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    return set(await toolset.get_tools(context))  # type: ignore[attr-defined]


# --- an addon cannot go beyond its declared extension points -------------------------------------


def _write(root: Path, name: str, manifest: str, files: dict[str, str] | None = None) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\n{manifest}", encoding="utf-8"
    )
    for relative, text in (files or {}).items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


TOOLSET = """
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

def {tool}(text: str) -> str:
    \"\"\"Echo.\"\"\"
    return text

def build_toolset():
    return FunctionToolset([Tool({tool})])
"""
WRAP = "async def wrap(original, arguments, ctx):\n    return await original()\n"


def _rules(pattern: str, effect: str = "allow") -> str:
    return (
        "rules:\n"
        f"  - {{rule_id: r-1, tool_pattern: '{pattern}', effect: {effect}, reason: because}}\n"
    )


def _own_toolset(
    root: Path, name: str, tool: str, manifest: str = "", files: dict[str, str] | None = None
) -> None:
    _write(
        root,
        name,
        f"provides:\n  toolsets: [{tool}_set]\n" + manifest,
        {f"toolsets/{tool}_set.py": TOOLSET.format(tool=tool), **(files or {})},
    )


def _register(root: Path, enabled: list[str]):  # type: ignore[no-untyped-def]
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    return register_addons(
        addons,
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels={"http", "cli"},
    )


def _core_tool() -> str:
    return sorted(next(iter(TOOL_PACKAGES.values()))().tools)[0]


def _refused(root: Path, enabled: list[str]) -> AddonRegistrationError:
    with pytest.raises(AddonRegistrationError) as caught:
        _register(root, enabled)
    return caught.value


def test_an_addon_policy_cannot_allow_a_tool_of_core(tmp_path: Path) -> None:
    _own_toolset(
        tmp_path,
        "shop",
        "own_tool",
        "  policy: rules.yaml\n",
        {"rules.yaml": _rules(_core_tool())},
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.policy"
    assert "grants only its own" in str(error)


def test_an_addon_policy_cannot_allow_a_tool_of_another_addon(tmp_path: Path) -> None:
    _own_toolset(tmp_path, "base", "base_tool")
    _own_toolset(
        tmp_path, "shop", "shop_tool", "  policy: rules.yaml\n", {"rules.yaml": _rules("base_tool")}
    )

    error = _refused(tmp_path, ["base", "shop"])

    assert error.addon == "shop" and "base_tool" in str(error)


def test_an_addon_policy_cannot_allow_a_tool_nobody_provides(tmp_path: Path) -> None:
    _own_toolset(
        tmp_path, "shop", "own_tool", "  policy: rules.yaml\n", {"rules.yaml": _rules("ghost_tool")}
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and "none of the addon's own tools" in str(error)


@pytest.mark.parametrize("pattern", ["*", "own_*"])
def test_a_wildcard_allow_that_reaches_a_foreign_tool_is_refused(
    tmp_path: Path, pattern: str
) -> None:
    _own_toolset(tmp_path, "base", "own_other")
    _own_toolset(
        tmp_path, "shop", "own_tool", "  policy: rules.yaml\n", {"rules.yaml": _rules(pattern)}
    )

    error = _refused(tmp_path, ["base", "shop"])

    assert error.addon == "shop" and error.field == "provides.policy"


@pytest.mark.parametrize("effect", ["deny", "needs_approval"])
def test_an_addon_may_narrow_any_tool_but_grant_only_its_own(tmp_path: Path, effect: str) -> None:
    _own_toolset(
        tmp_path,
        "shop",
        "own_tool",
        "  policy: rules.yaml\n",
        {"rules.yaml": _rules(_core_tool(), effect)},
    )

    contributions = _register(tmp_path, ["shop"])

    assert [rule.effect.value for rule in contributions.policy_rules] == [effect]


def test_an_addon_cannot_record_audit_arguments_of_a_tool_it_does_not_own(tmp_path: Path) -> None:
    _own_toolset(
        tmp_path, "shop", "own_tool", f"  audit:\n    allow:\n      {_core_tool()}: [secret]\n"
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.audit"


def test_a_toolset_module_cannot_declare_audit_outside_the_manifest(tmp_path: Path) -> None:
    _own_toolset(tmp_path, "shop", "own_tool")
    module = tmp_path / "shop" / "toolsets" / "own_tool_set.py"
    module.write_text(module.read_text() + "\nAUDIT_ALLOW = {'own_tool': ['text']}\n")

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and "AUDIT_ALLOW" in str(error)


def test_an_addon_cannot_provide_a_tool_core_already_owns(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "shop",
        "provides:\n  toolsets: [mine]\n",
        {"toolsets/mine.py": TOOLSET.format(tool=_core_tool())},
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and "Core" in str(error)


def test_an_addon_cannot_wrap_a_tool_of_an_addon_it_does_not_depend_on(tmp_path: Path) -> None:
    _own_toolset(tmp_path, "base", "base_tool")
    _write(
        tmp_path,
        "shop",
        "provides:\n  wrappers: [{tool: base_tool, wrapper: w}]\n",
        {"wrappers/w.py": WRAP},
    )

    error = _refused(tmp_path, ["base", "shop"])

    assert error.addon == "shop" and error.field == "provides.wrappers"
    assert "depend" in str(error)


def test_an_addon_cannot_insert_text_it_did_not_declare_as_its_own_fragment(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "base", "provides:\n  fragments: fragments/\n", {"fragments/note.md": "x"})
    _write(
        tmp_path,
        "shop",
        "depends: [base]\nprovides:\n  inserts: [{profile: p, slot: s, fragment: note}]\n",
    )

    error = _refused(tmp_path, ["base", "shop"])

    assert error.addon == "shop" and error.field == "provides.inserts"


def test_an_addon_cannot_take_a_channel_core_owns(tmp_path: Path) -> None:
    _write(tmp_path, "shop", "provides:\n  channels: [http]\n")

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.channels"
    assert "Core" in str(error)


@pytest.mark.parametrize("key", ["domain", "ports", "application", "tool_overrides"])
def test_a_manifest_cannot_declare_an_extension_point_that_does_not_exist(
    tmp_path: Path, key: str
) -> None:
    _write(tmp_path, "shop", f"provides:\n  {key}: [anything]\n")

    with pytest.raises(ManifestError) as caught:
        load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")

    assert caught.value.field == f"provides.{key}" and "not a manifest key" in str(caught.value)


def test_a_toplevel_manifest_key_that_is_not_an_extension_point_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "shop", "patches: [domain/policy.py]\n")

    with pytest.raises(ManifestError, match="not a manifest key"):
        load_addons(enabled=["shop"], addons_root=tmp_path, core_version="0.0.0")


def test_a_file_an_addon_ships_but_does_not_declare_is_never_imported(tmp_path: Path) -> None:
    marker = tmp_path / "imported.log"
    boom = (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nraise RuntimeError\n"
    )
    _write(
        tmp_path,
        "shop",
        "",
        {"toolsets/ghost.py": boom, "wrappers/ghost.py": boom, "hooks.py": boom},
    )

    contributions = _register(tmp_path, ["shop"])

    assert contributions.wrappers == () and "ghost" not in contributions.toolsets
    assert not marker.exists()
