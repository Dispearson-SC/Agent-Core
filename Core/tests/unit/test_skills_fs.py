"""The filesystem SkillRegistry: containment, and the (path, mtime) cache.

Tasks: docs/TASKS.md#t-f6-02

Two properties carry this adapter, and neither of them fails on its own:

CONTAINMENT
    `read` takes a name that came from the MODEL, which may have read a hostile page. The
    defence is not a blocklist of suspicious spellings - it is that a name is resolved
    through the index and the resulting path is proven to sit inside the skills root.
    `test_containment_rejects_a_path_that_only_escapes_after_resolution` pins that
    distinction: it feeds the check a path with no leading `..` that still lands outside,
    and a path that *does* contain `..` and stays inside. A literal `".." in name` check
    passes the first and wrongly rejects the second, so it cannot pass both.

THE CACHE
    `index` runs on every turn. If it re-reads fifty files each time, nothing goes red -
    the only symptom is latency. So the second call is asserted to read nothing, and the
    control test proves the cache still notices a file that actually changed. A cache
    without that control is indistinguishable from one that never refreshes.

The module is imported as a module, matching tests/unit/test_ports_skill_registry.py, so
a name that does not exist yet fails inside the test that needs it rather than at
COLLECTION.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import agent_core.adapters.driven.skills_fs.registry as skills_fs
from agent_core.domain.profile import AgentProfile

DELIVERY = AgentProfile(
    id="delivery_optimizer",
    persona="does not matter here",
    model="claude-sonnet-5",
    skill_namespaces=("delivery",),
)
FRAUD = AgentProfile(
    id="fraud_analyst",
    persona="does not matter here",
    model="claude-sonnet-5",
    skill_namespaces=("fraud",),
)

# Only ever written into a BODY. Finding it on the index side means the optimisation the
# whole port exists for has been undone.
BODY_MARKER = "ZONE_BODY_MARKER"

# Written into a file OUTSIDE the skills root. No assertion may ever find it.
OUTSIDE_MARKER = "CONTENT_OUTSIDE_THE_SKILLS_ROOT"


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "One line, and one line only.",
    namespaces: tuple[str, ...] = ("delivery",),
    requires_env: tuple[str, ...] = (),
    body: str = f"# Heading\n\n{BODY_MARKER}: the body nobody pays for until it is asked for.",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"name: {name}",
        f"description: {description}",
        "namespaces: [" + ", ".join(namespaces) + "]",
    ]
    if requires_env:
        frontmatter += ["requires:", "  env: [" + ", ".join(requires_env) + "]"]
    frontmatter += ["---"]
    path = directory / "SKILL.md"
    path.write_text("\n".join(frontmatter) + "\n\n" + body + "\n", encoding="utf-8")
    return path


def _skills_root(tmp_path: Path) -> Path:
    """A root nested two levels down, so `../../` reaches something worth stealing."""
    outside = tmp_path / ".env"
    outside.write_text(f"API_KEY={OUTSIDE_MARKER}\n", encoding="utf-8")
    root = tmp_path / "Core" / "skills"
    root.mkdir(parents=True)
    return root


def test_index_carries_metadata_only_and_never_a_body(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    _write_skill(root, "delivery-zone-rules", description="Zone surcharges and cutoffs.")

    registry = skills_fs.FilesystemSkillRegistry(root)
    metas = asyncio.run(registry.index(DELIVERY))

    assert [meta.name for meta in metas] == ["delivery-zone-rules"]
    assert metas[0].description == "Zone surcharges and cutoffs."
    for meta in metas:
        assert BODY_MARKER not in (meta.name + meta.description + meta.path)
    assert not Path(metas[0].path).is_absolute(), (
        "an absolute host path in the index is a layout disclosure in every prompt"
    )


def test_index_gives_one_agent_nothing_of_the_others(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    _write_skill(root, "delivery-zone-rules", namespaces=("delivery",))
    _write_skill(root, "chargeback-signals", namespaces=("fraud",))

    registry = skills_fs.FilesystemSkillRegistry(root)

    assert [meta.name for meta in asyncio.run(registry.index(DELIVERY))] == [
        "delivery-zone-rules"
    ]
    assert [meta.name for meta in asyncio.run(registry.index(FRAUD))] == ["chargeback-signals"]


def test_a_skill_whose_requirements_are_unmet_is_not_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offering a skill that cannot run buys prompt tokens and confident wrong answers."""
    root = _skills_root(tmp_path)
    _write_skill(root, "delivery-zone-rules", requires_env=("ZONE_API_URL",))
    monkeypatch.delenv("ZONE_API_URL", raising=False)

    registry = skills_fs.FilesystemSkillRegistry(root)
    assert asyncio.run(registry.index(DELIVERY)) == ()

    monkeypatch.setenv("ZONE_API_URL", "https://example.invalid")
    assert [meta.name for meta in asyncio.run(registry.index(DELIVERY))] == [
        "delivery-zone-rules"
    ]


def test_read_returns_the_body_without_the_frontmatter(tmp_path: Path) -> None:
    root = _skills_root(tmp_path)
    _write_skill(root, "delivery-zone-rules", description="Zone surcharges and cutoffs.")

    body = asyncio.run(skills_fs.FilesystemSkillRegistry(root).read("delivery-zone-rules"))

    assert BODY_MARKER in body
    assert "Zone surcharges and cutoffs." not in body
    assert not body.lstrip().startswith("---")


def test_read_refuses_to_escape_the_skills_root(tmp_path: Path) -> None:
    """The name arrives from the model. `../../.env` costs it nothing to try."""
    root = _skills_root(tmp_path)
    _write_skill(root, "delivery-zone-rules")
    registry = skills_fs.FilesystemSkillRegistry(root)

    with pytest.raises(skills_fs.SkillRegistryError) as caught:
        asyncio.run(registry.read("../../.env"))

    assert OUTSIDE_MARKER not in str(caught.value)
    for hostile in ("..", "../../.env", "/etc/passwd", "delivery-zone-rules/../../../.env"):
        with pytest.raises(skills_fs.SkillRegistryError):
            asyncio.run(registry.read(hostile))


def test_containment_rejects_a_path_that_only_escapes_after_resolution(tmp_path: Path) -> None:
    """The check is resolve-then-contain, not a search for the characters `..`.

    A substring check passes the escaping path below - it has no `..` in it - and rejects
    the legitimate one, which does. Only resolution answers both correctly.
    """
    root = _skills_root(tmp_path)
    (root / "delivery-zone-rules").mkdir()

    escaping = root / "delivery-zone-rules" / os.pardir / os.pardir / ".env"
    assert ".." not in str(escaping.resolve())

    with pytest.raises(skills_fs.SkillPathEscapeError):
        skills_fs.contained_path(root, escaping)

    stays_inside = root / "chargeback-signals" / os.pardir / "delivery-zone-rules" / "SKILL.md"
    assert skills_fs.contained_path(root, stays_inside) == stays_inside.resolve()


def test_a_second_index_with_unchanged_mtimes_reads_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`index` runs every turn. Re-reading fifty files per turn never goes red."""
    root = _skills_root(tmp_path)
    _write_skill(root, "delivery-zone-rules", description="Zone surcharges and cutoffs.")
    _write_skill(root, "refund-windows", description="How long a customer has to ask.")

    registry = skills_fs.FilesystemSkillRegistry(root)
    first = asyncio.run(registry.index(DELIVERY))

    reads: list[Path] = []
    real_read = skills_fs.read_frontmatter

    def counting(path: Path) -> str:
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(skills_fs, "read_frontmatter", counting)
    second = asyncio.run(registry.index(DELIVERY))

    assert reads == [], f"the second index re-read {len(reads)} file(s) that had not changed"
    assert second == first


def test_the_cache_still_notices_a_file_that_actually_changed(tmp_path: Path) -> None:
    """The control. Without it, "reads nothing" is satisfied by never refreshing at all."""
    root = _skills_root(tmp_path)
    path = _write_skill(root, "delivery-zone-rules", description="Before the edit.")

    registry = skills_fs.FilesystemSkillRegistry(root)
    assert asyncio.run(registry.index(DELIVERY))[0].description == "Before the edit."

    before = path.stat()
    _write_skill(root, "delivery-zone-rules", description="After the edit.")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))

    assert asyncio.run(registry.index(DELIVERY))[0].description == "After the edit."
