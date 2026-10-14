"""The sample wrapper: calls the original `sample_echo` and upper-cases what it returns."""

from __future__ import annotations


async def wrap(original, arguments, ctx):  # type: ignore[no-untyped-def]
    return str(await original()).upper()
