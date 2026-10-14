"""A5 (D76): profile inheritance (`inherit:`) and insertion at named slots.

Subject: adapters/driven/profiles_fs/inheritance.py (`resolve_documents`), the manifest's
`provides.inserts`, registration of inserts, and `composition.load_profiles` feeding both.

THE RULES UNDER TEST. A profile may `inherit: <id>` and patch it: scalars replace, lists append
(no repeats, parent first), mappings merge key by key, `replace: [field]` replaces a list or
mapping wholesale. A profile may declare `slots:` and mark where each goes in its persona with a
`{{slot:name}}` line; an addon inserts a fragment at a slot of another profile. Cycles, unknown
parents, too-deep chains, unknown profiles and slots fail at load naming what is wrong. The
machinery grants nothing: with none of it used the result is what `parse_profile` returned.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.profiles_fs.inheritance import (
    MAX_INHERIT_DEPTH,
    ProfileDocument,
    ProfileInheritanceError,
    SlotInsert,
    parse_profile_document,
    resolve_documents,
)
from agent_core.adapters.driven.profiles_fs.loader import parse_profile
from agent_core.composition import TOOL_PACKAGES, load_profiles
from agent_core.domain.profile import AgentProfile, ProfileVersionRegistry

BASE = """
id: base
description: Base agent.
persona: You help.
model: m
toolsets: [alpha]
max_iterations: 5
max_cost_usd: "0.10"
skill_namespaces: [core]
"""


def _doc(text: str, name: str = "p.yaml", **kwargs: object) -> ProfileDocument:
    return parse_profile_document(dedent(text), name=name, **kwargs)  # type: ignore[arg-type]


def _resolve(*texts: str, inserts: tuple[SlotInsert, ...] = ()) -> dict[str, AgentProfile]:
    documents = [_doc(text, name=f"{i}.yaml") for i, text in enumerate(texts)]
    resolved = resolve_documents(documents, inserts)
    profiles = [AgentProfile.from_mapping(mapping) for mapping in resolved]
    return {profile.id: profile for profile in profiles}


# --- merge rules ------------------------------------------------------------------------------


def test_scalars_replace_lists_append_without_repeats_and_mappings_merge() -> None:
    child = """
    id: child
    inherit: base
    model: other
    max_iterations: 9
    toolsets: [beta, alpha]
    skill_namespaces: [extra]
    peers: {enabled: true, peers: [{agent_id: x, display_name: X}]}
    """
    parent = BASE + "peers: {max_hops: 3, peers: [{agent_id: y, display_name: Y}]}\n"

    profiles = _resolve(parent, child)

    result = profiles["child"]
    assert result.model == "other" and result.max_iterations == 9
    assert result.persona == "You help."  # not given: inherited
    assert result.toolsets[:2] == ("alpha", "beta") and result.skill_namespaces == ("core", "extra")
    assert result.peers.enabled is True and result.peers.max_hops == 3
    assert [peer.agent_id for peer in result.peers.peers] == ["y", "x"]
    assert profiles["base"].toolsets == ("alpha",)  # the parent is untouched


def test_replace_swaps_a_field_wholesale() -> None:
    child = "id: child\ninherit: base\ntoolsets: [beta]\nreplace: [toolsets]\n"

    assert _resolve(BASE, child)["child"].toolsets == ("beta",)


@pytest.mark.parametrize(
    ("replace", "patch", "message"),
    [
        ("[approval_rules]", "approval_rules: []\n", "approval_rules"),
        ("[id]", "", "id"),
        ("[toolsets]", "", "toolsets"),  # replacing a field the patch does not carry
        ("toolsets", "toolsets: [b]\n", "list"),
    ],
)
def test_replace_is_limited_to_what_the_patch_carries_and_never_drops_approvals(
    replace: str, patch: str, message: str
) -> None:
    child = f"id: child\ninherit: base\nreplace: {replace}\n{patch}"

    with pytest.raises(ProfileInheritanceError, match=message):
        _resolve(BASE, child)


def test_the_topology_and_the_public_alias_are_not_inherited() -> None:
    parent = BASE + "peers: {enabled: true, topology: star}\npublic_alias: Ana\n"
    child = "id: child\ninherit: base\n"

    profiles = _resolve(parent, child)

    assert profiles["base"].peers.topology == "star" and profiles["base"].public_alias == "Ana"
    assert profiles["child"].peers.topology is None and profiles["child"].public_alias != "Ana"
    assert profiles["child"].peers.enabled is True


def test_a_child_persona_replaces_and_its_includes_are_appended(tmp_path: Path) -> None:
    fragments = tmp_path / "f"
    fragments.mkdir()
    (fragments / "tone.md").write_text("Be kind.", encoding="utf-8")
    parent = _doc(BASE, name="a.yaml", fragments_dir=fragments)
    kept = _doc("id: c1\ninherit: base\ninclude: [tone]\n", name="b.yaml", fragments_dir=fragments)
    swapped = _doc(
        "id: c2\ninherit: base\npersona: New.\ninclude: [tone]\n",
        name="c.yaml",
        fragments_dir=fragments,
    )

    resolved = resolve_documents([parent, kept, swapped], ())

    assert resolved[1]["persona"] == "You help.\n\nBe kind."
    assert resolved[2]["persona"] == "New.\n\nBe kind."


# --- failures at load -------------------------------------------------------------------------


def test_an_unknown_parent_is_refused_naming_both() -> None:
    with pytest.raises(ProfileInheritanceError, match=r"child.*ghost|ghost.*child"):
        _resolve(BASE, "id: child\ninherit: ghost\n")


@pytest.mark.parametrize(
    "ids", [["a", "a"], ["a", "b", "a"], ["a", "b", "c", "a"]], ids=["self", "two", "three"]
)
def test_an_inheritance_cycle_is_refused_listing_the_chain(ids: list[str]) -> None:
    texts = [f"id: {ids[i]}\ninherit: {ids[i + 1]}\n" for i in range(len(ids) - 1)]
    documents = [_doc(text, name=f"{i}.yaml") for i, text in enumerate(texts)]

    with pytest.raises(ProfileInheritanceError, match="cycle"):
        resolve_documents(documents, ())


def test_the_chain_depth_is_bounded() -> None:
    def chain(length: int) -> list[str]:
        texts = [BASE.replace("id: base", "id: p0")]
        texts += [f"id: p{i}\ninherit: p{i - 1}\n" for i in range(1, length + 1)]
        return texts

    assert _resolve(*chain(MAX_INHERIT_DEPTH))[f"p{MAX_INHERIT_DEPTH}"].model == "m"
    with pytest.raises(ProfileInheritanceError, match="depth"):
        _resolve(*chain(MAX_INHERIT_DEPTH + 1))


def test_inherit_must_name_a_profile_id() -> None:
    with pytest.raises(ProfileInheritanceError, match="inherit"):
        _resolve(BASE, "id: child\ninherit: [base]\n")


# --- slots ------------------------------------------------------------------------------------

SLOTTED = """
id: slotted
persona: |-
  Intro.

  {{slot:before_tools}}

  Outro.
model: m
slots:
  before_tools: [first]
max_iterations: 5
max_cost_usd: "0.10"
"""


def _fragments(tmp_path: Path, **texts: str) -> Path:
    directory = tmp_path / "frags"
    directory.mkdir(exist_ok=True)
    for name, text in texts.items():
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    return directory


def test_a_slot_is_filled_with_its_fragments_and_inserts_in_load_order(tmp_path: Path) -> None:
    fragments = _fragments(tmp_path, first="FIRST.")
    documents = [_doc(SLOTTED, fragments_dir=fragments)]
    inserts = (
        SlotInsert("alpha", "slotted", "before_tools", "FROM ALPHA."),
        SlotInsert("beta", "slotted", "before_tools", "FROM BETA."),
    )

    persona = resolve_documents(documents, inserts)[0]["persona"]

    assert persona == "Intro.\n\nFIRST.\n\nFROM ALPHA.\n\nFROM BETA.\n\nOutro."
    assert "slots" not in resolve_documents(documents, inserts)[0]


def test_an_empty_slot_leaves_no_trace(tmp_path: Path) -> None:
    text = SLOTTED.replace("[first]", "[]")

    persona = resolve_documents([_doc(text)], ())[0]["persona"]

    assert persona == "Intro.\n\nOutro."


def test_a_child_inherits_the_slots_and_the_inserts_made_into_its_parent(tmp_path: Path) -> None:
    fragments = _fragments(tmp_path, first="FIRST.", extra="CHILD.")
    parent = _doc(SLOTTED, name="a.yaml", fragments_dir=fragments)
    child = _doc(
        "id: child\ninherit: slotted\nslots: {before_tools: [extra]}\n",
        name="b.yaml",
        fragments_dir=fragments,
    )
    inserts = (SlotInsert("alpha", "slotted", "before_tools", "FROM ALPHA."),)

    resolved = resolve_documents([parent, child], inserts)

    assert resolved[0]["persona"] == "Intro.\n\nFIRST.\n\nFROM ALPHA.\n\nOutro."
    assert resolved[1]["persona"] == "Intro.\n\nFIRST.\n\nFROM ALPHA.\n\nCHILD.\n\nOutro."


def test_an_insert_into_a_slot_a_child_only_inherits_is_allowed() -> None:
    child = "id: child\ninherit: slotted\n"
    inserts = (SlotInsert("alpha", "child", "before_tools", "ONLY CHILD."),)
    documents = [_doc(SLOTTED.replace("[first]", "[]"), name="a.yaml"), _doc(child, name="b.yaml")]

    resolved = resolve_documents(documents, inserts)

    assert "ONLY CHILD." in resolved[1]["persona"] and "ONLY CHILD." not in resolved[0]["persona"]


def test_an_unknown_slot_is_an_error_naming_the_addon() -> None:
    inserts = (SlotInsert("alpha", "slotted", "nowhere", "x"),)

    with pytest.raises(ProfileInheritanceError) as caught:
        resolve_documents([_doc(SLOTTED.replace("[first]", "[]"))], inserts)

    assert caught.value.addon == "alpha" and "nowhere" in str(caught.value)


def test_an_unknown_profile_in_an_insert_is_an_error_naming_the_addon() -> None:
    inserts = (SlotInsert("alpha", "ghost", "before_tools", "x"),)

    with pytest.raises(ProfileInheritanceError) as caught:
        resolve_documents([_doc(BASE)], inserts)

    assert caught.value.addon == "alpha" and "ghost" in str(caught.value)


def test_a_slot_without_its_marker_and_a_marker_without_its_slot_are_refused() -> None:
    no_marker = SLOTTED.replace("{{slot:before_tools}}", "nothing")
    no_slot = SLOTTED.replace("before_tools: [first]", "other: []")
    twice = SLOTTED.replace("Outro.", "{{slot:before_tools}}")

    for text, message in ((no_marker, "marker"), (no_slot, "not declared"), (twice, "twice")):
        with pytest.raises(ProfileInheritanceError, match=message):
            resolve_documents([_doc(text.replace("[first]", "[]"))], ())


# --- A5b (D77): slice 21 advisories ---------------------------------------------------------------

PARENT_WITH_SLOT = SLOTTED.replace("[first]", "[]")


def test_a_child_persona_dropping_a_slot_with_inserts_is_refused_naming_who() -> None:
    child = "id: child\ninherit: slotted\npersona: Brand new persona.\n"
    inserts = (SlotInsert("alpha", "slotted", "before_tools", "FROM ALPHA."),)

    with pytest.raises(ProfileInheritanceError) as caught:
        resolve_documents(
            [_doc(PARENT_WITH_SLOT, name="a.yaml"), _doc(child, name="b.yaml")], inserts
        )

    message = str(caught.value)
    assert "before_tools" in message and "alpha" in message and "slotted" in message
    assert "b.yaml" in message


def test_a_child_persona_dropping_a_slot_with_defaults_is_refused(tmp_path: Path) -> None:
    fragments = _fragments(tmp_path, first="FIRST.")
    parent = _doc(SLOTTED, name="a.yaml", fragments_dir=fragments)
    child = _doc("id: child\ninherit: slotted\npersona: New.\n", name="b.yaml")

    with pytest.raises(ProfileInheritanceError, match="before_tools"):
        resolve_documents([parent, child], ())


def test_a_child_persona_may_drop_an_inherited_slot_nothing_was_put_into() -> None:
    child = "id: child\ninherit: slotted\npersona: Brand new persona.\n"

    resolved = resolve_documents(
        [_doc(PARENT_WITH_SLOT, name="a.yaml"), _doc(child, name="b.yaml")], ()
    )

    assert resolved[1]["persona"] == "Brand new persona."


def test_a_child_persona_that_carries_the_marker_keeps_every_insert() -> None:
    child = "id: child\ninherit: slotted\npersona: |-\n  Mine.\n  {{slot:before_tools}}\n"
    inserts = (SlotInsert("alpha", "slotted", "before_tools", "FROM ALPHA."),)

    resolved = resolve_documents(
        [_doc(PARENT_WITH_SLOT, name="a.yaml"), _doc(child, name="b.yaml")], inserts
    )

    assert resolved[1]["persona"] == "Mine.\nFROM ALPHA."


@pytest.mark.parametrize(
    ("persona", "expected"),
    [
        ("A\n\n\nB\n\n{{slot:before_tools}}\n\nC", "A\n\n\nB\n\nC"),
        ("{{slot:before_tools}}\n\nText", "Text"),
        ("Text\n\n{{slot:before_tools}}", "Text"),
        ("Line\n{{slot:before_tools}}\nNext", "Line\nNext"),
    ],
    ids=["keeps-other-gaps", "at-start", "at-end", "tight-lines"],
)
def test_an_empty_slot_removes_only_its_own_marker_line(persona: str, expected: str) -> None:
    text = (
        PARENT_WITH_SLOT.split("persona:")[0]
        + "persona: |-\n"
        + "".join(f"  {line}\n" for line in persona.split("\n"))
        + "model: m\nslots:\n  before_tools: []\nmax_iterations: 5\nmax_cost_usd: '0.10'\n"
    )
    text = text.replace("  \n", "\n")  # blank lines stay blank inside the block scalar

    assert resolve_documents([_doc(text)], ())[0]["persona"] == expected


def test_a_malformed_include_in_an_addon_profile_names_the_addon_and_the_file() -> None:
    document = _doc("id: kid\ninherit: base\ninclude: tone\n", name="kid.yaml", owner="shop")

    with pytest.raises(ProfileInheritanceError) as caught:
        resolve_documents([_doc(BASE, name="a.yaml"), document], ())

    assert caught.value.addon == "shop" and "kid.yaml" in str(caught.value)


def test_a_missing_fragment_in_an_inheriting_addon_profile_names_the_addon_and_the_file() -> None:
    document = _doc("id: kid\ninherit: base\ninclude: [ghost]\n", name="kid.yaml", owner="shop")

    with pytest.raises(ProfileInheritanceError) as caught:
        resolve_documents([_doc(BASE, name="a.yaml"), document], ())

    assert caught.value.addon == "shop" and "kid.yaml" in str(caught.value)


def test_a_literal_marker_outside_the_machinery_is_left_alone_wherever_it_loads() -> None:
    literal = BASE.replace("id: base", "id: literal").replace(
        "persona: You help.", "persona: |-\n  Docs say:\n  {{slot:x}}\n  end."
    )
    other = "id: kid\ninherit: base\n"

    alone = resolve_documents([_doc(literal, name="l.yaml")], ())[0]["persona"]
    beside = resolve_documents(
        [_doc(literal, name="l.yaml"), _doc(BASE, name="a.yaml"), _doc(other, name="k.yaml")], ()
    )[0]["persona"]
    aimed_elsewhere = resolve_documents(
        [_doc(literal, name="l.yaml"), _doc(PARENT_WITH_SLOT, name="s.yaml")],
        (SlotInsert("alpha", "slotted", "before_tools", "X."),),
    )[0]["persona"]

    assert alone == beside == aimed_elsewhere == "Docs say:\n{{slot:x}}\nend."


def test_a_marker_in_a_profile_that_uses_the_machinery_must_be_declared() -> None:
    child = BASE.replace("id: base", "id: kid") + "inherit: base\n"
    child = child.replace("persona: You help.", "persona: |-\n  Docs:\n  {{slot:x}}")

    with pytest.raises(ProfileInheritanceError, match="slot 'x'"):
        resolve_documents([_doc(BASE, name="a.yaml"), _doc(child, name="k.yaml")], ())


@pytest.mark.parametrize(
    "persona",
    [
        "Intro {{slot:before_tools}} outro.",
        "{{slot:before_tools}} and more",
        "Text first {{slot:before_tools}}",
        "{{slot:before_tools}}{{slot:before_tools}}",
        "Intro.\n\nSee: {{slot:before_tools}}\n\nOutro.",
    ],
    ids=["middle", "leading", "trailing", "twice-on-a-line", "after-a-label"],
)
def test_a_marker_that_is_not_alone_on_its_line_is_refused_naming_the_slot(persona: str) -> None:
    text = SLOTTED.replace("Intro.\n\n  {{slot:before_tools}}\n\n  Outro.", "X").replace(
        "persona: |-\n  X", "persona: |-\n" + "".join(f"  {line}\n" for line in persona.split("\n"))
    )

    with pytest.raises(ProfileInheritanceError, match="alone on its own line") as caught:
        resolve_documents([_doc(text.replace("[first]", "[]"), name="s.yaml")], ())

    assert "before_tools" in str(caught.value) and "s.yaml" in str(caught.value)


def test_an_inline_marker_is_refused_even_when_the_slot_is_not_declared() -> None:
    child = BASE.replace("id: base", "id: kid") + "inherit: base\n"
    child = child.replace("persona: You help.", "persona: Docs say {{slot:x}} here.")

    with pytest.raises(ProfileInheritanceError, match="alone on its own line"):
        resolve_documents([_doc(BASE, name="a.yaml"), _doc(child, name="k.yaml")], ())


def test_an_inline_marker_in_a_profile_outside_the_machinery_stays_text() -> None:
    literal = BASE.replace("persona: You help.", "persona: Docs say {{slot:x}} here.")

    assert resolve_documents([_doc(literal)], ())[0]["persona"] == "Docs say {{slot:x}} here."


# --- nothing used: nothing changes ------------------------------------------------------------


def test_without_inherit_or_slots_the_result_is_what_parse_profile_returned(tmp_path: Path) -> None:
    fragments = _fragments(tmp_path, tone="Be kind.")
    text = BASE + "include: [tone]\npublic_alias: Ana\n"

    resolved = resolve_documents([_doc(text, fragments_dir=fragments)], ())

    assert AgentProfile.from_mapping(resolved[0]) == parse_profile(text, fragments_dir=fragments)


# --- end to end through addons and load_profiles ----------------------------------------------

TOOLSET = """
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset


def tool_{n}(text: str) -> str:
    "Return the text."
    return text


def build_toolset():
    return FunctionToolset([Tool(tool_{n})])
"""


def _addon(root: Path, name: str, manifest: str, **files: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\n{dedent(manifest)}", encoding="utf-8"
    )
    for key, text in files.items():
        target = directory / key.replace("__", "/").replace("_dot_", ".")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text), encoding="utf-8")


def _load(root: Path, enabled: list[str], core: Path) -> dict[str, AgentProfile]:
    addons = load_addons(enabled=enabled, addons_root=root, core_version="0.0.0")
    contributions = register_addons(
        addons,
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=("http", "cli"),
    )
    return load_profiles(core, ProfileVersionRegistry(), contributions)


def _core(tmp_path: Path, **files: str) -> Path:
    core = tmp_path / "core_profiles"
    core.mkdir()
    for key, text in files.items():
        (core / f"{key}.yaml").write_text(dedent(text), encoding="utf-8")
    return core


def test_a_core_profile_inherits_a_profile_template_an_addon_provides(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        "provides:\n  profiles: profiles/\n",
        profiles__template_dot_yaml=BASE.replace("id: base", "id: shop_template"),
    )
    core = _core(tmp_path, mine="id: mine\ninherit: shop_template\ntoolsets: [beta]\n")

    profiles = _load(tmp_path, ["shop"], core)

    assert profiles["mine"].persona == "You help." and profiles["mine"].toolsets == (
        "alpha",
        "beta",
    )
    assert profiles["mine"].version >= 1


def test_an_addon_inserts_a_fragment_at_a_slot_of_a_core_profile(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        """
        provides:
          fragments: fragments/
          inserts:
            - {profile: slotted, slot: before_tools, fragment: shop_note}
        """,
        fragments__shop_note_dot_md="Mind the cart.\n",
    )
    core = _core(tmp_path, slotted=SLOTTED.replace("[first]", "[]"))

    profiles = _load(tmp_path, ["shop"], core)

    assert profiles["slotted"].persona == "Intro.\n\nMind the cart.\n\nOutro."


def test_two_addons_insert_in_activation_order(tmp_path: Path) -> None:
    for name in ("zeta", "alpha"):  # activation order is the dependency order, then the list
        _addon(
            tmp_path,
            name,
            f"""
            provides:
              fragments: fragments/
              inserts:
                - {{profile: slotted, slot: before_tools, fragment: {name}_note}}
            """,
            **{f"fragments__{name}_note_dot_md": f"{name}.\n"},
        )
    core = _core(tmp_path, slotted=SLOTTED.replace("[first]", "[]"))

    first = _load(tmp_path, ["zeta", "alpha"], core)["slotted"].persona
    second = _load(tmp_path, ["alpha", "zeta"], core)["slotted"].persona

    assert first == "Intro.\n\nzeta.\n\nalpha.\n\nOutro."
    assert second == "Intro.\n\nalpha.\n\nzeta.\n\nOutro."


def test_an_insert_into_an_unknown_slot_stops_startup_naming_the_addon(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        """
        provides:
          fragments: fragments/
          inserts:
            - {profile: slotted, slot: nowhere, fragment: shop_note}
        """,
        fragments__shop_note_dot_md="x\n",
    )
    core = _core(tmp_path, slotted=SLOTTED.replace("[first]", "[]"))

    with pytest.raises(AddonRegistrationError) as caught:
        _load(tmp_path, ["shop"], core)

    assert caught.value.addon == "shop" and "nowhere" in str(caught.value)


def test_an_insert_may_only_name_a_fragment_the_addon_provides(tmp_path: Path) -> None:
    _addon(
        tmp_path,
        "shop",
        """
        provides:
          inserts:
            - {profile: slotted, slot: before_tools, fragment: foreign}
        """,
    )

    with pytest.raises(AddonRegistrationError) as caught:
        _load(tmp_path, ["shop"], _core(tmp_path))

    assert caught.value.addon == "shop" and caught.value.field == "provides.inserts"


def test_an_addon_profile_that_inherits_a_missing_parent_is_refused_naming_the_addon(
    tmp_path: Path,
) -> None:
    _addon(
        tmp_path,
        "shop",
        "provides:\n  profiles: profiles/\n",
        profiles__kid_dot_yaml="id: kid\ninherit: ghost\n",
    )

    with pytest.raises(AddonRegistrationError) as caught:
        _load(tmp_path, ["shop"], _core(tmp_path))

    assert caught.value.addon == "shop" and "ghost" in str(caught.value)


def test_a_core_profile_inheriting_a_missing_parent_fails_load_unwrapped(tmp_path: Path) -> None:
    core = _core(tmp_path, kid="id: kid\ninherit: ghost\n")

    with pytest.raises(ProfileInheritanceError, match="ghost"):
        load_profiles(core, ProfileVersionRegistry())


def test_the_inherited_profile_is_versioned_from_its_resolved_content(tmp_path: Path) -> None:
    core = _core(tmp_path, base=BASE, kid="id: kid\ninherit: base\n")
    registry = ProfileVersionRegistry()
    first = load_profiles(core, registry)["kid"].version

    (core / "base.yaml").write_text(BASE.replace("You help.", "You help more."), encoding="utf-8")
    second = load_profiles(core, registry)["kid"]

    assert second.version == first + 1 and second.persona == "You help more."


def test_preflight_reports_a_broken_inheritance_as_a_profile_finding(tmp_path: Path) -> None:
    """`preflight` loads profiles through `load_profiles`, so inheritance is checked there too.

    (`:reload` in the console is bound to the same `load_profiles`; main.py passes it.)
    """
    import asyncio

    from agent_core.adapters.driving.cli.preflight import preflight
    from agent_core.composition import Settings

    core = _core(tmp_path, kid="id: kid\ninherit: ghost\n")
    settings = Settings(profiles_dir=core, app_conninfo="postgresql://nobody@127.0.0.1:1/none")

    report = asyncio.run(preflight(settings))

    findings = [c for c in report.checks if c.severity == "fail" and "profiles" in c.name]
    assert findings and "ghost" in findings[0].detail


# --- an insert aimed at every profile that declares the slot (`profile: "*"`, G4, D89) ---------

OTHER_SLOTTED = SLOTTED.replace("id: slotted", "id: other")


def test_a_wildcard_insert_reaches_every_profile_that_declares_the_slot() -> None:
    plain = "id: plain\npersona: No slot here.\nmodel: m\nmax_iterations: 5\nmax_cost_usd: '0.10'\n"
    documents = [
        _doc(SLOTTED.replace("[first]", "[]"), name="a.yaml"),
        _doc(OTHER_SLOTTED.replace("[first]", "[]"), name="b.yaml"),
        _doc(plain, name="c.yaml"),
    ]
    inserts = (SlotInsert("alpha", "*", "before_tools", "FROM ALPHA."),)

    resolved = resolve_documents(documents, inserts)

    assert resolved[0]["persona"] == "Intro.\n\nFROM ALPHA.\n\nOutro."
    assert resolved[1]["persona"] == "Intro.\n\nFROM ALPHA.\n\nOutro."
    assert resolved[2]["persona"] == "No slot here."


def test_a_wildcard_insert_reaches_a_child_once_through_its_parent() -> None:
    redeclaring = "id: child\ninherit: slotted\nslots: {before_tools: []}\n"
    documents = [
        _doc(SLOTTED.replace("[first]", "[]"), name="a.yaml"),
        _doc(redeclaring, name="b.yaml"),
    ]
    inserts = (SlotInsert("alpha", "*", "before_tools", "ONCE."),)

    resolved = resolve_documents(documents, inserts)

    assert resolved[0]["persona"].count("ONCE.") == 1
    assert resolved[1]["persona"].count("ONCE.") == 1


def test_a_wildcard_insert_into_a_slot_nobody_declares_is_an_error_naming_the_addon() -> None:
    inserts = (SlotInsert("alpha", "*", "nowhere", "x"),)

    with pytest.raises(ProfileInheritanceError) as caught:
        resolve_documents([_doc(SLOTTED.replace("[first]", "[]"))], inserts)

    assert caught.value.addon == "alpha" and "nowhere" in str(caught.value)
    assert "no profile" in str(caught.value)
