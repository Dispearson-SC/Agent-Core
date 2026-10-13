"""CHARACTERIZATION (written after the loader; the samples are data, not behaviour).

The sample addons shipped in `addons/` load through the real default root (A2).

They are the smallest worked example of the authoring guide and the fixture the later contract
tests (A8) build on. Variants (cycles, bad manifests) are built in `tmp_path` by the other tests.
"""

from __future__ import annotations

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment


def test_the_sample_bridge_activates_when_both_its_dependencies_are_enabled() -> None:
    loaded = load_addons_from_environment({"AGENT_CORE_ADDONS": "sample_extra,sample_base"})

    assert [(a.manifest.name, a.source) for a in loaded] == [
        ("sample_extra", "explicit"),
        ("sample_base", "explicit"),
        ("sample_bridge", "auto_install"),
    ]


def test_the_sample_bridge_stays_off_with_one_dependency_and_nothing_is_on_by_default() -> None:
    one = load_addons_from_environment({"AGENT_CORE_ADDONS": "sample_base"})

    assert [a.manifest.name for a in one] == ["sample_base"]
    assert load_addons_from_environment({}) == ()
