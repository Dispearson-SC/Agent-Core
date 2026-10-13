"""Recorded provider cassettes: record real LLM HTTP once, replay it from JSON (C31, D66).

An `httpx` transport for tests (the idea of OpenCode's `http-recorder`). Handed to the HTTP client
a model is built with, it makes an adapter test run the real request-building and response-parsing
code against a recorded exchange, with no network and no cost.

REPLAY (the default). Each request is matched to a recorded interaction by method, URL and JSON
body. A request nothing matches raises `CassetteMiss`: it never falls through to a network call.
An interaction can be matched more than once (a retry, a second identical turn).

RECORD (opt-in). Only when `AGENT_CORE_RECORD_CASSETTES=1` is set in the environment. The request
goes to the real upstream and the exchange is written to the cassette file, REDACTED. Nothing is
recorded unless the variable is set, so an accident cannot send a test to a provider.

REDACTION. What is written never carries a secret, whichever way it got into the exchange:
  * request headers are written only from an allowlist (`content-type`, `accept`); the credential
    headers (`authorization`, `x-api-key`, ...) are written as `[REDACTED]`;
  * response headers are written only from an allowlist (`content-type`); cookies, request ids and
    the rest are dropped;
  * query parameters named like a credential (`key`, `api_key`, ...) are masked in the URL;
  * every string in a body or URL is scrubbed of (a) the credential the request itself carried,
    (b) the value of every environment variable whose name looks like a secret (`*KEY*`, `*TOKEN*`,
    `*SECRET*`, `*PASSWORD*`), and (c) anything shaped like an API key (`sk-...`, `Bearer ...`).
Redaction runs on the way OUT to the file, so a recorded cassette is safe to commit as written.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

__all__ = [
    "RECORD_ENV",
    "REDACTED",
    "CassetteMiss",
    "CassetteTransport",
    "redact",
    "redact_headers",
    "redact_url",
]

RECORD_ENV = "AGENT_CORE_RECORD_CASSETTES"
REDACTED = "[REDACTED]"
_VERSION = 1

_KEPT_REQUEST_HEADERS = frozenset({"content-type", "accept"})
_KEPT_RESPONSE_HEADERS = frozenset({"content-type"})
_SECRET_QUERY = re.compile(r"(key|token|secret|password|signature|auth)", re.IGNORECASE)
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
_SECRET_SHAPES = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")
_MIN_SECRET_LENGTH = 8


class CassetteMiss(AssertionError):
    """A request the cassette holds no recorded interaction for. Never includes a header or a
    credential: only the method, the URL (redacted) and the body."""


def _secret_values(extra: Iterable[str] = ()) -> list[str]:
    """Values to scrub from anything written: the env's secrets plus `extra`, longest first (so a
    secret that contains another is removed whole)."""
    found = {
        v for k, v in os.environ.items() if _SECRET_ENV.search(k) and len(v) >= _MIN_SECRET_LENGTH
    }
    found.update(v for v in extra if len(v) >= _MIN_SECRET_LENGTH)
    return sorted(found, key=len, reverse=True)


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """`value` (JSON-shaped) with every secret string replaced by `[REDACTED]`."""
    known = _secret_values(secrets)

    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            for secret in known:
                item = item.replace(secret, REDACTED)
            return _SECRET_SHAPES.sub(REDACTED, item)
        if isinstance(item, Mapping):
            return {scrub(str(k)): scrub(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [scrub(v) for v in item]
        return item

    return scrub(value)


def redact_headers(headers: Mapping[str, str], keep: frozenset[str]) -> dict[str, str]:
    """Allowlisted headers as they are; every other header present by name with `[REDACTED]` when
    it is a credential, and dropped otherwise."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in keep:
            out[lowered] = value
        elif lowered in {"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie"}:
            out[lowered] = REDACTED
    return out


def redact_url(url: str, secrets: Iterable[str] = ()) -> str:
    """`url` with credential-looking query parameters masked and secrets scrubbed."""
    parts = urlsplit(url)
    query = [
        (k, REDACTED if _SECRET_QUERY.search(k) else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    # `urlencode` would percent-encode the marker; build the query by hand.
    rendered = "&".join(f"{k}={v}" if v == REDACTED else urlencode([(k, v)]) for k, v in query)
    return str(redact(urlunsplit(parts._replace(query=rendered)), secrets))


def _body_of(content: bytes, content_type: str) -> Any:
    """JSON when it parses (so a cassette diff is readable), else the text."""
    text = content.decode("utf-8", errors="replace")
    if not text:
        return None
    if "json" in content_type or text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _bytes_of(body: Any) -> bytes:
    if body is None:
        return b""
    return body.encode() if isinstance(body, str) else json.dumps(body).encode()


def _credentials_of(request: httpx.Request) -> list[str]:
    """The secret values this very request carried, so they are scrubbed even when no
    environment variable names them."""
    found: list[str] = []
    for name in ("authorization", "proxy-authorization", "x-api-key", "api-key"):
        value = request.headers.get(name)
        if value:
            found.append(value)
            found.append(value.split(" ", 1)[-1])
    for key, value in parse_qsl(request.url.query.decode()):
        if _SECRET_QUERY.search(key):
            found.append(value)
    return found


class CassetteTransport(httpx.AsyncBaseTransport):
    """Replays (default) or records (`AGENT_CORE_RECORD_CASSETTES=1`) a cassette file.

    `upstream` is the transport a recording goes through; a test can pass a fake one. In replay it
    is never used."""

    def __init__(self, path: Path, *, upstream: httpx.AsyncBaseTransport | None = None) -> None:
        self._path = Path(path)
        self._recording = os.environ.get(RECORD_ENV) == "1"
        self._upstream = upstream
        self._interactions: list[dict[str, Any]] = [] if self._recording else self._load(self._path)

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        data = json.loads(path.read_text())
        if data.get("version") != _VERSION:
            raise ValueError(f"{path.name}: unsupported cassette version {data.get('version')!r}")
        return list(data["interactions"])

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._recording:
            return await self._record(request)
        return self._replay(request)

    # --- replay -------------------------------------------------------------------------------

    def _replay(self, request: httpx.Request) -> httpx.Response:
        url = redact_url(str(request.url))
        body = _body_of(request.content, request.headers.get("content-type", ""))
        for interaction in self._interactions:
            recorded = interaction["request"]
            if (
                recorded["method"] == request.method
                and recorded["url"] == url
                and recorded["body"] == redact(body)
            ):
                reply = interaction["response"]
                return httpx.Response(
                    reply["status"],
                    headers=reply.get("headers", {}),
                    content=_bytes_of(reply.get("body")),
                )
        raise CassetteMiss(
            f"no recorded interaction in {self._path.name} for {request.method} {url} with body "
            f"{json.dumps(redact(body))[:500]}. Re-record it with {RECORD_ENV}=1."
        )

    # --- record -------------------------------------------------------------------------------

    async def _record(self, request: httpx.Request) -> httpx.Response:
        upstream = self._upstream or httpx.AsyncHTTPTransport()
        response = await upstream.handle_async_request(request)
        content = await response.aread()
        secrets = _credentials_of(request)
        sent = _body_of(request.content, request.headers.get("content-type", ""))
        got = _body_of(content, response.headers.get("content-type", ""))
        self._interactions.append(
            {
                "request": {
                    "method": request.method,
                    "url": redact_url(str(request.url), secrets),
                    "headers": redact_headers(dict(request.headers), _KEPT_REQUEST_HEADERS),
                    "body": redact(sent, secrets),
                },
                "response": {
                    "status": response.status_code,
                    "headers": redact_headers(dict(response.headers), _KEPT_RESPONSE_HEADERS),
                    "body": redact(got, secrets),
                },
            }
        )
        self._write()
        return httpx.Response(
            response.status_code,
            headers=[
                (k, v) for k, v in response.headers.items() if k.lower() != "content-encoding"
            ],
            content=content,
            request=request,
        )

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": _VERSION, "interactions": self._interactions}
        self._path.write_text(json.dumps(document, indent=2) + "\n")
