"""Port: SpeechPort - how do I convert between audio and text?

Phase:      F7 (transcription, only if needed) / D2 (synthesis)
Tasks:      docs/TASKS.md#t-d2-02
Adapter:    NOT BUILT. A TTS/STT provider credential exists nowhere in this repository,
            so the adapter stays anchored and unwritten; the contract below is what it
            will have to satisfy.
Per-vertical: NO
Status:     FROZEN (t-d2-02) - members are async per D13, and the synthesis grant is a
            required parameter. Bodies stay pseudo-code until an adapter lands.

WHY THIS PORT EXISTS AT ALL, GIVEN PYDANTIC AI HANDLES AUDIO INPUT
    Two paths, and the port is what keeps the choice in configuration:

    - The profile's model accepts native audio -> pass BinaryContent straight through.
      This port is not called.
    - It does not -> transcribe here first and hand the agent text.

    Without the port, that branch ends up as an `if` inside core code, and core code
    starts knowing about model capabilities. With it, the decision is a profile field.

SYNTHESIS IS DAY 2 AND ON DEMAND
    `MediaPolicy.allow_speech_output` gates it. Never on by default: generating audio for
    every response multiplies cost and latency with nobody having asked for it. It is a
    feature for specific channels, not a global setting.

THE GRANT IS A PARAMETER, NOT A RULE ABOUT CALL SITES
    This file used to say "check it at the call site, not here - the port should not need
    to know about profiles". That instruction was the defect, in the class
    `docs/TASKS.md` records for `t-f1-04` and `t-f1-05`: a port whose safety depends on
    every caller remembering an unwritten rule has no safety, and here the failure is
    silent - audio nobody asked for is produced, delivered, and billed, and no test, log
    or exception ever mentions it.

    So `synthesize` takes `MediaPolicy` as a REQUIRED, KEYWORD-ONLY, DEFAULTLESS
    parameter. Three consequences, and each one is asserted in
    `tests/unit/test_ports_speech_port.py`:

    - a call that never thought about speech does not type-check, rather than running;
    - the omission case is `MediaPolicy()`, whose `allow_speech_output` is False, so a
      profile with no `media:` block is silent by construction rather than by diligence;
    - no default exists to be edited, which is the change that would otherwise flip every
      profile in the system at once without any profile file changing.

    The port does not know about profiles by doing this. `MediaPolicy` is a domain type
    (`domain/media.py`), exactly as `MediaRef` is; what it knows is that speech output is
    a grant, and refusing to name the grant is what made it forgettable. Compare
    `MediaStore.signed_url`, which already carries the same duty for the same reason.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.media import MediaPolicy, MediaRef


class SpeechPort(Protocol):
    async def transcribe(self, audio: MediaRef) -> str:
        """PSEUDO-CODE - F7, only when the model lacks native audio input.

        ASYNC (D13): a speech-to-text provider call, measured in seconds.

        Return plain text. If the provider returns segments or timestamps, flatten them:
        the agent wants a message, not a transcript structure.

        DELIBERATELY UNGATED BY `allow_speech_output`, which is about PRODUCING audio.
        Whether audio may enter at all is already decided upstream, once, by
        `MediaPolicy.accepts(MediaKind.AUDIO)` and enforced by `IngestMedia` before a byte
        is stored. Gating it a second time here is how the two gates drift apart and a
        profile that only declined to talk quietly stops being able to listen.

        FAILURE MODE: an empty transcription must RAISE, not return "". An empty string
        reaches the model as an empty user message and produces a confidently wrong reply
        about nothing, with no error anywhere.
        """
        ...

    async def synthesize(
        self, text: str, *, policy: MediaPolicy, voice: str | None = None
    ) -> bytes:
        """PSEUDO-CODE - D2. Caller stores the result via MediaStore and the channel sends it.

        ASYNC (D13): a text-to-speech provider call, and a slower one than the model.

        `policy` is the profile's own `AgentProfile.media`. The implementation MUST refuse
        - raising `PermissionError` - when `policy.allow_speech_output` is False, and MUST
        refuse BEFORE calling the provider. A refusal that arrives after the request was
        paid for is not a refusal, it is the bill plus an exception. `MediaPolicy()`
        carries False, so a profile that never mentioned speech is refused by omission.

        Cap the input length. A model that emits a two-thousand-word answer would
        otherwise produce a fifteen-minute audio file nobody will listen to, at full TTS
        price. Truncate with a spoken note, or refuse.

        Payloads leave as bare `bytes` and are never wrapped in a domain type, for the
        reason `tests/unit/test_ports_media_store.py` states at length: a payload inside
        a domain object travels into the turn record and into every exception trace.
        """
        ...
