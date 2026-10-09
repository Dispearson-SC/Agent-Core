"""Driven adapter: the Summariser behind ladder rungs L3 and L4.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-10
Status:  IMPLEMENTED (t-f5-10). Fills the `Summariser` seat in engine.py.
Fills:   `Summariser` in adapters/driven/context/engine.py

THIS FILE EXISTS BECAUSE A SEAT WITH NO ADAPTER IS A GAP WITH A DEFAULT
    `LadderContextEngine` takes a `Summariser` OPTIONALLY, and until now nothing in the
    tree built one, so `composition.py` wired `None`. L3 and L4 returned on their first
    line on every turn: the ladder looked fully wired and its top half freed nothing.
    Every test passed. The only witness was the invoice - CLAUDE.md's silent-bug table
    calls that out by name for exactly this subsystem.

WHICH MODEL, AND WHY - THIS IS THE PART THAT COSTS MONEY
    Default: `minimax/MiniMax-M3`, reached through `ModelGateway` so the day-2 proxy gets
    the bare alias `MiniMax-M3` and day 1 gets the provider-qualified id. It is the
    default for one unglamorous reason: it is the only CHAT model the proxy actually
    grants either tenant team (docs/STATE.md's infrastructure table lists `MiniMax-M3` and
    `MiniMax-Speech`, and nothing else). Picking a cheaper alias out of litellm's registry
    would produce a summariser that 404s in proxy mode and works in library mode - a
    difference that shows up in production and never in a test.

    So the cheapness here is NOT the model. It is the two bounds below, and they are the
    whole economic argument of this file:

      * `max_input_chars` caps what is SENT. A 200-turn history rendered whole into a
        summarisation prompt costs more to send than the compaction frees, and it grows
        with the conversation - so the trap gets worse exactly as the ladder gets more
        useful. The middle is elided; the opening and the most recent material survive.
      * `max_summary_chars` caps what is KEPT. An unbounded summary makes L3 hand back a
        history larger than it received; the engine's own guard then reverts the rung, and
        the model call is paid for and buys nothing. A prompt asking for brevity is a
        request, not a guarantee, so the bound is enforced on the way out.

    `CompactionPolicy.summariser_model` overrides the default per profile and arrives here
    as `SummaryRequest.model`. That field already existed with nothing reading it; the day
    a deployment is granted something cheaper, that is the only edit.

    Reasoning tokens are billed and not asked for: docs/FIELD-NOTES.md records that M3
    reasons in BOTH modes and that `reasoning_effort` is rejected by this litellm version
    unless allow-listed. Nothing here passes it. `max_tokens` is set from the output bound
    so the provider stops generating at roughly the point this adapter would truncate -
    paying for tokens that are then thrown away is the same waste at a different address.

A FAILING PROVIDER MUST NOT FAIL THE TURN
    `LadderContextEngine.compress` puts no handler around the summariser call, so an
    exception raised here propagates out of compaction and kills a turn that was only
    trying to make room. That is strictly worse than the inert ladder this anchor repairs.
    The answer is `_extractive_fallback`: bounded, marked as degraded so nobody reads it
    as a real summary, and DETERMINISTIC - the same input gives the same text on every
    replay, which is what a checkpoint id hashed from it (engine.py `_checkpoint_id`)
    requires when a DBOS step is retried.

PAIRING IS NOT THIS FILE'S TO BREAK, AND THAT IS DELIBERATE
    CLAUDE.md #5 - a tool call separated from its return makes the provider reject the
    conversation. This adapter returns TEXT. It never sees, reorders or drops a message,
    so there is no code path here that could separate a `ToolCallPart` from its
    `ToolReturnPart`; the engine cuts on exchange boundaries and places the summary
    between whole exchanges. `Core/tests/unit/test_summariser.py` reads that pairing back
    off a history this summariser actually compacted, because "structurally impossible"
    and "checked once" are different claims.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from agent_core.adapters.driven.context.engine import CHARS_PER_TOKEN, SummaryRequest
from agent_core.ports.model_gateway import ModelGateway

DEFAULT_SUMMARISER_MODEL = "minimax/MiniMax-M3"
"""See WHICH MODEL, AND WHY. Overridden per profile by `CompactionPolicy.summariser_model`."""

DEFAULT_MAX_SUMMARY_CHARS = 2_000
"""~500 tokens. Small enough that L3 always frees room, large enough to carry the task,
the decisions taken and the open questions - which is what a summary is for."""

DEFAULT_MAX_INPUT_CHARS = 60_000
"""~15_000 tokens sent per summarisation. The cost of the call is bounded by the CAP, not
by the conversation, which is the only way this stays affordable at turn 200."""

_ELISION = "\n[...omitted from the middle of the material...]\n"
_TRUNCATED = " [...truncated]"
_DEGRADED = "[summariser unavailable - verbatim extract, not a summary]\n"

_INSTRUCTION = (
    "You compact a running conversation so an assistant can continue it with less "
    "context. Write a single dense paragraph-per-topic summary of the material below.\n"
    "Keep, in this order of priority: what the user asked for and any constraints they "
    "set; decisions already taken and why; facts established by tool results; and "
    "anything still open or promised.\n"
    "Drop pleasantries, restatements and the raw text of tool output.\n"
    "Write only the summary. Do not add a preamble, a heading or a closing remark. "
    "Invent nothing that is not in the material.\n"
    "Hard limit: {limit} characters."
)

_FOLD_INSTRUCTION = (
    "A summary of the earlier part of this conversation already exists and is given "
    "first. Refine and extend it with the newer material that follows - do not repeat it "
    "back, and do not lose what it already records."
)

Completion = Callable[[str, str | None, Sequence[ModelMessage]], Awaitable[str]]
"""`(wire model id, base url, messages) -> text`. The one place a network appears.

Injected so the adapter's own behaviour - what it sends, what it keeps, how it degrades -
is provable without a provider. The default reaches the model through the same
`model_for` day-1/day-2 switch every other model call in this repository uses, so a
summariser cannot end up on a different route from the turn it is compacting.
"""


def _elide(text: str, limit: int) -> str:
    """Keep the opening and the ending, drop the middle. Never grows the text.

    Head-biased two-to-one: the opening of a conversation carries the task definition,
    which the ladder protects everywhere else for the same reason (engine.py, L3).
    """
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(_ELISION):
        return text[:limit]
    room = limit - len(_ELISION)
    head = (room * 2) // 3
    return text[:head] + _ELISION + text[len(text) - (room - head) :]


def _bound(text: str, limit: int) -> str:
    """Hard cap, cutting on a word boundary when there is one to cut on."""
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return text[:limit]
    keep = limit - len(_TRUNCATED)
    cut = text[:keep]
    space = cut.rfind(" ")
    if space > keep // 2:
        cut = cut[:space]
    return cut + _TRUNCATED


class ModelSummariser:
    """`Summariser` over one cheap, bounded model call. See the module docstring.

    `gateway` rather than a model id string: `ModelGateway.base_url()` being the entire
    day-2 code change is the promise `ports/model_gateway.py` makes, and a summariser that
    resolved its own endpoint would be the second place that promise has to be kept.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_id: str = DEFAULT_SUMMARISER_MODEL,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
        complete: Completion | None = None,
    ) -> None:
        self._gateway = gateway
        self._model_id = model_id
        self._max_summary_chars = max_summary_chars
        self._max_input_chars = max_input_chars
        self._complete: Completion = complete or self._model_request

    async def __call__(self, request: SummaryRequest) -> str:
        """One model call, bounded on both sides, degrading rather than raising."""
        # The profile's choice wins over this adapter's default; the gateway decides what
        # that name looks like on the wire. Neither answer is hard-coded here.
        model_id = self._gateway.model_id_for(request.model or self._model_id)
        base_url = self._gateway.base_url()

        try:
            answer = await self._complete(
                model_id, base_url, self._prompt(request)
            )
        except Exception:
            # Deliberately broad. Every provider failure has the same correct response
            # here, and `ModelGateway.classify_error` is not implemented yet (t-f0-04);
            # letting one through would fail a turn that was only making room.
            return self._extractive_fallback(request)

        summary = answer.strip()
        if not summary:
            # An empty summary is not free room - it is the aged material deleted with no
            # record of it, which is worse than not compacting.
            return self._extractive_fallback(request)
        return _bound(summary, self._max_summary_chars)

    # -- the prompt ------------------------------------------------------------

    def _prompt(self, request: SummaryRequest) -> list[ModelMessage]:
        """What is actually sent. Bounded before it leaves this method."""
        instruction = _INSTRUCTION.format(limit=self._max_summary_chars)
        body: list[str] = []
        if request.previous_summary:
            instruction = f"{instruction}\n{_FOLD_INSTRUCTION}"
            body.append(
                "Existing summary of the earlier conversation:\n"
                # The previous summary was produced under the same bound, but a summariser
                # constructed with a smaller one would otherwise inherit the older, larger
                # text and quietly re-expand the prompt it is supposed to shrink.
                + _elide(request.previous_summary, self._max_summary_chars)
            )
        body.append(
            "Material to summarise:\n" + _elide(request.text, self._max_input_chars)
        )
        return [
            ModelRequest(
                parts=[
                    SystemPromptPart(content=instruction),
                    UserPromptPart(content="\n\n".join(body)),
                ]
            )
        ]

    # -- the two things that touch the outside world ---------------------------

    async def _model_request(
        self, model_id: str, base_url: str | None, messages: Sequence[ModelMessage]
    ) -> str:
        """The default `Completion`: one direct request, no agent, no tools, no history.

        Imports are local for the reason `models.py` gives: pydantic-ai's OpenAI provider
        and litellm are both expensive to import, and a deployment that injects its own
        `complete` should pay for neither.
        """
        from pydantic_ai.direct import model_request
        from pydantic_ai.settings import ModelSettings

        from agent_core.adapters.driven.llm_litellm.models import model_for

        model = model_for(
            model_id,
            base_url,
            settings=ModelSettings(
                # Stop generating around where this adapter would truncate anyway.
                max_tokens=max(1, self._max_summary_chars // CHARS_PER_TOKEN),
                # A summary is not a place for sampling: the same history should compact
                # to the same text, because `_checkpoint_id` hashes it and a DBOS step
                # retry must produce the same checkpoint rather than a second row.
                temperature=0.0,
            ),
        )
        response = await model_request(model, messages)
        return "".join(
            part.content for part in response.parts if isinstance(part, TextPart)
        )

    def _extractive_fallback(self, request: SummaryRequest) -> str:
        """No model. Bounded, marked, and identical on every replay.

        It keeps the previous summary ahead of the new material: on L4 that chain is the
        only record of everything already dropped, and losing it to a transient 503 loses
        the conversation, not just this pass.
        """
        material = request.text
        if request.previous_summary:
            material = f"{request.previous_summary}\n\n{material}"
        room = self._max_summary_chars - len(_DEGRADED)
        return _bound(_DEGRADED + _elide(material, room), self._max_summary_chars)
