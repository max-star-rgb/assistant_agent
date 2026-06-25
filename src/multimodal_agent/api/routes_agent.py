"""Agent HTTP routes."""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.api import AgentRunResponse, PROTOCOL_VERSION
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.assistant_run_service import (
    clear_user_conversation_history,
    create_runtime,
    run_assistant_request,
    runtime_info,
)
from multimodal_agent.services.beta_feedback import (
    BetaEvaluationExport,
    BetaEvaluationItem,
    BetaFeedbackCreate,
    BetaFeedbackRecord,
    BetaFeedbackStore,
    summarize_feedback,
)
from multimodal_agent.services.demo_examples import get_demo_examples
from multimodal_agent.services.trace_query import RunSummary, ToolCallSummary, TraceQueryService, TraceSummary
from multimodal_agent.services.trial_access import (
    TrialAccessGate,
    TrialAccessStatus,
    trial_access_gate_from_env,
)


router = APIRouter()
_RUNTIME: AgentGraphRuntime | None = None
_FEEDBACK_STORE: BetaFeedbackStore | None = None
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCENARIO_PATH = _REPO_ROOT / "demo_data" / "scenarios" / "e2e_demo_scenarios.json"


def get_agent_runtime() -> AgentGraphRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = create_runtime()
    return _RUNTIME


def get_beta_feedback_store() -> BetaFeedbackStore:
    global _FEEDBACK_STORE
    if _FEEDBACK_STORE is None:
        _FEEDBACK_STORE = BetaFeedbackStore()
    return _FEEDBACK_STORE


def get_trial_access_gate() -> TrialAccessGate:
    return trial_access_gate_from_env(base_dir=_REPO_ROOT)


@router.post("/agent/run", response_model=AgentRunResponse)
def run_agent(request: UserRequest) -> AgentRunResponse:
    _require_trial_access(request.user_id)
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


@router.get("/demo/examples")
def list_demo_examples() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "examples": get_demo_examples(),
    }


@router.get("/demo/runtime-info")
def demo_runtime_info() -> dict[str, Any]:
    """Return a redacted runtime summary for the local Web Console."""

    config = get_agent_runtime().config
    return {
        "protocol_version": PROTOCOL_VERSION,
        **runtime_info(config),
    }


@router.get("/demo/access", response_model=TrialAccessStatus)
def demo_access(user_id: str = Query(...)) -> TrialAccessStatus:
    """Validate a Web Console trial user id before enabling the demo UI."""

    return get_trial_access_gate().check(user_id)


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


@router.post("/beta/feedback", response_model=BetaFeedbackRecord)
def submit_beta_feedback(feedback: BetaFeedbackCreate) -> BetaFeedbackRecord:
    _assert_run_belongs_to_user(feedback.run_id, feedback.user_id)
    return get_beta_feedback_store().append(feedback)


@router.get("/beta/evaluations", response_model=BetaEvaluationExport)
def export_beta_evaluations(user_id: str | None = Query(default=None)) -> BetaEvaluationExport:
    store = get_beta_feedback_store()
    records = store.list_by_user(user_id) if user_id else store.read_all()
    trace_service = TraceQueryService(get_agent_runtime().trace_store)
    items: list[BetaEvaluationItem] = []
    for record in records:
        summary = trace_service.run_summary(record.run_id)
        run_payload = summary.model_dump(mode="json") if summary is not None else {"run_id": record.run_id, "missing": True}
        items.append(BetaEvaluationItem(feedback=record, run=run_payload))
    return BetaEvaluationExport(
        user_id=user_id,
        summary=summarize_feedback(records, user_id=user_id),
        items=items,
    )


@router.delete("/beta/users/{user_id}/data")
def delete_beta_user_data(user_id: str) -> dict[str, Any]:
    runtime = get_agent_runtime()
    memory_items = runtime.memory_store.list_by_user(user_id) if hasattr(runtime.memory_store, "list_by_user") else []
    if hasattr(runtime.memory_store, "clear_user"):
        runtime.memory_store.clear_user(user_id)
    run_history_deleted = runtime.run_history.delete_by_user(user_id) if runtime.run_history is not None else 0
    tool_history_deleted = runtime.tool_history.delete_by_user(user_id) if runtime.tool_history is not None else 0
    trace_deleted = runtime.trace_store.delete_by_user(user_id)
    feedback_deleted = get_beta_feedback_store().delete_by_user(user_id)
    conversation_sessions_deleted = clear_user_conversation_history(user_id)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "user_id": user_id,
        "deleted": {
            "memory_items": len(memory_items),
            "run_history_records": run_history_deleted,
            "tool_history_records": tool_history_deleted,
            "trace_events": trace_deleted,
            "feedback_records": feedback_deleted,
            "conversation_sessions": conversation_sessions_deleted,
        },
    }


def _assert_run_belongs_to_user(run_id: str, user_id: str) -> None:
    summary = TraceQueryService(get_agent_runtime().trace_store).run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    if summary.user_id != user_id:
        raise HTTPException(status_code=403, detail="run does not belong to user")


def _require_trial_access(user_id: str) -> None:
    status = get_trial_access_gate().check(user_id)
    if not status.allowed:
        raise HTTPException(status_code=403, detail=status.reason or "trial user is not allowed")
