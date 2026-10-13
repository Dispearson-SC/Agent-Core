"""A2: the enabled addons are resolved into one ordered, compatible set, or startup fails.

Subject: adapters/driven/addons_fs/resolver.py (pure: manifests in, ordered set out).

THE RULES (roadmap 5.2/5.4). An addon runs only if it is in the explicit list. Dependencies are
NOT pulled in: a missing one is an error that says to enable it. A bridge (`auto_install`) is the
one addon that is not listed: it activates when ALL its dependencies are active, and never
otherwise.
"""

from __future__ import annotations

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonResolutionError
from agent_core.adapters.driven.addons_fs.manifest import AddonManifest, parse_manifest
from agent_core.adapters.driven.addons_fs.resolver import parse_enabled, resolve_addons


def _m(
    name: str,
    depends: tuple[str, ...] = (),
    *,
    bridge: bool = False,
    core: str = ">=0.0,<1",
) -> AddonManifest:
    lines = [f"name: {name}", "version: 1.0.0", f"core: '{core}'"]
    if depends:
        lines.append(f"depends: [{', '.join(depends)}]")
    if bridge:
        lines.append("auto_install: true")
    return parse_manifest("\n".join(lines) + "\n")


def _available(*manifests: AddonManifest) -> dict[str, AddonManifest]:
    return {m.name: m for m in manifests}


def _resolve(enabled: list[str], *manifests: AddonManifest, core: str = "0.0.0") -> list[str]:
    result = resolve_addons(enabled=enabled, available=_available(*manifests), core_version=core)
    return [r.manifest.name for r in result]


def test_a_dependency_is_ordered_before_the_addon_that_needs_it() -> None:
    base, top = _m("base"), _m("top", ("base",))

    assert _resolve(["top", "base"], base, top) == ["base", "top"]


def test_independent_addons_keep_the_order_they_were_listed_in() -> None:
    a, b, c = _m("a"), _m("b"), _m("c")

    assert _resolve(["c", "a", "b"], a, b, c) == ["c", "a", "b"]


def test_nothing_is_enabled_unless_it_is_listed() -> None:
    assert _resolve([], _m("a"), _m("b")) == []
    assert _resolve(["a"], _m("a"), _m("b")) == ["a"]


def test_a_missing_dependency_fails_and_says_to_enable_it() -> None:
    with pytest.raises(AddonResolutionError) as caught:
        _resolve(["top"], _m("top", ("base",)), _m("base"))  # base exists, but is not enabled

    assert caught.value.addon == "top" and caught.value.field == "base"
    assert "depends on 'base'" in str(caught.value) and "ADDONS" in str(caught.value)


def test_a_cycle_fails_and_names_the_path() -> None:
    with pytest.raises(AddonResolutionError, match="a -> b -> c -> a"):
        _resolve(["a", "b", "c"], _m("a", ("b",)), _m("b", ("c",)), _m("c", ("a",)))


def test_an_incompatible_core_version_fails_naming_both() -> None:
    with pytest.raises(AddonResolutionError) as caught:
        _resolve(["old"], _m("old", core=">=1.0,<2"), core="0.0.0")

    assert caught.value.addon == "old"
    assert ">=1.0,<2" in str(caught.value) and "0.0.0" in str(caught.value)


def test_an_unlisted_addon_with_an_incompatible_core_is_not_looked_at() -> None:
    assert _resolve(["a"], _m("a"), _m("old", core=">=9")) == ["a"]


def test_an_unknown_core_version_is_refused_not_guessed() -> None:
    with pytest.raises(AddonResolutionError, match="Core version"):
        _resolve(["a"], _m("a"), core="not-a-version")


def test_a_listed_name_that_has_no_manifest_fails() -> None:
    with pytest.raises(AddonResolutionError, match="'ghost'"):
        _resolve(["ghost"], _m("a"))


def test_a_bridge_activates_when_all_its_dependencies_are_enabled() -> None:
    a, b, bridge = _m("a"), _m("b"), _m("a_b", ("a", "b"), bridge=True)

    result = resolve_addons(
        enabled=["a", "b"], available=_available(a, b, bridge), core_version="0.0.0"
    )

    assert [(r.manifest.name, r.source) for r in result] == [
        ("a", "explicit"),
        ("b", "explicit"),
        ("a_b", "auto_install"),
    ]


def test_a_bridge_never_activates_with_only_some_of_its_dependencies() -> None:
    a, b, bridge = _m("a"), _m("b"), _m("a_b", ("a", "b"), bridge=True)

    assert _resolve(["a"], a, b, bridge) == ["a"]
    assert _resolve(["b"], a, b, bridge) == ["b"]
    assert _resolve([], a, b, bridge) == []


def test_a_bridge_of_a_bridge_activates_in_dependency_order() -> None:
    a, b = _m("a"), _m("b")
    first = _m("a_b", ("a", "b"), bridge=True)
    second = _m("a_b_a", ("a_b", "a"), bridge=True)

    assert _resolve(["b", "a"], a, b, second, first) == ["b", "a", "a_b", "a_b_a"]


def test_a_bridge_waiting_on_a_bridge_that_never_activates_stays_off() -> None:
    a, b = _m("a"), _m("b")
    first = _m("a_b", ("a", "b"), bridge=True)
    second = _m("a_b_a", ("a_b", "a"), bridge=True)

    assert _resolve(["a"], a, b, first, second) == ["a"]


def test_a_bridge_with_an_incompatible_core_fails_instead_of_silently_staying_off() -> None:
    a = _m("a")
    bridge = _m("a_x", ("a",), bridge=True, core=">=9")

    with pytest.raises(AddonResolutionError) as caught:
        _resolve(["a"], a, bridge)

    assert caught.value.addon == "a_x"


def test_a_bridge_that_is_listed_explicitly_is_required_like_any_other() -> None:
    a, bridge = _m("a"), _m("a_b", ("a", "b"), bridge=True)

    with pytest.raises(AddonResolutionError, match="depends on 'b'"):
        _resolve(["a_b", "a"], a, bridge, _m("b"))
    assert [r.source for r in resolve_addons(
        enabled=["a", "b", "a_b"],
        available=_available(a, _m("b"), bridge),
        core_version="0.0.0",
    )] == ["explicit", "explicit", "explicit"]


# --- the explicit list -------------------------------------------------------------------------


def test_the_list_is_comma_separated_and_trimmed() -> None:
    assert parse_enabled(" a, b ,c ") == ("a", "b", "c")
    assert parse_enabled("") == () and parse_enabled("  ") == ()


@pytest.mark.parametrize("raw", ["a,,b", "a,a", "a b", "A", "a,../b", "a/b"])
def test_a_malformed_list_is_refused(raw: str) -> None:
    with pytest.raises(AddonResolutionError):
        parse_enabled(raw)
