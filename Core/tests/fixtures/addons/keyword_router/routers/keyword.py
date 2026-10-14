"""A keyword router: `route` to the configured target when the keyword appears, else `pass`.

Plain Python on purpose: an addon never imports Core's domain, application or ports. It reads a
plain mapping and answers one.
"""

from __future__ import annotations

from typing import Any


def route(request: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    if settings["keyword"] in request["text"].lower():
        return {
            "decision": "route",
            "profile_id": settings["target"],
            "label": settings["keyword"],
            "confidence": 1.0,
            "calibrated": False,
            "model": "keyword",
        }
    return {"decision": "pass", "label": "no_match", "model": "keyword"}
