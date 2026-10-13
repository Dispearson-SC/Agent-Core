"""A3: an addon's settings are validated against the schema its manifest declares.

Subject: adapters/driven/addons_fs/settings.py.

THE CHOICE (D72). The schema is DECLARATIVE, inside `addon.yaml`'s `settings:` block, not a pydantic
class the manifest points at: reading a manifest must not import addon code (that would run an
addon before it is enabled, resolved or checked, and the contract that no addon touches the inner
layers could not be tested statically). Values come from the deployment (a YAML file and
`AGENT_CORE_ADDON__<ADDON>__<KEY>` environment variables, env winning) over the manifest's defaults,
and this is the only thing a vertical edits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonSettingsError, ManifestError
from agent_core.adapters.driven.addons_fs.manifest import AddonManifest, parse_manifest
from agent_core.adapters.driven.addons_fs.settings import read_addons_config, resolve_settings

ROADMAP = """
settings:
  tiers:
    n1_max_cost: 150
    n1_min_confidence: 0.85
  actions_catalog: actions.yaml
  verbose: false
"""

TYPED = """
settings:
  threshold:
    type: int
    default: 5
    minimum: 1
    maximum: 10
  ratio: {type: float, default: 0.5, minimum: 0, maximum: 1}
  mode: {type: str, default: fast, choices: [fast, slow]}
  tags: {type: list, items: str, default: []}
  limits: {type: list, items: int, default: [1, 2]}
  token: {type: str}
  nested:
    type: object
    fields:
      depth: {type: int, default: 2}
"""


def _m(body: str, name: str = "demo") -> AddonManifest:
    return parse_manifest(f"name: {name}\nversion: 1.0.0\ncore: '>=0'\n{body}")


def _resolve(
    body: str,
    config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    name: str = "demo",
) -> dict[str, Any]:
    return dict(
        resolve_settings([_m(body, name)], config=config or {}, env=env or {})[name]
    )


def _refused(
    body: str,
    config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> AddonSettingsError:
    with pytest.raises(AddonSettingsError) as caught:
        _resolve(body, config, env)
    return caught.value


# --- defaults ------------------------------------------------------------------------------------


def test_the_manifests_defaults_are_the_settings_when_the_deployment_says_nothing() -> None:
    assert _resolve(ROADMAP) == {
        "tiers": {"n1_max_cost": 150, "n1_min_confidence": 0.85},
        "actions_catalog": "actions.yaml",
        "verbose": False,
    }


def test_an_explicit_schema_gives_typed_defaults() -> None:
    values = _resolve(TYPED, config={"demo": {"token": "t"}})

    assert values == {
        "threshold": 5,
        "ratio": 0.5,
        "mode": "fast",
        "tags": (),
        "limits": (1, 2),
        "token": "t",
        "nested": {"depth": 2},
    }


def test_an_addon_without_settings_has_none() -> None:
    assert _resolve("") == {}


def test_each_addon_has_its_own_settings() -> None:
    first, second = _m("settings:\n  n: 1\n", "first"), _m("settings:\n  n: 2\n", "second")

    result = resolve_settings(
        [first, second], config={"second": {"n": 9}}, env={"AGENT_CORE_ADDON__FIRST__N": "7"}
    )

    assert dict(result["first"]) == {"n": 7} and dict(result["second"]) == {"n": 9}


def test_the_result_cannot_be_edited_by_the_code_that_receives_it() -> None:
    manifest, config = _m(TYPED), {"demo": {"token": "t"}}
    values = resolve_settings([manifest], config=config, env={})["demo"]

    with pytest.raises(TypeError):
        values["threshold"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        values["nested"]["depth"] = 1  # type: ignore[index]


# --- sources and precedence ----------------------------------------------------------------------


def test_the_deployment_file_overrides_the_default_and_the_environment_overrides_the_file() -> None:
    config = {"demo": {"token": "t", "threshold": 7, "nested": {"depth": 4}}}
    env = {"AGENT_CORE_ADDON__DEMO__THRESHOLD": "9"}

    values = _resolve(TYPED, config, env)

    assert values["threshold"] == 9 and values["nested"] == {"depth": 4}


def test_the_environment_reaches_nested_keys_and_parses_each_type() -> None:
    env = {
        "AGENT_CORE_ADDON__DEMO__TOKEN": "abc",
        "AGENT_CORE_ADDON__DEMO__RATIO": "0.25",
        "AGENT_CORE_ADDON__DEMO__TAGS": "a, b ,c",
        "AGENT_CORE_ADDON__DEMO__LIMITS": "3,4",
        "AGENT_CORE_ADDON__DEMO__NESTED__DEPTH": "6",
    }

    values = _resolve(TYPED, env=env)

    assert values["ratio"] == 0.25 and values["tags"] == ("a", "b", "c")
    assert values["limits"] == (3, 4) and values["nested"] == {"depth": 6}


def test_a_boolean_in_the_environment_is_true_or_false_and_nothing_else() -> None:
    body = "settings:\n  verbose: false\n"

    assert _resolve(body, env={"AGENT_CORE_ADDON__DEMO__VERBOSE": "true"}) == {"verbose": True}
    error = _refused(body, env={"AGENT_CORE_ADDON__DEMO__VERBOSE": "yes"})
    assert error.addon == "demo" and error.field == "verbose"


def test_an_addon_name_with_underscores_is_addressed_by_its_upper_case_name() -> None:
    result = resolve_settings(
        [_m("settings:\n  n: 1\n", "sample_base")],
        config={},
        env={"AGENT_CORE_ADDON__SAMPLE_BASE__N": "5"},
    )

    assert dict(result["sample_base"]) == {"n": 5}


# --- invalid settings fail naming the addon and the key ------------------------------------------


@pytest.mark.parametrize(
    ("config", "env", "field"),
    [
        ({"token": "t", "threshold": "five"}, {}, "threshold"),
        ({"token": "t", "threshold": 5.5}, {}, "threshold"),
        ({"token": "t", "threshold": True}, {}, "threshold"),
        ({"token": "t", "threshold": 0}, {}, "threshold"),
        ({"token": "t", "threshold": 11}, {}, "threshold"),
        ({"token": "t", "ratio": 1.5}, {}, "ratio"),
        ({"token": "t", "mode": "medium"}, {}, "mode"),
        ({"token": "t", "tags": "a"}, {}, "tags"),
        ({"token": "t", "limits": [1, "x"]}, {}, "limits.1"),
        ({"token": "t", "nested": {"depth": "deep"}}, {}, "nested.depth"),
        ({"token": "t", "nested": 3}, {}, "nested"),
        ({"token": "t", "threshhold": 3}, {}, "threshhold"),
        ({"token": "t", "nested": {"depht": 3}}, {}, "nested.depht"),
        ({}, {}, "token"),
        ({"token": "t"}, {"AGENT_CORE_ADDON__DEMO__THRESHOLD": "five"}, "threshold"),
        ({"token": "t"}, {"AGENT_CORE_ADDON__DEMO__THRESHOLD": "50"}, "threshold"),
        ({"token": "t"}, {"AGENT_CORE_ADDON__DEMO__LIMITS": "1,x"}, "limits"),
        ({"token": "t"}, {"AGENT_CORE_ADDON__DEMO__NOPE": "1"}, "nope"),
        ({"token": "t"}, {"AGENT_CORE_ADDON__DEMO__NESTED__NOPE": "1"}, "nested.nope"),
        ({"token": "t"}, {"AGENT_CORE_ADDON__DEMO__NESTED": "1"}, "nested"),
    ],
)
def test_an_invalid_setting_fails_naming_the_addon_and_the_key(
    config: dict[str, Any], env: dict[str, str], field: str
) -> None:
    error = _refused(TYPED, {"demo": config}, env)

    assert error.addon == "demo" and error.field == field, str(error)
    assert "'demo'" in str(error) and repr(field) in str(error)


def test_a_wrong_value_is_never_echoed_back() -> None:
    body = "settings:\n  token: {type: str, choices: [a, b]}\n"

    error = _refused(body, {"demo": {"token": "hunter2-secret"}})

    assert "hunter2-secret" not in str(error)
    error = _refused(
        "settings:\n  n: 1\n", env={"AGENT_CORE_ADDON__DEMO__N": "hunter2-secret"}
    )
    assert "hunter2-secret" not in str(error)


def test_deployment_settings_for_an_addon_that_is_not_active_fail() -> None:
    with pytest.raises(AddonSettingsError, match="'ghost'.*not enabled"):
        resolve_settings([_m("")], config={"ghost": {"a": 1}}, env={})
    with pytest.raises(AddonSettingsError, match="'ghost'.*not enabled"):
        resolve_settings([_m("")], config={}, env={"AGENT_CORE_ADDON__GHOST__A": "1"})


def test_the_environment_variables_of_other_settings_are_not_ours() -> None:
    env = {
        "AGENT_CORE_ADDONS": "demo",
        "AGENT_CORE_ADDONS_DIR": "/x",
        "AGENT_CORE_PULL_CHANNELS": "a",
    }

    assert _resolve("settings:\n  n: 1\n", env=env) == {"n": 1}


# --- a bad schema in the manifest is a manifest error --------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("settings:\n  n: {type: complex}\n", "settings.n.type"),
        ("settings:\n  n: {type: int, default: x}\n", "settings.n.default"),
        ("settings:\n  n: {type: int, default: 3, minimum: 5}\n", "settings.n.default"),
        ("settings:\n  n: {type: int, defualt: 3}\n", "settings.n.defualt"),
        ("settings:\n  n: {type: str, minimum: 1}\n", "settings.n.minimum"),
        ("settings:\n  n: {type: list, default: []}\n", "settings.n.items"),
        ("settings:\n  n: {type: list, items: object}\n", "settings.n.items"),
        ("settings:\n  n: {type: object}\n", "settings.n.fields"),
        ("settings:\n  n: {type: int, choices: [1, x]}\n", "settings.n.choices"),
        ("settings:\n  n: []\n", "settings.n"),
        ("settings:\n  n: null\n", "settings.n"),
        ("settings:\n  Big: 1\n", "settings.Big"),
        ("settings:\n  a__b: 1\n", "settings.a__b"),
        ("settings:\n  g:\n    Big: 1\n", "settings.g.Big"),
    ],
)
def test_a_bad_schema_is_a_manifest_error_naming_the_addon_and_the_setting(
    body: str, field: str
) -> None:
    with pytest.raises(ManifestError) as caught:
        resolve_settings([_m(body)], config={}, env={})

    assert caught.value.addon == "demo" and caught.value.field == field, str(caught.value)


# --- the deployment file -------------------------------------------------------------------------


def test_the_deployment_file_is_a_mapping_of_addon_to_settings(tmp_path: Path) -> None:
    path = tmp_path / "addons.yaml"
    path.write_text("sample_base:\n  max_items: 4\n", encoding="utf-8")

    assert read_addons_config(path) == {"sample_base": {"max_items": 4}}


@pytest.mark.parametrize("text", ["- a\n", "sample_base: 3\n", "a: [", "sample_base:\n  - x\n"])
def test_a_deployment_file_of_the_wrong_shape_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "addons.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(AddonSettingsError):
        read_addons_config(path)


def test_a_missing_deployment_file_is_refused_not_ignored(tmp_path: Path) -> None:
    with pytest.raises(AddonSettingsError, match="nope.yaml"):
        read_addons_config(tmp_path / "nope.yaml")
