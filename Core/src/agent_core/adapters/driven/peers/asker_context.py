"""What a peer is told about the asking conversation (C18, D47).

`peers.visibility` was declared (`none | summary | full`) and read by nobody. This is the reader
for the two values that are not `none`:

* `full`    - the conversation's messages, newest kept, cut to the asker's `max_context_chars`.
* `summary` - the Core summariser's text over the same messages (the one the context ladder uses),
              cut to the same cap.

IT READS AS THE USER AUDIENCE. The peer is another agent, not an operator: it is told what the
person and the agent SAID, never the tool calls, arguments, policy rows or reasoning of the
operator's view. That is the narrowest projection that exists, already tenant-scoped in SQL.

THE FAILURE DIRECTION IS "TELL LESS". No summariser wired, or one that fell back to a verbatim
extract because its model was unreachable, means `summary` sends nothing (it never degrades to the
transcript, which is `full`, a different promise); an empty conversation sends nothing; and the
caller degrades to the bare question when this raises.

The text returned here is NOT yet fenced: the workflow wraps it as untrusted before it travels
(`wrap_peer_answer`, the one boundary), since the conversation contains what a person typed.
"""

from __future__ import annotations

from agent_core.adapters.driven.context.engine import Summariser, SummaryRequest
from agent_core.adapters.driven.context.summariser import DEGRADED_MARKER
from agent_core.domain.peers import PeerVisibility
from agent_core.domain.transcript import Audience, EntryKind
from agent_core.domain.turn import SessionRef
from agent_core.ports.transcript_reader import TranscriptReader

_OMITTED = "[earlier messages omitted]\n"
_CUT = " [...cut]"
_ROLES = {EntryKind.USER_MESSAGE: "User", EntryKind.AGENT_MESSAGE: "Assistant"}
_PAGE = 200
# A conversation is read to its end (the NEWEST messages are what survive the cap), but not without
# bound: this many pages of `_PAGE` entries is far past anything a cap of a few thousand characters
# can use.
_MAX_PAGES = 50


class TranscriptAskerContext:
    """`AskerContext` over a `TranscriptReader` and, for `summary`, the Core `Summariser`."""

    def __init__(self, reader: TranscriptReader, summariser: Summariser | None) -> None:
        self._reader = reader
        self._summariser = summariser

    async def render(self, session: SessionRef, mode: PeerVisibility, max_chars: int) -> str:
        if mode is PeerVisibility.NONE:
            raise ValueError("visibility none tells a peer nothing: there is no context to render")
        lines = await self._messages(session)
        if not lines:
            return ""
        if mode is PeerVisibility.FULL:
            return _newest_within(lines, max_chars)
        if self._summariser is None:
            return ""
        summary = await self._summariser(
            SummaryRequest(text="\n".join(lines), model=None, previous_summary=None)
        )
        if summary.lstrip().startswith(DEGRADED_MARKER):
            # The summariser could not reach its model and handed back a verbatim extract. For
            # compaction that is right; to a peer it would be `full` under another name.
            return ""
        return _cut(summary.strip(), max_chars)

    async def _messages(self, session: SessionRef) -> list[str]:
        lines: list[str] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self._reader.page(session, Audience.USER, cursor=cursor, limit=_PAGE)
            for entry in page.entries:
                role = _ROLES.get(entry.kind)
                said = "\n".join(_said(entry.payload.get("content"))).strip()
                if role is not None and said:
                    lines.append(f"{role}: {said}")
            if not page.has_more or page.next_cursor is None:
                break
            cursor = page.next_cursor
        return lines


# The parts of a stored message that are something SAID. A request also carries system prompts and
# tool returns, a response tool calls: none of those is for another agent.
_SPOKEN_PARTS = frozenset({"user-prompt", "text"})


def _said(content: object) -> list[str]:
    """The spoken text of one stored message.

    `messages.content` holds ONE serialised Pydantic AI message (conversation_repository.py, HOW A
    MESSAGE IS ENCODED), so the text is in its `parts`; rows written before that encoding hold
    `{"text": ...}` and are read as well. Anything else is not text and yields nothing.
    """
    if isinstance(content, str):
        return [content]
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    if isinstance(parts, list):
        said: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("part_kind") not in _SPOKEN_PARTS:
                continue
            body = part.get("content")
            if isinstance(body, str):
                said.append(body)
            elif isinstance(body, list):
                said.extend(item for item in body if isinstance(item, str))
        return said
    text = content.get("text")
    return [text] if isinstance(text, str) else []


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_CUT):
        return text[:limit]
    return text[: limit - len(_CUT)] + _CUT


def _newest_within(lines: list[str], limit: int) -> str:
    """The newest whole messages that fit, oldest first, marked when older ones were dropped. A
    single message longer than the cap is cut rather than dropped: the newest thing said is the
    last thing to lose."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + (1 if kept else 0)
        if used + cost > limit:
            break
        kept.append(line)
        used += cost
    if not kept:
        return _cut(lines[-1], limit)
    kept.reverse()
    text = "\n".join(kept)
    if len(kept) < len(lines):
        marked = _OMITTED + text
        if len(marked) <= limit:
            return marked
        # No room for the marker next to every kept message: drop the oldest kept ones for it.
        while kept and len(_OMITTED + "\n".join(kept)) > limit:
            kept.pop(0)
        return _OMITTED + "\n".join(kept) if kept else _cut(lines[-1], limit)
    return text
