"""Agent HTTP routes."""

from fastapi import APIRouter, HTTPException

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.api import AgentRunResponse, agent_run_response_from_state
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.trace_query import RunSummary, ToolCallSummary, TraceQueryService, TraceSummary


router = APIRouter()


def get_agent_runtime() -> AgentGraphRuntime:
    return AgentGraphRuntime()


@router.post("/agent/run", response_model=AgentRunResponse)
def run_agent(request: UserRequest) -> AgentRunResponse:
    state = get_agent_runtime().run_state(request)
    return agent_run_response_from_state(state)


@router.get("/runs/{run_id}", response_model=RunSummary)
def get_run_summary(run_id: str) -> RunSummary:
    summary = TraceQueryService(get_agent_runtime().trace_store).run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


@router.get("/traces/{trace_id}", response_model=TraceSummary)
def get_trace_summary(trace_id: str) -> TraceSummary:
    summary = TraceQueryService(get_agent_runtime().trace_store).trace_summary(trace_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return summary


@router.get("/runs/{run_id}/tool-calls", response_model=ToolCallSummary)
def get_run_tool_calls(run_id: str) -> ToolCallSummary:
    summary = TraceQueryService(get_agent_runtime().trace_store).tool_calls_by_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary
