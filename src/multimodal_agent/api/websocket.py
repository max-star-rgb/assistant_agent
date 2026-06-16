"""Agent WebSocket routes backed by graph runtime events."""

from fastapi import APIRouter, Query, WebSocket

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.agent.state import AgentState
from multimodal_agent.schemas.api import api_error_from_agent_error
from multimodal_agent.schemas.capability_output import contract_summary
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.event_sink import ListEventSink
from multimodal_agent.services.event_sink import EventSink


router = APIRouter()


def get_agent_runtime(event_sink: EventSink | None = None) -> AgentGraphRuntime:
    return AgentGraphRuntime(event_sink=event_sink)


def mock_agent_events(session_id: str) -> list[AgentEvent]:
    """Fallback deterministic events kept for tests that need mock progress."""

    run_id = f"mock_run_{session_id}"
    return [
        AgentEvent(
            type="tool_started",
            session_id=session_id,
            run_id=run_id,
            tool_name="render_3d",
        ),
        AgentEvent(
            type="tool_progress",
            session_id=session_id,
            run_id=run_id,
            tool_name="render_3d",
            progress=0.5,
        ),
        AgentEvent(
            type="tool_completed",
            session_id=session_id,
            run_id=run_id,
            tool_name="render_3d",
            output_ref="mock://render/preview.png",
        ),
        AgentEvent(
            type="agent_response",
            session_id=session_id,
            run_id=run_id,
            text="渲染完成。",
        ),
    ]


def graph_runtime_events(state: AgentState) -> list[AgentEvent]:
    """Convert graph runtime state into observable WebSocket events."""

    events: list[AgentEvent] = []
    results_by_tool = {result.tool_name: result for result in state.tool_results}
    for call in state.tool_calls:
        events.append(
            AgentEvent(
                type="tool_started",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=call.tool_name,
            )
        )
        result = results_by_tool.get(call.tool_name)
        if call.status == "failed":
            matching_error = next((error for error in state.errors if error.source == call.tool_name), None)
            events.append(
                AgentEvent(
                    type="tool_failed",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=call.tool_name,
                    error=(
                        api_error_from_agent_error(matching_error).model_dump(mode="json")
                        if matching_error is not None
                        else {
                            "code": "TASK_FAILED",
                            "message": call.error_message or (result.error if result else "Tool failed."),
                            "detail": {"source": call.tool_name},
                            "recoverable": False,
                        }
                    ),
                    payload={"contract": contract_summary(result.contract if result else None)},
                )
            )
        else:
            events.append(
                AgentEvent(
                    type="tool_completed",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=call.tool_name,
                    output_ref=call.output_ref or (result.output_ref if result else None),
                    payload={"contract": contract_summary(result.contract if result else None)},
                )
            )
    if state.status == "failed":
        events.append(
            AgentEvent(
                type="agent_error",
                session_id=state.session_id,
                run_id=state.run_id,
                error=(
                    api_error_from_agent_error(state.errors[-1]).model_dump(mode="json")
                    if state.errors
                    else {"code": "TASK_FAILED", "message": "Agent run failed.", "detail": {}, "recoverable": False}
                ),
            )
        )
    else:
        events.append(
            AgentEvent(
                type="agent_response",
                session_id=state.session_id,
                run_id=state.run_id,
                text=state.response.message if state.response else "",
            )
        )
    return events


@router.websocket("/ws/agent/{session_id}")
async def agent_websocket(
    websocket: WebSocket,
    session_id: str,
    text: str = Query(default="渲染到客厅场景"),
    user_id: str = Query(default="u1"),
    video_id: str | None = Query(default=None),
) -> None:
    await websocket.accept()
    request = UserRequest(user_id=user_id, session_id=session_id, text=text, video_ids=[video_id] if video_id else [])
    event_sink = ListEventSink()
    get_agent_runtime(event_sink=event_sink).run_state(request)
    for event in event_sink.events:
        await websocket.send_json(event.model_dump(mode="json", exclude_none=True))
    await websocket.close()
