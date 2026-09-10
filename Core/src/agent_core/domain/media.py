"""Media vocabulary - references to binaries, never the binaries themselves.

Phase:   F7 - Multimodal input and evidence
Tasks:   docs/TASKS.md#t-f7-01
Status:  TYPES DEFINED / BEHAVIOUR PENDING

WHAT THIS FILE IS
    The domain's view of a file. The domain never holds bytes: it holds a `MediaRef`
    that `MediaStore` can resolve. Two reasons, both learned the hard way elsewhere:
    a turn record carrying payloads becomes enormous, and payloads in domain objects
    leak into logs and exception traces.

THE PRIVACY DECISION THAT LIVES HERE (verified, decide it in F7 - do not drift)
    Pydantic AI, given `ImageUrl` or `AudioUrl`, SENDS THE URL TO THE MODEL PROVIDER,
    which downloads the file from its own side. For incident evidence that may contain
    personal data, that exposes a URL reachable from the provider's infrastructure.

    `MediaPolicy.delivery` is the switch. Default is BYTES (safe). Choosing URL is an
    explicit, written decision, not something that happens by omission.
    See docs/ARCHITECTURE.md#7 and docs/DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

MediaId = NewType("MediaId", str)


class MediaKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class MediaDelivery(StrEnum):
    """How a binary reaches the model provider.

    BYTES       - inline the payload. Private, costs tokens/bandwidth. THE DEFAULT.
    SIGNED_URL  - hand the provider a short-lived URL it fetches itself. Cheaper,
                  and it means the provider touches the file. Opt in deliberately.
    """

    BYTES = "bytes"
    SIGNED_URL = "signed_url"


@dataclass(frozen=True, slots=True)
class MediaRef:
    """A pointer to a stored binary.

    `sha256` is not decoration: it is how `IngestMedia` detects that the same evidence
    was uploaded twice and avoids storing it twice, and how an auditor proves the file
    attached to a case is the file that was uploaded."""

    media_id: MediaId
    kind: MediaKind
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: datetime | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    """Per-profile media rules. Lives on `AgentProfile`.

    `max_bytes` is enforced by `IngestMedia` BEFORE the file reaches storage. Validating
    after storing means a hostile upload has already consumed the disk.

    `allow_evidence_requests` gates the `request_evidence` tool. A fraud agent that reads
    documents does not necessarily get to ASK a human for new ones."""

    accepted_kinds: frozenset[MediaKind] = frozenset()
    delivery: MediaDelivery = MediaDelivery.BYTES
    max_bytes: int = 10 * 1024 * 1024
    allow_evidence_requests: bool = False
    allow_speech_output: bool = False

    def accepts(self, kind: MediaKind) -> bool:
        """PSEUDO-CODE - implement in F7.

        Empty `accepted_kinds` means NOTHING is accepted, not everything. Media is the
        one place where a permissive default is wrong: an agent that silently accepts
        video because a set was left empty is a cost and a privacy incident.

        NOTE the deliberate asymmetry with PolicyRule.subject_roles, where empty means
        "any". Different defaults because the failure modes are different. Test both.
        """
        raise NotImplementedError("F7 - docs/TASKS.md#t-f7-01")
