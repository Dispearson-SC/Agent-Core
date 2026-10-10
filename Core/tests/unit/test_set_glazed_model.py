"""`Core/scripts/set_glazed_model.py` rewrites the `model:` line of the glazed profiles."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "set_glazed_model", ROOT / "scripts" / "set_glazed_model.py"
)
assert _spec and _spec.loader
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ROLES = (
    "orchestrator",
    "present",
    "past",
    "supply",
    "strategist",
    "sentinel",
    "auditor",
    "liaison",
)


def _make(tmp_path: Path) -> Path:
    for role in ROLES:
        (tmp_path / f"glazed_{role}.yaml").write_text(
            f"id: glazed_{role}\npersona: |\n  model: not this line\n"
            "model: old/model\nmax_iterations: 5\n"
        )
    (tmp_path / "support_triage.yaml").write_text("id: support_triage\nmodel: old/model\n")
    return tmp_path


def test_rewrites_only_the_model_line_of_the_eight_glazed_profiles(tmp_path: Path) -> None:
    profiles = _make(tmp_path)

    changed = script.set_model(profiles, "minimax/MiniMax-M3")

    assert len(changed) == 8
    for role in ROLES:
        text = (profiles / f"glazed_{role}.yaml").read_text()
        assert "\nmodel: minimax/MiniMax-M3\n" in text
        assert "  model: not this line" in text
        assert "max_iterations: 5" in text
    assert "old/model" in (profiles / "support_triage.yaml").read_text()


def test_is_idempotent(tmp_path: Path) -> None:
    profiles = _make(tmp_path)
    script.set_model(profiles, "minimax/MiniMax-M3")

    assert script.set_model(profiles, "minimax/MiniMax-M3") == []


def test_refuses_a_model_without_provider_prefix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        script.set_model(_make(tmp_path), "MiniMax-M3")


def test_refuses_when_a_profile_has_no_model_line(tmp_path: Path) -> None:
    profiles = _make(tmp_path)
    (profiles / "glazed_past.yaml").write_text("id: glazed_past\n")

    with pytest.raises(ValueError, match="glazed_past"):
        script.set_model(profiles, "minimax/MiniMax-M3")
