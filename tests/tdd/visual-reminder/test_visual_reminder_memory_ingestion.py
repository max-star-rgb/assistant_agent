from __future__ import annotations

from types import SimpleNamespace

from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.memory.session_snapshot import SessionMemorySnapshotStore
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME
from assistant_agent.tools.models import ToolResult


class _RecordingClient:
    configured = True

    def __init__(self) -> None:
        self.turns = []

    def ingest_completed_turn(self, turn):
        self.turns.append(turn)
        return SimpleNamespace(accepted=True, memory_ids=[], errors=[], changes=[])


def _service(client: _RecordingClient) -> LongTermMemoryService:
    return LongTermMemoryService(
        client=client,
        snapshot_store=SessionMemorySnapshotStore(),
        ingestion_queue=MemoryIngestionQueue(max_workers=1, max_pending=4),
    )


def _completed_state(*tool_results: ToolResult) -> AgentState:
    state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="request")
    )
    state.status = "completed"
    state.response = AgentResponse(message="response")
    state.tool_results = list(tool_results)
    return state


def test_pure_connection_visual_reminder_turn_skips_mem0() -> None:
    client = _RecordingClient()
    service = _service(client)
    state = _completed_state(
        ToolResult(
            tool_name=VISUAL_REMINDER_MANAGE_TOOL_NAME,
            success=True,
            data={"status": "pending"},
        )
    )

    assert service.enqueue_completed_turn(state=state, trace_store=None) is False
    assert state.request.metadata["memory_ingestion"] == {
        "status": "skipped",
        "reason": "connection_scoped_visual_reminder",
    }
    assert service.ingestion_queue.pending_count == 0
    assert client.turns == []
    service.close(timeout=1.0)


def test_mixed_tool_turn_and_plain_turn_still_reach_mem0() -> None:
    client = _RecordingClient()
    service = _service(client)
    reminder = ToolResult(
        tool_name=VISUAL_REMINDER_MANAGE_TOOL_NAME,
        success=True,
        data={"status": "pending"},
    )
    other = ToolResult(tool_name="other_tool", success=True, data={"status": "ok"})

    assert service.enqueue_completed_turn(
        state=_completed_state(reminder, other),
        trace_store=None,
    ) is True
    assert service.enqueue_completed_turn(
        state=_completed_state(),
        trace_store=None,
    ) is True
    assert service.drain(timeout=1.0) is True
    assert len(client.turns) == 2
    service.close(timeout=1.0)
