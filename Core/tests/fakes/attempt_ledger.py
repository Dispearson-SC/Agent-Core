"""An in-memory `AttemptLedger` with the same rules as the Postgres one (see
`tests/contract/test_attempt_ledger.py`, which runs both through one suite)."""

from __future__ import annotations


class InMemoryAttemptLedger:
    def __init__(self) -> None:
        self.attempts: dict[str, int] = {}
        self.parked: dict[str, bool] = {}
        self.begun: list[str] = []

    async def begin(self, workflow_id: str) -> int:
        self.begun.append(workflow_id)
        if workflow_id not in self.attempts or self.parked.get(workflow_id, False):
            self.attempts[workflow_id] = 1
        else:
            self.attempts[workflow_id] += 1
        self.parked[workflow_id] = False
        return self.attempts[workflow_id]

    async def park(self, workflow_id: str) -> None:
        self.parked[workflow_id] = True

    async def resume(self, workflow_id: str) -> None:
        self.parked[workflow_id] = False

    async def forget(self, workflow_id: str) -> None:
        self.attempts.pop(workflow_id, None)
        self.parked.pop(workflow_id, None)
