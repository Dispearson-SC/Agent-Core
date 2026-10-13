"""In-memory `ToolJournal` for unit tests (C30)."""

from __future__ import annotations

from agent_core.domain.turn import TurnId
from agent_core.ports.tool_journal import ToolRunState


class FakeToolJournal:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, ToolRunState]] = {}

    async def begin(self, turn_id: TurnId, tool_call_id: str, tool: str) -> ToolRunState:
        key = (str(turn_id), tool_call_id)
        if key in self.rows:
            return self.rows[key][1]
        self.rows[key] = (tool, ToolRunState.STARTED)
        return ToolRunState.FRESH

    async def finish(self, turn_id: TurnId, tool_call_id: str) -> None:
        key = (str(turn_id), tool_call_id)
        if key in self.rows:
            self.rows[key] = (self.rows[key][0], ToolRunState.FINISHED)

    async def abandon(self, turn_id: TurnId, tool_call_id: str) -> None:
        self.rows.pop((str(turn_id), tool_call_id), None)
