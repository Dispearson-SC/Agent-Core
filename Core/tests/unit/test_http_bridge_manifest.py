"""The declarative tool manifest of the bridge: what it accepts and what it refuses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_core.adapters.driven.tools.http_bridge import (
    BridgeManifestError,
    load_bridge_manifest,
    parse_bridge_manifest,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "http_bridge" / "example_tools.yaml"


def _write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _tool(**overrides: Any) -> dict[str, Any]:
    tool = {
        "name": "t",
        "description": "d",
        "method": "GET",
        "route": "/t",
        "kind": "read",
        "arguments": {},
    }
    tool.update(overrides)
    return tool


def test_the_example_manifest_loads_with_its_defaults_resolved() -> None:
    manifest = load_bridge_manifest(EXAMPLE)

    names = [t.name for t in manifest.tools]
    assert names == ["list_books", "get_book", "recall_reviews", "reserve_book", "ping"]
    by_name = {t.name: t for t in manifest.tools}
    assert by_name["get_book"].context == ("session", "agent", "tenant")  # the default
    assert by_name["ping"].context == ()
    assert by_name["reserve_book"].replay == "interrupted"  # write default
    assert by_name["get_book"].replay == "safe"  # read default
    assert manifest.api_prefix == "/api/v1"


@pytest.mark.parametrize(
    ("document", "needle"),
    [
        ({"tools": [_tool(), _tool()]}, "duplicate"),
        ({"tools": [_tool(method="TRACE")]}, "method"),
        ({"tools": [_tool(kind="maybe")]}, "kind"),
        ({"tools": [_tool(route="/t/{id}")]}, "id"),  # placeholder without a path argument
        (
            {"tools": [_tool(arguments={"x": {"type": "string", "in": "path", "required": True}})]},
            "route",
        ),
        ({"tools": [_tool(arguments={"x": {"type": "string", "in": "header"}})]}, "in"),
        ({"tools": [_tool(arguments={"x": {"type": "date", "in": "query"}})]}, "type"),
        ({"tools": [_tool(arguments={"x": {"type": "string", "in": "body"}})]}, "body"),  # GET
        ({"tools": [_tool(kind="write", method="GET")]}, "write"),
        ({"tools": [_tool(idempotency=["a"])]}, "idempotency"),  # reads carry no key
        ({"tools": [_tool(kind="write", method="POST", idempotency=["nope"])]}, "nope"),
        ({"tools": [_tool(context=["planet"])]}, "planet"),
        ({"tools": [_tool(replay="sometimes")]}, "replay"),
        ({"tools": [_tool(name="Bad Name")]}, "name"),
        ({"tools": [_tool(surprise=1)]}, "surprise"),
        ({"tools": []}, "tools"),
        ({"tools": [_tool()], "extra": 1}, "extra"),
    ],
)
def test_a_bad_manifest_is_refused_naming_the_problem(
    tmp_path: Path, document: Any, needle: str
) -> None:
    with pytest.raises(BridgeManifestError, match=needle):
        load_bridge_manifest(_write(tmp_path, document))


def test_a_missing_or_malformed_file_is_a_manifest_error(tmp_path: Path) -> None:
    with pytest.raises(BridgeManifestError, match="missing"):
        load_bridge_manifest(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("tools: [unclosed", encoding="utf-8")
    with pytest.raises(BridgeManifestError):
        load_bridge_manifest(bad)


def test_a_manifest_can_come_from_any_source_as_a_plain_mapping() -> None:
    manifest = parse_bridge_manifest(
        {"tools": [_tool(name="from_elsewhere")]}, source="openapi:/openapi.json"
    )
    assert [t.name for t in manifest.tools] == ["from_elsewhere"]
    assert manifest.tools[0].replay == "safe"

    with pytest.raises(BridgeManifestError, match="openapi:/openapi.json"):
        parse_bridge_manifest({"tools": []}, source="openapi:/openapi.json")


# --- a fixed argument: sent on every call, never shown to the model (G2) ---


def test_a_fixed_argument_is_part_of_the_manifest() -> None:
    manifest = parse_bridge_manifest(
        {"tools": [_tool(arguments={"limit": {"type": "integer", "in": "query", "fixed": 10}})]}
    )

    assert manifest.tools[0].arguments["limit"].fixed == 10


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "integer", "in": "query", "fixed": 10, "required": True},
        {"type": "integer", "in": "query", "fixed": 10, "default": 3},
        {"type": "string", "in": "query", "fixed": "a", "enum": ["b"]},
        {"type": "integer", "in": "query", "fixed": 99, "maximum": 10},
    ],
)
def test_a_fixed_argument_that_contradicts_its_own_spec_is_refused(spec: dict[str, Any]) -> None:
    with pytest.raises(BridgeManifestError, match="fixed"):
        parse_bridge_manifest({"tools": [_tool(arguments={"x": spec})]})
