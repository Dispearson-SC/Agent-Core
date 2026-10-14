"""Profile inheritance and named slots: the Odoo `_inherit` and `xpath` analogues.

Phase:   Phase 2 (addon system), A5
Tests:   Core/tests/unit/test_profile_inheritance.py
Implements: no port - startup wiring (D76).

WHAT THIS MODULE DOES
    Turns the profile documents of one startup (Core's files, then the enabled addons' templates)
    into the mappings `AgentProfile.from_mapping` validates. It works on MAPPINGS, before the
    domain sees anything, so `AgentProfile` stays the one place that decides what a profile may
    say and every resolved profile is validated exactly as a hand-written one is.

INHERITANCE  (`inherit: <profile id>`)
    A profile names a parent (a Core file or an addon template; the order they load in does not
    matter) and carries a PATCH: the fields it overrides or extends. Merge rules, applied key by
    key from the parent's resolved mapping:

      scalar          the child's value replaces the parent's.
      list            the parent's items, then the child's that are not already there.
      mapping         merged key by key, recursively (so `peers: {allow: [x]}` extends the
                      parent's `peers`, it does not erase its other keys).
      `replace: [f]`  the named top-level fields are replaced wholesale instead of merged. It may
                      name only a field the patch carries, never `id`, never `approval_rules`:
                      a child may add approval requirements, it may not drop its parent's.
      persona         a scalar: the child's replaces the parent's (and with it the parent's
                      `include:` text); its own `include:` fragments are appended to whichever
                      persona results.
                      The parent's slots stay: see NAMED SLOTS.
      not inherited   `id` (always the child's own), `peers.topology` (declared once, on the hub:
                      `topology_violations`) and `public_alias` (an alias names one profile).

    Inheriting grants nothing the parent plus the patch would not have by being written out: the
    result is validated as a profile, its tools still need a policy `allow` at runtime, and the
    peer topology check runs on the resolved profiles.

NAMED SLOTS  (`slots:` and `{{slot:name}}`)
    A profile declares `slots: {name: [fragment, ...]}` (the fragments are its defaults, possibly
    none) and marks where each goes with a line holding only `{{slot:name}}` in its persona. An
    addon inserts at a slot of ANY profile with `provides.inserts` (`profile`, `slot`,
    `fragment`). Inserts append after the defaults, in activation order, so the result is
    deterministic. A child inherits its parent's slots with whatever was inserted into them and
    may add fragments to them. Declared-but-unmarked, marked-but-undeclared, twice-marked and
    unknown slots are errors; so is an insert naming a profile that does not exist.

    A child that REPLACES the persona must carry the marker of every inherited slot that holds
    content (defaults or inserts): dropping one is an error naming the slot and who put content in
    it, never a silent loss (D77). An inherited slot nothing was put into may be left out.
    An empty slot removes its own marker line and one adjoining blank line, nothing else.
    A marker must be ALONE on its line (blanks around it allowed): one inside a sentence, beside
    other text or twice on a line is an error naming the slot, because a slot's content is whole
    paragraphs and there is no honest place to put them mid-sentence (A6b, D79).
    The machinery applies only to a profile that inherits, declares slots or is the target of an
    insert; in any other a literal `{{slot:x}}` line is plain text, whatever the other profiles do.

BOUNDS
    Chains longer than `MAX_INHERIT_DEPTH` parents, cycles and unknown parents fail at load.

NOTHING USED, NOTHING CHANGES
    A document with no `inherit`, no `slots` and no insert aimed at it is resolved by
    `resolve_includes` alone - the same function `parse_profile` calls.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml

from agent_core.adapters.driven.profiles_fs.fragments import FragmentFile
from agent_core.adapters.driven.profiles_fs.loader import (
    FragmentReader,
    fragment_reader,
    resolve_includes,
)
from agent_core.domain.profile import ProfileValidationError

__all__ = [
    "MAX_INHERIT_DEPTH",
    "ProfileDocument",
    "ProfileInheritanceError",
    "SlotInsert",
    "parse_profile_document",
    "read_profile_document",
    "resolve_documents",
]

MAX_INHERIT_DEPTH: Final = 4

# Set by the child, never inherited from the parent.
_NOT_INHERITED_TOP: Final = ("id", "public_alias")
# A child may never drop what its parent requires.
_NEVER_REPLACED: Final = ("id", "approval_rules")
_SLOT_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_MARKER: Final = re.compile(r"^[ \t]*\{\{slot:([^}\s]*)\}\}[ \t]*$", re.MULTILINE)
# A marker anywhere in the text; the ones `_MARKER` does not also match are inline (A6b, D79).
_ANY_MARKER: Final = re.compile(r"\{\{slot:([^}\s]*)\}\}")


class ProfileInheritanceError(ProfileValidationError):
    """Inheritance, a slot or an insert is wrong. Names the addon when an addon caused it."""

    def __init__(self, message: str, *, addon: str | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.addon = addon
        self.field = field
        self.reason = message


@dataclass(frozen=True)
class ProfileDocument:
    """One profile file, parsed but not yet validated as a profile."""

    name: str  # the file name, for messages
    data: Any  # whatever the YAML held
    read_fragment: FragmentReader
    owner: str | None = None  # the addon that ships it; None for Core's own


@dataclass(frozen=True)
class SlotInsert:
    """One `provides.inserts` entry, with the fragment text already read."""

    addon: str
    profile: str
    slot: str
    text: str


def parse_profile_document(
    text: str,
    *,
    name: str,
    owner: str | None = None,
    fragments_dir: Path | None = None,
    addon_fragments: Mapping[str, FragmentFile] | None = None,
) -> ProfileDocument:
    data: Any = yaml.safe_load(text)
    return ProfileDocument(name, data, fragment_reader(fragments_dir, addon_fragments), owner)


def read_profile_document(
    path: str | Path,
    *,
    owner: str | None = None,
    fragments_dir: Path | None = None,
    addon_fragments: Mapping[str, FragmentFile] | None = None,
) -> ProfileDocument:
    """Read one profile file. Fragments default to the `fragments/` directory next to it."""
    file = Path(path)
    directory = file.parent / "fragments" if fragments_dir is None else fragments_dir
    return parse_profile_document(
        file.read_text(encoding="utf-8"),
        name=file.name,
        owner=owner,
        fragments_dir=directory,
        addon_fragments=addon_fragments,
    )


@dataclass
class _Layer:
    """One document split into what it patches (`fields`) and the machinery around it."""

    document: ProfileDocument
    profile_id: str | None
    parent: str | None
    fields: dict[str, Any]
    includes: list[str]
    slots: dict[str, list[str]]
    replace: tuple[str, ...]
    # The slots this document declares itself (`slots:`), before any insert is aimed at it.
    declared: frozenset[str] = frozenset()
    # slot -> the addons whose inserts were appended to `slots[slot]` (they sit after the defaults).
    inserted: dict[str, list[str]] = field(default_factory=dict)


def _fail(layer: _Layer, message: str) -> ProfileInheritanceError:
    return ProfileInheritanceError(
        f"{layer.document.name}: {message}", addon=layer.document.owner, field="provides.profiles"
    )


def _layer_of(document: ProfileDocument) -> _Layer:
    data = document.data
    if not isinstance(data, dict):
        return _Layer(document, None, None, {}, [], {}, ())
    fields = dict(data)
    profile_id = fields.get("id") if isinstance(fields.get("id"), str) else None
    layer = _Layer(document, profile_id, None, fields, [], {}, ())
    parent = fields.pop("inherit", None)
    if parent is not None:
        if not isinstance(parent, str) or not parent:
            raise _fail(layer, "inherit must be the id of a profile")
        layer.parent = parent
    raw_replace = fields.pop("replace", [])
    if not isinstance(raw_replace, list) or not all(isinstance(f, str) for f in raw_replace):
        raise _fail(layer, "replace must be a list of field names")
    layer.replace = tuple(raw_replace)
    if "include" in fields:
        names = fields.pop("include")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise _fail(layer, "include must be a list of fragment names")
        layer.includes = [document.read_fragment(n) for n in names]
    if "slots" in fields:
        layer.slots = _slots_of(layer, fields.pop("slots"))
        layer.declared = frozenset(layer.slots)
    if layer.parent is None and layer.replace:
        raise _fail(layer, "replace is only meaningful with inherit")
    return layer


def _slots_of(layer: _Layer, raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise _fail(layer, "slots must map a slot name to a list of fragment names")
    slots: dict[str, list[str]] = {}
    for slot, names in raw.items():
        if not isinstance(slot, str) or not _SLOT_NAME.fullmatch(slot):
            raise _fail(layer, f"invalid slot name {slot!r}: lower case words joined by '_'")
        listed = [] if names is None else names
        if not isinstance(listed, list) or not all(isinstance(n, str) for n in listed):
            raise _fail(layer, f"slot {slot!r} must list fragment names")
        slots[slot] = [layer.document.read_fragment(n) for n in listed]
    return slots


def _chain(layer: _Layer, by_id: Mapping[str, _Layer]) -> list[_Layer]:
    """`layer`, its parent, its grandparent... Raises on an unknown parent, a cycle, too deep."""
    chain = [layer]
    seen = [layer.profile_id]
    while chain[-1].parent is not None:
        parent_id = chain[-1].parent
        assert parent_id is not None
        if parent_id in seen:
            path = " -> ".join(str(i) for i in (*seen, parent_id))
            raise _fail(layer, f"inheritance cycle: {path}")
        parent = by_id.get(parent_id)
        if parent is None:
            raise _fail(
                chain[-1],
                f"profile {chain[-1].profile_id!r} inherits {parent_id!r}, which does not exist",
            )
        chain.append(parent)
        seen.append(parent_id)
        if len(chain) - 1 > MAX_INHERIT_DEPTH:
            raise _fail(
                layer,
                f"inheritance depth exceeds {MAX_INHERIT_DEPTH} parents: "
                + " -> ".join(map(str, seen)),
            )
    return chain


def _merge_value(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        return _merge_mapping(base, patch)
    if isinstance(base, list) and isinstance(patch, list):
        return [*base, *(item for item in patch if item not in base)]
    return patch


def _merge_mapping(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        merged[key] = _merge_value(merged[key], value) if key in merged else value
    return merged


@dataclass
class _Resolved:
    fields: dict[str, Any]
    slots: dict[str, list[str]]
    # slot -> who put content in it (profiles with defaults, addons with inserts), for messages.
    sources: dict[str, list[str]] = field(default_factory=dict)


def _inheritable(fields: Mapping[str, Any]) -> dict[str, Any]:
    kept = {key: value for key, value in fields.items() if key not in _NOT_INHERITED_TOP}
    peers = kept.get("peers")
    if isinstance(peers, dict) and "topology" in peers:
        kept["peers"] = {key: value for key, value in peers.items() if key != "topology"}
    return kept


def _sources_of(layer: _Layer) -> dict[str, list[str]]:
    """Who put content into each slot this document holds: its defaults, then each insert."""
    found: dict[str, list[str]] = {}
    for name, texts in layer.slots.items():
        who = [f"addon {addon!r}" for addon in layer.inserted.get(name, [])]
        if len(texts) > len(who):
            who.insert(0, f"profile {layer.profile_id!r}")
        if who:
            found[name] = who
    return found


def _apply(layer: _Layer, parent: _Resolved | None) -> _Resolved:
    for name in layer.replace:
        if name in _NEVER_REPLACED:
            raise _fail(layer, f"replace cannot name {name!r}: a child never drops it")
        if name not in layer.fields:
            raise _fail(layer, f"replace names {name!r}, which the patch does not carry")
    if parent is None:
        fields = dict(layer.fields)
        slots = {name: list(texts) for name, texts in layer.slots.items()}
    else:
        base = _inheritable(parent.fields)
        merged = _merge_mapping(
            {k: v for k, v in base.items() if k not in layer.replace},
            {k: v for k, v in layer.fields.items() if k not in layer.replace},
        )
        fields = {**merged, **{k: layer.fields[k] for k in layer.replace}}
        slots = {name: list(texts) for name, texts in parent.slots.items()}
        for name, texts in layer.slots.items():
            slots[name] = [*slots.get(name, []), *texts]
        if "persona" not in layer.fields and "persona" in base:
            fields["persona"] = base["persona"]
    persona = fields.get("persona")
    if layer.includes and isinstance(persona, str):
        fields["persona"] = "\n\n".join([persona, *layer.includes])
    sources = {name: list(who) for name, who in (parent.sources if parent else {}).items()}
    for name, who in _sources_of(layer).items():
        sources[name] = [*sources.get(name, []), *who]
    return _Resolved(fields, slots, sources)


def _filled(layer: _Layer, resolved: _Resolved) -> dict[str, Any]:
    fields = dict(resolved.fields)
    persona = fields.get("persona")
    inline = _first_inline_marker(persona) if isinstance(persona, str) else None
    if inline is not None:
        raise _fail(
            layer,
            f"the marker {{{{slot:{inline}}}}} must be alone on its own line of the persona: "
            "a slot holds paragraphs of text, so it cannot sit inside a sentence",
        )
    if not isinstance(persona, str) or (not resolved.slots and not _MARKER.search(persona)):
        return fields
    names = [match.group(1) for match in _MARKER.finditer(persona)]
    for name in names:
        if name not in resolved.slots:
            raise _fail(layer, f"the persona marks slot {name!r}, which is not declared in slots:")
        if names.count(name) > 1:
            raise _fail(layer, f"the persona marks slot {name!r} twice")
    slots = dict(resolved.slots)
    for name in resolved.slots:
        if name in names:
            continue
        who = resolved.sources.get(name, [])
        if who:
            # Content would be lost without a word: the persona must say where it goes.
            raise _fail(
                layer,
                f"slot {name!r} has no {{{{slot:{name}}}}} marker in the persona, and it holds "
                f"content from {', '.join(who)}"
                + (f" (inherited from {layer.parent!r})" if layer.parent else "")
                + "; carry the marker line in the persona",
            )
        if name in layer.declared or layer.parent is None:
            raise _fail(layer, f"slot {name!r} has no {{{{slot:{name}}}}} marker in the persona")
        del slots[name]  # inherited, empty and not marked here: nothing to place, nothing lost
    fields["persona"] = _place(persona, slots)
    return fields


def _first_inline_marker(persona: str) -> str | None:
    """The name in the first `{{slot:x}}` that shares its line with anything else, if any."""
    for line in persona.split("\n"):
        found = _ANY_MARKER.findall(line)
        if found and _MARKER.fullmatch(line) is None:
            return str(found[0])
    return None


def _place(persona: str, slots: Mapping[str, list[str]]) -> str:
    """`persona` with each marker line replaced by its slot's fragments.

    An empty slot removes its own marker line and ONE adjoining blank line (the one after it, or
    the one before it at the end), so the gap the marker sat in closes; every other line, blank
    ones included, stays exactly as written.
    """
    lines = persona.split("\n")
    out: list[str] = []
    skip_blank = False
    for index, line in enumerate(lines):
        match = _MARKER.fullmatch(line)
        if match is None:
            if not (skip_blank and line.strip() == ""):
                out.append(line)
            skip_blank = False
            continue
        skip_blank = False
        texts = slots[match.group(1)]
        if texts:
            out.extend("\n\n".join(texts).split("\n"))
        elif index == len(lines) - 1:
            if out and out[-1].strip() == "":
                out.pop()
        else:
            skip_blank = True
    return "\n".join(out)


def _participates(layer: _Layer, aimed_at: set[str]) -> bool:
    """Whether the slot machinery applies to this document: it inherits, declares slots or is
    the target of an insert. Anything else is resolved by `resolve_includes` alone."""
    return bool(layer.parent or layer.declared or layer.profile_id in aimed_at)


def _includes_only(document: ProfileDocument) -> Any:
    return resolve_includes(document.data, document.read_fragment)


def _owned[T](document: ProfileDocument, step: Callable[[ProfileDocument], T]) -> T:
    """`step()`, with a failure of an addon's document naming the addon and the file."""
    try:
        return step(document)
    except ProfileInheritanceError:
        raise
    except ProfileValidationError as error:
        if document.owner is None:
            raise
        raise ProfileInheritanceError(
            f"{document.name}: {error}", addon=document.owner, field="provides.profiles"
        ) from error


ALL_PROFILES = "*"


def _declaring(
    slot: str, layers: Sequence[_Layer], chains: Mapping[int, list[_Layer]]
) -> list[_Layer]:
    """The documents that DECLARE `slot` first: no parent in their chain declared it before.

    An insert aimed at "every profile with this slot" goes into those, once; a child inherits it
    (and the content already put there) through its parent instead of receiving a second copy.
    """
    return [
        layer
        for layer in layers
        if slot in layer.declared and not any(slot in p.declared for p in chains[id(layer)][1:])
    ]


def resolve_documents(
    documents: Sequence[ProfileDocument], inserts: Sequence[SlotInsert] = ()
) -> list[Any]:
    """The mapping `AgentProfile.from_mapping` should validate for each document, in order.

    A document using none of the machinery comes back as `resolve_includes` returns it.
    """
    layers = [_owned(document, _layer_of) for document in documents]
    machinery = bool(inserts) or any(
        layer.parent is not None or "slots" in layer.document.data
        for layer in layers
        if isinstance(layer.document.data, dict)
    )
    if not machinery:
        return [_owned(d, _includes_only) for d in documents]

    by_id: dict[str, _Layer] = {}
    for layer in layers:
        if layer.profile_id is not None:
            by_id.setdefault(layer.profile_id, layer)
    chains = {id(layer): _chain(layer, by_id) for layer in layers}

    for insert in inserts:
        if insert.profile == ALL_PROFILES:
            targets = _declaring(insert.slot, layers, chains)
            if not targets:
                raise ProfileInheritanceError(
                    f"insert into slot {insert.slot!r}, which no profile declares",
                    addon=insert.addon,
                    field="provides.inserts",
                )
        else:
            target = by_id.get(insert.profile)
            if target is None:
                raise ProfileInheritanceError(
                    f"insert into profile {insert.profile!r}, which does not exist",
                    addon=insert.addon,
                    field="provides.inserts",
                )
            if not any(insert.slot in link.slots for link in chains[id(target)]):
                raise ProfileInheritanceError(
                    f"insert into slot {insert.slot!r}, which profile {insert.profile!r} "
                    "does not declare",
                    addon=insert.addon,
                    field="provides.inserts",
                )
            targets = [target]
        for target in targets:
            target.slots.setdefault(insert.slot, []).append(insert.text)
            target.inserted.setdefault(insert.slot, []).append(insert.addon)

    resolved: dict[int, _Resolved] = {}

    def resolve(layer: _Layer) -> _Resolved:
        if id(layer) not in resolved:
            chain = chains[id(layer)]
            parent = resolve(chain[1]) if len(chain) > 1 else None
            resolved[id(layer)] = _apply(layer, parent)
        return resolved[id(layer)]

    aimed_at = {insert.profile for insert in inserts} | {
        layer.profile_id
        for insert in inserts
        if insert.profile == ALL_PROFILES
        for layer in _declaring(insert.slot, layers, chains)
        if layer.profile_id
    }
    out: list[Any] = []
    for layer in layers:
        if not isinstance(layer.document.data, dict):
            out.append(layer.document.data)
            continue
        if not _participates(layer, aimed_at):
            # Outside the machinery, a literal `{{slot:x}}` is text, as it is with no machinery
            # anywhere: the answer must not depend on what OTHER profiles do.
            out.append(_owned(layer.document, _includes_only))
            continue
        out.append(_filled(layer, resolve(layer)))
    return out
