"""Port: SpeechPort - how do I convert between audio and text?

Phase:      F7 (transcription, only if needed) / D2 (synthesis)
Tasks:      docs/TASKS.md#t-d2-02
Adapter:    not built yet
Per-vertical: NO

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
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.media import MediaRef


class SpeechPort(Protocol):
    def transcribe(self, audio: MediaRef) -> str:
        """PSEUDO-CODE - F7, only when the model lacks native audio input.

        Return plain text. If the provider returns segments or timestamps, flatten them:
        the agent wants a message, not a transcript structure.

        FAILURE MODE: an empty transcription must RAISE, not return "". An empty string
        reaches the model as an empty user message and produces a confidently wrong reply
        about nothing, with no error anywhere.
        """
        ...

    def synthesize(self, text: str, *, voice: str | None = None) -> bytes:
        """PSEUDO-CODE - D2. Caller stores the result via MediaStore and the channel sends it.

        Only called when `profile.media.allow_speech_output` is True. Check it at the call
        site, not here - the port should not need to know about profiles.

        Cap the input length. A model that emits a two-thousand-word answer would
        otherwise produce a fifteen-minute audio file nobody will listen to, at full TTS
        price. Truncate with a spoken note, or refuse.
        """
        ...
