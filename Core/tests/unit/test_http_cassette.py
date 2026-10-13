"""C31: the cassette transport itself, and above all that it never writes a secret.

Subject: tests/support/http_cassette.py. Recording is exercised against a FAKE upstream transport
(an `httpx.MockTransport`), so nothing here reaches a network or needs a key. The 'secrets' are
obviously fake strings.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.support.http_cassette import (
    RECORD_ENV,
    REDACTED,
    CassetteMiss,
    CassetteTransport,
    redact,
    redact_headers,
    redact_url,
)

FAKE_KEY = "sk-live-0123456789abcdefABCDEF"
OTHER_SECRET = "hunter2-hunter2-hunter2"


def _upstream(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "set-cookie": "session=abc123abc123",
                "x-request-id": "req-1",
            },
            # A provider that echoes a credential back is the case redaction exists for.
            json={"echo": f"your key was {FAKE_KEY}", "reply": "hi", "org": OTHER_SECRET},
        )

    return httpx.MockTransport(handler)


def _post(
    transport: httpx.AsyncBaseTransport, url: str = "https://api.example.test/v1/chat"
) -> httpx.Response:
    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post(
                url,
                headers={"authorization": f"Bearer {FAKE_KEY}", "cookie": "s=1"},
                json={
                    "model": "m",
                    "messages": [{"role": "user", "content": f"hi {OTHER_SECRET}"}],
                },
            )

    return asyncio.run(go())


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECORD_ENV, "1")


def test_recording_is_off_unless_the_environment_asks_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RECORD_ENV, raising=False)
    cassette = tmp_path / "c.json"
    cassette.write_text(json.dumps({"version": 1, "interactions": []}))
    seen: list[httpx.Request] = []

    with pytest.raises(CassetteMiss):
        _post(CassetteTransport(cassette, upstream=_upstream(seen)))

    assert seen == [], "a replay never reaches the upstream"
    assert json.loads(cassette.read_text()) == {"version": 1, "interactions": []}


def test_a_recorded_cassette_carries_no_secret_in_any_place(
    tmp_path: Path, recording: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", OTHER_SECRET)
    cassette = tmp_path / "c.json"

    live = _post(
        CassetteTransport(cassette, upstream=_upstream()),
        "https://api.example.test/v1/chat?api_key=" + FAKE_KEY + "&mode=fast",
    )

    assert live.json()["reply"] == "hi", "the caller still gets the live response"
    written = cassette.read_text()
    assert FAKE_KEY not in written and OTHER_SECRET not in written
    assert "abc123abc123" not in written and "req-1" not in written
    document = json.loads(written)
    (interaction,) = document["interactions"]
    assert interaction["request"]["headers"]["authorization"] == REDACTED
    assert interaction["request"]["headers"]["cookie"] == REDACTED
    assert (
        interaction["request"]["url"]
        == f"https://api.example.test/v1/chat?api_key={REDACTED}&mode=fast"
    )
    assert set(interaction["response"]["headers"]) == {"content-type"}
    assert interaction["response"]["body"]["reply"] == "hi", "non-secret content is kept"


def test_a_recorded_cassette_replays_the_same_request_with_no_upstream(
    tmp_path: Path, recording: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette = tmp_path / "c.json"
    _post(CassetteTransport(cassette, upstream=_upstream()))
    monkeypatch.delenv(RECORD_ENV)

    replayed = _post(CassetteTransport(cassette))

    assert replayed.status_code == 200 and replayed.json()["reply"] == "hi"
    assert FAKE_KEY not in replayed.text, "what is replayed is what was written: redacted"


def test_a_miss_names_the_request_but_never_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", OTHER_SECRET)
    cassette = tmp_path / "c.json"
    cassette.write_text(json.dumps({"version": 1, "interactions": []}))

    with pytest.raises(CassetteMiss) as miss:
        _post(CassetteTransport(cassette))

    text = str(miss.value)
    assert "POST" in text and "api.example.test" in text
    assert FAKE_KEY not in text and OTHER_SECRET not in text


def test_an_unknown_cassette_version_is_refused(tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    cassette.write_text(json.dumps({"version": 99, "interactions": []}))

    with pytest.raises(ValueError, match="unsupported cassette version"):
        CassetteTransport(cassette)


def test_redact_scrubs_nested_values_and_secret_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", OTHER_SECRET)

    scrubbed = redact({"a": [f"x {OTHER_SECRET}", {"b": f"Bearer {FAKE_KEY}"}], "n": 3})

    assert scrubbed == {"a": [f"x {REDACTED}", {"b": REDACTED}], "n": 3}


def test_header_and_url_helpers() -> None:
    kept = redact_headers(
        {"Authorization": "Bearer x", "Content-Type": "a/b", "X-Other": "y"},
        frozenset({"content-type"}),
    )

    assert kept == {"authorization": REDACTED, "content-type": "a/b"}
    assert redact_url("https://h/p?token=abc&q=1") == f"https://h/p?token={REDACTED}&q=1"


def test_every_shipped_cassette_is_free_of_secret_shapes_and_credential_headers() -> None:
    cassettes = sorted(
        (Path(__file__).resolve().parent.parent / "fixtures" / "cassettes").glob("*.json")
    )
    assert cassettes, "at least one cassette ships"

    for path in cassettes:
        text = path.read_text()
        assert redact(json.loads(text)) == json.loads(text), f"{path.name} holds a secret shape"
        for interaction in json.loads(text)["interactions"]:
            headers = interaction["request"]["headers"]
            assert all(
                value == REDACTED for name, value in headers.items() if name != "content-type"
            ), path.name
