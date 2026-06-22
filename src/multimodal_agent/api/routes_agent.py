"""Agent HTTP routes."""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.api import AgentRunResponse, PROTOCOL_VERSION
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.assistant_run_service import create_runtime, run_assistant_request, runtime_info
from multimodal_agent.services.trace_query import RunSummary, ToolCallSummary, TraceQueryService, TraceSummary


router = APIRouter()
_RUNTIME: AgentGraphRuntime | None = None
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCENARIO_PATH = _REPO_ROOT / "demo_data" / "scenarios" / "e2e_demo_scenarios.json"


def get_agent_runtime() -> AgentGraphRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = create_runtime()
    return _RUNTIME


@router.post("/agent/run", response_model=AgentRunResponse)
def run_agent(request: UserRequest) -> AgentRunResponse:
    return run_assistant_request(request, runtime=get_agent_runtime()).api_response()


@router.get("/demo/scenarios")
def list_demo_scenarios() -> dict[str, Any]:
    scenarios = json.loads(_SCENARIO_PATH.read_text(encoding="utf-8"))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "offline": True,
        "total": len(scenarios),
        "scenarios": [_public_scenario(scenario) for scenario in scenarios],
    }


@router.get("/demo/runtime-info")
def demo_runtime_info() -> dict[str, Any]:
    """Return a redacted runtime summary for the local Web Console."""

    config = get_agent_runtime().config
    return {
        "protocol_version": PROTOCOL_VERSION,
        **runtime_info(config),
    }


@router.get("/runs/{run_id}", response_model=RunSummary)
def get_run_summary(run_id: str) -> RunSummary:
    summary = TraceQueryService(get_agent_runtime().trace_store).run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


def _public_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(scenario.get("metadata", {}))
    return {
        "scenario_id": scenario["scenario_id"],
        "title": scenario["title"],
        "user_query": scenario["user_query"],
        "input_type": metadata.get("input_type", "text"),
        "expected_tools": list(scenario.get("expected_tools", [])),
        "expected_response_contains": list(scenario.get("expected_response_contains", [])),
        "mock_only": bool(metadata.get("mock_only") or metadata.get("mock_media")),
    }


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
