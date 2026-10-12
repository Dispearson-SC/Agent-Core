"""The structured result of one peer ask (C8, D35).

A peer ask used to resolve to one of several unrelated strings: the peer's answer inside the
untrusted delimiters, or one of five plain sentences of ours (refused, timed out, cancelled, ...).
The model could not tell them apart without reading them. `PeerResult` is the one shape every
outcome now takes, rendered as a single compact tag (the OpenCode `<subagent>` style):

    <peer id="billing" session="peer~case-7~..." status="ok" truncated="true">
    <untrusted-tool-output> ... </untrusted-tool-output>
    </peer>

`text` is exactly what the model reads between the tags. When the PEER wrote it (status `ok` or
`error`), the mailbox port already put it inside the untrusted delimiters and nothing here wraps
again (CLAUDE.md non-negotiable #10: one boundary, applied at one place). When WE wrote it
(`timeout`, `cancelled`, `refused`) it is plain, because nothing outside this process wrote it.

Pure: no I/O, no clock, so the workflow can build it inside the body without breaking replay.
`data` (C9) marks a text that is a JSON object checked against the callee's `answer_schema`: the
JSON itself is the text, inside the same untrusted wrapper, because its string values were still
written by another agent. `usage` is not a field yet: it needs the callee's usage persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from html import escape


class PeerStatus(StrEnum):
    """How an ask ended, from the asker's point of view.

    OK        - the peer answered.
    ERROR     - the peer's turn failed or was closed; the text is our fixed failure sentence.
    TIMEOUT   - no answer inside the asker's `reply_timeout_seconds`; the ask was cancelled.
    CANCELLED - the ask was cancelled before this call (a repeat of one that timed out).
    REFUSED   - the ask was never sent (policy, an unknown peer, a malformed call).
    RUNNING   - a background ask (C19): sent, not answered; its answer arrives later as a follow-up.
    """

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class PeerResult:
    """One resolved peer ask. `peer` is the target agent id as the model wrote it, `session` the
    id of the conversation the peer ran in (None when the ask was never sent)."""

    peer: str
    status: PeerStatus
    text: str
    session: str | None = None
    truncated: bool = False
    # C9. The text is a JSON object that was checked against the callee's `answer_schema`.
    data: bool = False
    # C19. The handle of a background ask (its correlation id): how the owner conversation names it
    # to `peer_status`, `peer_message` and `peer_stop` (C20) and how a follow-up names its ask.
    handle: str | None = None

    def render(self) -> str:
        """The tagged form the model reads. Attributes are escaped: `peer` is model-written."""
        attributes = [f'id="{_attribute(self.peer)}"']
        if self.session is not None:
            attributes.append(f'session="{_attribute(self.session)}"')
        if self.handle is not None:
            attributes.append(f'handle="{_attribute(self.handle)}"')
        attributes.append(f'status="{self.status.value}"')
        if self.truncated:
            attributes.append('truncated="true"')
        if self.data:
            attributes.append('data="json"')
        return f"<peer {' '.join(attributes)}>\n{self.text}\n</peer>"


def _attribute(value: str) -> str:
    return escape(value, quote=True)
