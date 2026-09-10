"""Driving adapter: the DBOS workflow that orchestrates one turn.

Phase:   F2 (durability) / F3 (the human wait) / F5 (compaction step)
Tasks:   docs/TASKS.md#t-f2-01
Status:  PSEUDO-CODE ONLY

THIS FILE IS THE REASON FOR NON-NEGOTIABLE #1
    A DBOS workflow is a DRIVING ADAPTER, exactly like a FastAPI router. It orchestrates
    steps; each step resolves a use case and calls it.

    NEVER put @DBOS.workflow() on a use case. That drags infrastructure into the
    application layer: tests then need Postgres, and the use case is married to an
    orchestration engine. docs/DECISIONS.md#d6.

WHAT DBOS BUYS, STATED SO NOBODY REIMPLEMENTS IT
    - Crash recovery: die after step 1, restart, step 1 is NOT re-run.
    - Durable waits: DBOS.recv() survives restarts, redeploys and three days of silence.
    - Per-session serialisation and isolation for the compaction pass.

    Hermes hand-built the equivalent - snapshot, commit fence, durable lock - across
    several hundred lines. Here it is decorators. Do not rebuild it.

THE SHAPE (verify signatures against the installed DBOS version before coding - F2/F3)

    @DBOS.workflow()
    def run_turn_workflow(request: TurnRequest) -> TurnResult:
        turn_id = _step_new_turn_id()          # id generated INSIDE a step
        outcome = _step_start(turn_id, request)

        rounds = 0
        while outcome.pending:
            rounds += 1
            if rounds > MAX_HUMAN_ROUNDS:
                return _step_abandon(turn_id, "too many human round-trips")

            _step_publish(turn_id, outcome.pending)
            answer  = DBOS.recv(timeout_seconds=THREE_DAYS)
            if answer is None:
                return _step_expire(turn_id)
            outcome = _step_resume(turn_id, answer)

        return outcome.result

    @DBOS.step()
    def _step_start(turn_id, request):
        return container.start_turn.execute(turn_id, request)
"""

from __future__ import annotations

from typing import Final

# Three days. Long enough for a weekend plus a public holiday, short enough that an
# abandoned turn does not sit in the database forever.
THREE_DAYS: Final[int] = 3 * 24 * 60 * 60

# A resumed turn may suspend AGAIN - an approval unlocks a tool whose result triggers an
# evidence request. That is legitimate, but it needs a bound: without one, a badly written
# tool that always defers turns the workflow into an infinite human-round-trip loop that
# pesters a real person forever. Bound it and abandon loudly.
MAX_HUMAN_ROUNDS: Final[int] = 5


def run_turn_workflow() -> None:
    """PSEUDO-CODE - implement in F2, extended in F3 and F5.

    RULES THAT MAKE REPLAY CORRECT - each of these produces a SILENT bug when broken,
    visible only during crash recovery:

    R1. NON-DETERMINISM LIVES INSIDE STEPS.
        Clock, UUID, randomness - all generated inside @DBOS.step(). The workflow body
        must produce the same sequence of decisions on replay. A `uuid4()` in the body
        yields a new turn_id after a crash and forks the conversation.

    R2. NO SIDE EFFECT OUTSIDE A STEP.
        Every write, publish and model call is a step. A side effect in the body runs
        again on every replay.

    R3. THE MODEL CALL IS NEVER INSIDE A DATABASE TRANSACTION.
        A forty-second step holding a connection ends in pool exhaustion under load.
        CLAUDE.md #3.

    R4. STEPS ARE IDEMPOTENT WHERE THEY TOUCH THE WORLD.
        `_step_resume` keys on (turn_id, tool_call_id) and treats an already-resolved id
        as a no-op returning the stored outcome. Without it, a crash between the tool
        running and the outcome being persisted freezes the account twice on recovery.

    R5. COMPACTION IS A STEP, NOT A HOOK.
        _step_compact() wraps CompactContext. DBOS's durable lock gives one pass per
        session for free.

    THE F2 ACCEPTANCE TEST, WHICH IS ALSO THE ONLY WAY THESE BUGS SURFACE:
        Start a multi-tool turn. Kill the process mid-flight. Restart. Assert the turn
        completes and the already-executed tool did NOT run twice. A green unit suite
        proves nothing about any of R1-R5.
    """
    raise NotImplementedError("F2 - docs/TASKS.md#t-f2-01")
