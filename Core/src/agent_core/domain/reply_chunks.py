"""Cutting a streaming reply into the pieces the event feed carries (C26, D58). Pure.

A model streams its reply in whatever sizes it likes (a token, a word, a line). A voice front-end
wants the FIRST SENTENCE as soon as it is complete, and a feed wants a handful of rows per reply,
not one per token. `ReplyChunker` turns the first into the second:

  * a chunk ends where a sentence ends: `.`, `!`, `?` or `…` followed by whitespace (so `3.50` and
    `v2.1.3` stay whole), a newline, or a CJK full stop (`。！？`, which needs no space after it);
  * the whitespace that follows the end belongs to the chunk, so the chunks put back together are
    EXACTLY the text, however it arrived;
  * a stretch with no end is cut at the last space before `MAX_CHUNK_CHARS`, and a single token
    longer than that is cut hard, so nothing waits for ever and a word is split only when it is
    bigger than a chunk;
  * a full stop at the very end of what has arrived is NOT yet an end (it may be the start of a
    decimal); it is released when the next character shows it was one, or by `flush`.
"""

from __future__ import annotations

from typing import Final

__all__ = ["MAX_CHUNK_CHARS", "ReplyChunker"]

MAX_CHUNK_CHARS: Final[int] = 240
"""The longest chunk, well under `MAX_DELTA_CHARS`. A longer sentence is cut at a space."""

_NEEDS_SPACE: Final[str] = ".!?…"
_STANDALONE: Final[str] = "。！？\n"


class ReplyChunker:
    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = ""

    def push(self, text: str) -> list[str]:
        """Add what the model just sent; return the chunks that are now complete (maybe none)."""
        self._buffer += text
        return self._drain()

    def flush(self) -> list[str]:
        """The reply is over: return everything still held, cut to the limit."""
        out = self._drain()
        if self._buffer:
            out.append(self._buffer)
            self._buffer = ""
        return out

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            end = self._sentence_end()
            if end is not None and end <= MAX_CHUNK_CHARS:
                out.append(self._take(end))
            elif len(self._buffer) > MAX_CHUNK_CHARS:
                out.append(self._take(self._cut_point()))
            else:
                return out

    def _take(self, count: int) -> str:
        chunk, self._buffer = self._buffer[:count], self._buffer[count:]
        return chunk

    def _sentence_end(self) -> int | None:
        """Where the first complete sentence of the buffer ends (past its trailing whitespace)."""
        text = self._buffer
        for i, char in enumerate(text):
            if char in _STANDALONE:
                return self._past_whitespace(i + 1)
            if char in _NEEDS_SPACE and i + 1 < len(text) and text[i + 1].isspace():
                return self._past_whitespace(i + 1)
        return None

    def _past_whitespace(self, index: int) -> int:
        while index < len(self._buffer) and self._buffer[index].isspace():
            index += 1
        return index

    def _cut_point(self) -> int:
        for index in range(MAX_CHUNK_CHARS, 0, -1):
            if self._buffer[index - 1].isspace():
                return index
        return MAX_CHUNK_CHARS
