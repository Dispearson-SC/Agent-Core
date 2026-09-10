"""Use case: IngestMedia - accept a file from a human and turn it into a MediaRef.

Phase:   F7
Tasks:   docs/TASKS.md#t-f7-03
Status:  PSEUDO-CODE ONLY

WHERE THIS SITS IN THE EVIDENCE FLOW
    1. Agent calls `request_evidence(kind="photo", reason=...)` - an externally-executed
       deferred tool, so the run ENDS and returns DeferredToolRequests.
    2. HumanGateway publishes the ask to the channel.
    3. DBOS.recv() waits durably. No hung process; the service may restart.
    4. >>> THIS USE CASE <<< validates and stores the upload, returns a MediaRef.
    5. ResumeTurn resumes; the binary arrives as the deferred tool's result.

    To the agent it was simply a tool that returned an image.

THIS IS THE UNTRUSTED-INPUT BOUNDARY
    A file arriving from a channel is the least trusted input the system has. Everything
    below is validation, and every check must happen BEFORE the bytes reach storage.
"""

from __future__ import annotations

from agent_core.domain.media import MediaKind, MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId
from agent_core.ports.audit_sink import AuditSink
from agent_core.ports.media_store import MediaStore


class IngestMedia:
    def __init__(self, *, media: MediaStore, audit: AuditSink) -> None:
        self._media = media
        self._audit = audit

    def execute(
        self,
        turn_id: TurnId,
        profile: AgentProfile,
        data: bytes,
        *,
        declared_mime: str,
        filename: str | None = None,
    ) -> MediaRef:
        """PSEUDO-CODE - implement in F7.

        STEP 1 - SIZE, BEFORE ANYTHING ELSE
            if len(data) > profile.media.max_bytes: reject

            First check, always. Validating after storing means a hostile upload has
            already consumed the disk.

        STEP 2 - SNIFF, DO NOT TRUST
            kind = sniff magic bytes -> MediaKind
            Reject when the sniffed type contradicts `declared_mime`. The client's declared
            type is a hint from an untrusted source, nothing more.

        STEP 3 - CHECK THE PROFILE ACCEPTS IT
            if not profile.media.accepts(kind): reject

            Remember the deliberate asymmetry: an EMPTY `accepted_kinds` means NOTHING is
            accepted. Media is the one place a permissive default is wrong.

        STEP 4 - STORE
            ref = self._media.put(data, kind=kind, mime_type=..., filename=filename)
            Deduplicates by sha256, so the same evidence uploaded twice is stored once.

        STEP 5 - AUDIT
            self._audit.record_media(turn_id, ref, direction="inbound")
            The hash goes in the record, never the bytes. That hash is how an auditor later
            proves the file attached to a case is the file that was uploaded.

        STEP 6 - RETURN THE REF

        WHAT THIS USE CASE MUST NEVER DO
            - Return a ref for a file it did not fully validate.
            - Let `filename` reach a filesystem path. It is attacker-controlled;
              `MediaStore` names files by id, and the original name is metadata only.
            - Decide BYTES vs SIGNED_URL. That is `MediaPolicy.delivery`, read at the
              moment the file is handed to the model, not at ingestion.
        """
        raise NotImplementedError("F7 - docs/TASKS.md#t-f7-03")
