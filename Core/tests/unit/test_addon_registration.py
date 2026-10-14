"""A4: what the enabled addons provide is registered through the declared extension points only.

Subject: adapters/driven/addons_fs/registration.py and the seams it feeds in composition.py
(`addon_contributions`, `served_tool_packages`, `load_profiles`, `build_channel_registry`,
`build_container`) plus `collect_audit_policy(contributions=...)`.

THE RULE UNDER TEST (D74). An addon contributes what its manifest `provides` declares, nothing
else; a name another addon or Core owns, or anything undeclared, stops startup with an
`AddonRegistrationError` naming the addon. With no addons the root is exactly what it was.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonError, AddonRegistrationError
from agent_core.adapters.driven.addons_fs.loader import (
    ResolvedAddon,
    load_addons,
    load_addons_from_environment,
)
from agent_core.adapters.driven.addons_fs.registration import (
    EMPTY,
    AddonContributions,
    collect_audit_for,
    register_addons,
)
from agent_core.adapters.driven.persistence_pg.audit_repository import (
    ARGUMENT_ALLOWLIST,
    AuditPolicyConflictError,
    collect_audit_policy,
)
from agent_core.composition import (
    TOOL_PACKAGES,
    Settings,
    addon_contributions,
    build_channel_registry,
    build_container,
    load_profiles,
    served_tool_packages,
)
from agent_core.domain.policy import Effect
from agent_core.domain.profile import ProfileVersionRegistry

_NO_ENV_FILE = Path("/nonexistent/.env")

TOOLSET = '''
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


def {tool}(text: str) -> str:
    """Return the text."""
    return text


def build_toolset() -> FunctionToolset[None]:
    return FunctionToolset([Tool({tool})])
'''


def _addon(root: Path, name: str, manifest: str = "", **files: str) -> None:
    """`files` maps a path with `__` for `/` and `_dot_` for `.` to its text."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\n{dedent(manifest)}", encoding="utf-8"
    )
    for key, text in files.items():
        target = directory / key.replace("__", "/").replace("_dot_", ".")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text), encoding="utf-8")


def _register(
    root: Path,
    enabled: list[str],
    *,
    builtin_fragments_dir: Path | None = None,
    reserved: tuple[str, ...] = ("http", "cli"),
    rule_ids: tuple[str, ...] = (),
    manages_policy: bool = True,
) -> AddonContributions:
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    return register_addons(
        addons,
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=builtin_fragments_dir,
        reserved_channels=reserved,
        builtin_rule_ids=rule_ids,
        manages_policy_files=manages_policy,
    )


def _refused(root: Path, enabled: list[str], **kwargs: object) -> AddonRegistrationError:
    with pytest.raises(AddonRegistrationError) as caught:
        _register(root, enabled, **kwargs)  # type: ignore[arg-type]
    return caught.value


# --- nothing enabled: nothing changes ---------------------------------------------------------


def test_with_no_addons_the_root_is_exactly_what_it_was() -> None:
    settings = Settings.from_env({}, env_file=_NO_ENV_FILE)

    assert settings.addons == ()
    assert addon_contributions(settings) is EMPTY
    assert served_tool_packages(EMPTY) is TOOL_PACKAGES
    assert (
        build_channel_registry(settings).channel_ids()
        == build_channel_registry(settings, ()).channel_ids()
    )
    assert collect_audit_for(TOOL_PACKAGES, EMPTY) == collect_audit_policy(TOOL_PACKAGES)


def test_the_environment_enables_the_addons_the_root_registers() -> None:
    settings = Settings.from_env({"AGENT_CORE_ADDONS": "sample_extra"}, env_file=_NO_ENV_FILE)

    assert [a.manifest.name for a in settings.addons] == ["sample_extra"]
    assert addon_contributions(settings).pull_channels == ("sample_channel",)


# --- the shipped sample addons exercise every extension point --------------------------------


def _samples() -> tuple[ResolvedAddon, ...]:
    return load_addons_from_environment({"AGENT_CORE_ADDONS": "sample_base,sample_extra"})


def test_the_sample_addons_register_one_of_each_extension_point() -> None:
    contributions = register_addons(
        _samples(),
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=("http", "cli"),
    )

    assert set(contributions.toolsets) == {"sample_echo"}
    assert contributions.tools["sample_base"] == {"sample_echo"}
    by_value, by_presence = contributions.audit["sample_base"]
    assert dict(by_value) == {"sample_echo": {"text"}} and not by_presence
    assert [(r.rule_id, r.effect) for r in contributions.policy_rules] == [
        ("sample-base-allow-echo", Effect.ALLOW)
    ]
    assert set(contributions.fragments) == {"sample_note"}
    assert [(a, p.name) for a, p in contributions.profile_files] == [
        ("sample_base", "sample_assistant.yaml")
    ]
    assert contributions.pull_channels == ("sample_channel",)
    assert set(contributions.toolsets["sample_echo"]().tools) == {"sample_echo"}


def test_the_sample_addons_reach_every_seat_of_the_composition_root() -> None:
    settings = Settings(
        addons=_samples(),
        profiles_dir=Path("/nonexistent/profiles"),
        policy_dir=Path("/nonexistent/policy"),
    )
    with pytest.raises(AddonRegistrationError, match="sample_base"):
        addon_contributions(settings)  # the sample brings rules and there is no policy directory

    real = Settings(addons=_samples())
    contributions = addon_contributions(real)
    packages = served_tool_packages(contributions)
    profiles = load_profiles(real.profiles_dir, ProfileVersionRegistry(), contributions)

    assert "sample_echo" in packages and set(TOOL_PACKAGES) < set(packages)
    assert "sample_assistant" in profiles and profiles["sample_assistant"].version >= 1
    assert "A sample prompt fragment" in profiles["sample_assistant"].persona
    assert (
        "sample_channel" in build_channel_registry(real, contributions.pull_channels).channel_ids()
    )
    _, by_presence = collect_audit_for(TOOL_PACKAGES, contributions)
    by_value, _ = collect_audit_for(TOOL_PACKAGES, contributions)
    assert by_value["sample_echo"] == {"text"} and "sample_echo" not in by_presence
    assert by_value["pricing_apply"] == ARGUMENT_ALLOWLIST["pricing_apply"]  # built-ins intact


def test_a_container_builds_with_the_sample_addons_enabled() -> None:
    container = build_container(Settings(addons=_samples()))

    try:
        assert "sample_assistant" in container.profiles
    finally:
        container.domain_pool.close()
        container.audit_pool.close()


# --- toolsets ---------------------------------------------------------------------------------


def test_a_toolset_must_be_a_file_of_the_addon(tmp_path: Path) -> None:
    _addon(tmp_path, "shop", "provides:\n  toolsets: [cart]\n")

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.toolsets"
    assert "toolsets/cart.py" in str(error)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("x = 1\n", "build_toolset"),
        ("def build_toolset():\n    return 3\n", "FunctionToolset"),
        ("raise RuntimeError('boom')\n", "failed to import"),
        (
            TOOLSET.format(tool="cart_add") + "\nAUDIT_ALLOW = {'cart_add': {'text'}}\n",
            "provides.audit",
        ),
    ],
)
def test_a_toolset_module_that_is_not_what_the_manifest_promised_is_refused(
    tmp_path: Path, source: str, reason: str
) -> None:
    _addon(tmp_path, "shop", "provides:\n  toolsets: [cart]\n", toolsets__cart_dot_py=source)

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and reason in str(error)


def test_a_toolset_name_a_builtin_or_another_addon_owns_is_refused(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        "provides:\n  toolsets: [fraud]\n",
        toolsets__fraud_dot_py=TOOLSET.format(tool="shop_tool"),
    )
    assert "Core" in str(_refused(tmp_path, ["shop"]))

    other = tmp_path / "two"
    _addon(
        other,
        "a",
        "provides:\n  toolsets: [cart]\n",
        toolsets__cart_dot_py=TOOLSET.format(tool="a_tool"),
    )
    _addon(
        other,
        "b",
        "provides:\n  toolsets: [cart]\n",
        toolsets__cart_dot_py=TOOLSET.format(tool="b_tool"),
    )
    error = _refused(other, ["a", "b"])
    assert error.addon == "b" and "addon 'a'" in str(error)


def test_a_tool_name_another_source_owns_is_refused_because_policy_and_audit_key_on_it(
    tmp_path: Path,
) -> None:
    builtin_tool = next(iter(TOOL_PACKAGES["fraud"]().tools))
    _addon(
        tmp_path,
        "shop",
        "provides:\n  toolsets: [cart]\n",
        toolsets__cart_dot_py=TOOLSET.format(tool=builtin_tool),
    )

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and builtin_tool in str(error)


# --- audit ------------------------------------------------------------------------------------

SHOP = "provides:\n  toolsets: [cart]\n"


def _shop(root: Path, manifest: str, **files: str) -> None:
    _addon(root, "shop", manifest, toolsets__cart_dot_py=TOOLSET.format(tool="cart_add"), **files)


def test_an_addon_audits_only_the_tools_of_its_own_toolsets(tmp_path: Path) -> None:
    _shop(tmp_path, SHOP + "  audit:\n    allow:\n      pricing_apply: [sku]\n")

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.audit"


def test_an_addons_audit_declaration_goes_through_the_same_guards_as_a_package(
    tmp_path: Path,
) -> None:
    both = (
        SHOP
        + "  audit:\n    allow:\n      cart_add: [text]\n"
        + "    presence_only:\n      cart_add: [text]\n"
    )
    _shop(tmp_path, both)
    contributions = _register(tmp_path, ["shop"])

    with pytest.raises(AddonRegistrationError) as caught:
        collect_audit_for(TOOL_PACKAGES, contributions)

    assert caught.value.addon == "shop" and "both by value and by presence" in str(caught.value)


def test_a_contribution_may_not_redefine_a_decision_that_was_already_made() -> None:
    tool, fields = next(iter(ARGUMENT_ALLOWLIST.items()))

    with pytest.raises(AuditPolicyConflictError) as caught:
        collect_audit_policy(
            {}, contributions={"shop": ({tool: frozenset({*fields, "extra"})}, {})}
        )

    assert caught.value.sources == ("shop",)
    # an identical repeat is accepted, as it is for a package
    assert collect_audit_policy({}, contributions={"shop": ({tool: frozenset(fields)}, {})})


# --- policy -----------------------------------------------------------------------------------


def _rules(*lines: str) -> str:
    return "rules:\n" + "\n".join(f"  {line}" for line in lines) + "\n"


ALLOW_OWN = "- {rule_id: shop-ok, tool_pattern: cart_add, effect: allow, reason: ok}"
POLICY = SHOP + "  policy: policy/rules.yaml\n"


def test_an_addons_rules_are_collected_with_the_effects_they_declare(tmp_path: Path) -> None:
    deny = "- {rule_id: shop-no, tool_pattern: pricing_apply, effect: deny, reason: no}"
    _shop(tmp_path, POLICY, policy__rules_dot_yaml=_rules(ALLOW_OWN, deny))

    contributions = _register(tmp_path, ["shop"])

    assert [r.rule_id for r in contributions.policy_rules] == ["shop-ok", "shop-no"]


@pytest.mark.parametrize(
    "rule",
    [
        "- {rule_id: r, tool_pattern: pricing_apply, effect: allow, reason: x}",
        "- {rule_id: r, tool_pattern: '*', effect: allow, reason: x}",
        "- {rule_id: r, tool_pattern: 'pricing_*', effect: allow, reason: x}",
        "- {rule_id: r, tool_pattern: nothing_here, effect: allow, reason: x}",
    ],
)
def test_an_addon_never_grants_a_tool_it_does_not_own(tmp_path: Path, rule: str) -> None:
    _shop(tmp_path, POLICY, policy__rules_dot_yaml=_rules(rule))

    error = _refused(tmp_path, ["shop"])

    assert error.addon == "shop" and error.field == "provides.policy"


def test_a_rule_id_another_source_declared_is_refused(tmp_path: Path) -> None:
    _shop(tmp_path, POLICY, policy__rules_dot_yaml=_rules(ALLOW_OWN))

    assert "Core's policy files" in str(_refused(tmp_path, ["shop"], rule_ids=("shop-ok",)))


def test_a_malformed_rules_file_names_the_addon(tmp_path: Path) -> None:
    _shop(tmp_path, POLICY, policy__rules_dot_yaml="rules: nope\n")

    assert _refused(tmp_path, ["shop"]).addon == "shop"


def test_rules_need_a_deployment_that_manages_policy_from_files(tmp_path: Path) -> None:
    _shop(tmp_path, POLICY, policy__rules_dot_yaml=_rules(ALLOW_OWN))

    error = _refused(tmp_path, ["shop"], manages_policy=False)

    assert error.addon == "shop" and "does not manage policy" in str(error)


# --- fragments and profiles -------------------------------------------------------------------

PROFILE = """
id: shop_agent
description: Shop agent.
persona: You sell.
include: [shop_voice]
model: m
toolsets: [cart]
max_iterations: 3
max_cost_usd: "0.10"
"""


def test_an_addon_fragment_is_included_by_an_addon_profile(tmp_path: Path) -> None:
    _shop(
        tmp_path,
        SHOP + "  fragments: fragments/\n  profiles: profiles/\n",
        fragments__shop_voice_dot_md="Be brief.\n",
        profiles__shop_agent_dot_yaml=PROFILE,
    )
    contributions = _register(tmp_path, ["shop"])
    empty_dir = tmp_path / "core_profiles"
    empty_dir.mkdir()

    profiles = load_profiles(empty_dir, ProfileVersionRegistry(), contributions)

    assert profiles["shop_agent"].persona == "You sell.\n\nBe brief."


def test_a_profile_id_core_already_has_is_refused_naming_the_addon(tmp_path: Path) -> None:
    _shop(
        tmp_path,
        SHOP + "  fragments: fragments/\n  profiles: profiles/\n",
        fragments__shop_voice_dot_md="Be brief.\n",
        profiles__shop_agent_dot_yaml=PROFILE,
    )
    contributions = _register(tmp_path, ["shop"])
    core = tmp_path / "core_profiles"
    core.mkdir()
    (core / "shop_agent.yaml").write_text(
        PROFILE.replace("include: [shop_voice]\n", ""), encoding="utf-8"
    )

    with pytest.raises(AddonRegistrationError) as caught:
        load_profiles(core, ProfileVersionRegistry(), contributions)

    assert caught.value.addon == "shop" and "shop_agent" in str(caught.value)


def test_an_addon_profile_naming_a_missing_fragment_is_refused_naming_the_addon(
    tmp_path: Path,
) -> None:
    _shop(tmp_path, SHOP + "  profiles: profiles/\n", profiles__shop_agent_dot_yaml=PROFILE)
    contributions = _register(tmp_path, ["shop"])

    with pytest.raises(AddonRegistrationError, match="shop_voice"):
        load_profiles(tmp_path / "core_profiles", ProfileVersionRegistry(), contributions)


def test_a_fragment_name_core_or_another_addon_owns_is_refused(tmp_path: Path) -> None:
    core_fragments = tmp_path / "frag"
    core_fragments.mkdir()
    (core_fragments / "shop_voice.md").write_text("core\n", encoding="utf-8")
    addons = tmp_path / "addons"
    _shop(addons, SHOP + "  fragments: fragments/\n", fragments__shop_voice_dot_md="x\n")
    error = _refused(addons, ["shop"], builtin_fragments_dir=core_fragments)
    assert error.addon == "shop" and "built-in" in str(error)

    twice = tmp_path / "twice"
    for name in ("a", "b"):
        _addon(twice, name, "provides:\n  fragments: f/\n", f__same_dot_md="x\n")
    error = _refused(twice, ["a", "b"])
    assert error.addon == "b" and "addon 'a'" in str(error)


# --- channels ---------------------------------------------------------------------------------


def test_a_channel_core_the_operator_or_another_addon_owns_is_refused(tmp_path: Path) -> None:
    _addon(tmp_path, "a", "provides:\n  channels: [http]\n")
    assert "channel of Core" in str(_refused(tmp_path, ["a"]))

    operator = tmp_path / "op"
    _addon(operator, "a", "provides:\n  channels: [glazed]\n")
    assert _refused(operator, ["a"], reserved=("http", "glazed")).addon == "a"

    both = tmp_path / "both"
    _addon(both, "a", "provides:\n  channels: [voice]\n")
    _addon(both, "b", "provides:\n  channels: [voice]\n")
    error = _refused(both, ["a", "b"])
    assert error.addon == "b" and "addon 'a'" in str(error)


def test_every_failure_is_an_addon_error_naming_the_addon(tmp_path: Path) -> None:
    _addon(tmp_path, "a", "provides:\n  channels: [http]\n")

    with pytest.raises(AddonError, match="addon 'a'"):
        _register(tmp_path, ["a"])
