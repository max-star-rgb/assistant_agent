"""Agent HTTP routes."""

import json
import os
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from assistant_agent.agent_routing import AgentRouteRequest, AgentRouter, create_default_agent_router
from assistant_agent.api import gateway_runtime
from assistant_agent.api.auth import get_auth_context, require_auth_bound_identity
from assistant_agent.schemas.agent_control_plane import (
    AgentAuditEvent,
    AgentAuditEventList,
    AgentControlPlaneBudgetSummary,
    AgentControlPlaneDelegationTree,
    AgentControlPlaneReplayPreview,
    AgentControlPlaneRouteSummary,
    AgentControlPlaneRunSummary,
)
from assistant_agent.schemas.api import AgentRunResponse, PROTOCOL_VERSION
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryType
from assistant_agent.schemas.memory_audit import (
    MemoryAuditEventList,
    MemoryAuditItem,
    MemoryAuditList,
    MemoryAuditReport,
    MemoryConfirmationList,
    MemoryConfirmationResult,
    MemoryDeleteResult,
    MemoryExport,
    MemoryMetricsReport,
    MemoryProfileRepairResult,
    MemoryRetentionSweepResult,
)
from assistant_agent.schemas.memory_snapshot import MemorySnapshot, MemoryStorageSnapshot
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.sessions import SessionCreate, SessionDeleteResult, SessionList, SessionRecord
from assistant_agent.services.assistant_run_service import (
    create_runtime,
    get_default_conversation_store as _get_default_conversation_store,
)
from assistant_agent.services.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.services.agent_control_plane import AgentControlPlaneQueryService, audit_event
from assistant_agent.services.api_identity import (
    ApiIdentitySource,
    AuthContext,
    IdentityPolicy,
    IdentityPolicyError,
    ResolvedRequestIdentity,
    enforce_identity_policy,
    resolve_request_identity,
)
from assistant_agent.services.beta_feedback import (
    BetaEvaluationExport,
    BetaEvaluationItem,
    BetaFeedbackCreate,
    BetaFeedbackRecord,
    BetaFeedbackStore,
    summarize_feedback,
)
from assistant_agent.services.demo_examples import get_demo_examples
from assistant_agent.services.gateway_turn_facade import (
    GatewayTurnError,
    GatewayTurnRequest,
    GatewayTurnResult,
    GatewayTurnTimeout,
)
from assistant_agent.services.memory_audit import MemoryAuditService
from assistant_agent.services.memory_snapshot import MemorySnapshotService
from assistant_agent.services.agent_pilot_readiness import PilotReadinessChecker, PilotReadinessReport
from assistant_agent.services.provider_budget import ProviderCallBudget
from assistant_agent.services.provider_readiness import build_provider_readiness_report
from assistant_agent.services.trace_query import (
    ContextReportQueryResult,
    RunSummary,
    ToolCallSummary,
    TraceSummary,
)
from assistant_agent.services.trace_persistence import (
    close_trace_store,
    create_server_trace_store,
)
from assistant_agent.services.trace_conversation import (
    TraceConversationView,
    find_trace_conversation,
)
from assistant_agent.services.trial_access import (
    TrialAccessGate,
    TrialAccessStatus,
    trial_access_gate_from_env,
)


router = APIRouter()
_RUNTIME: Any | None = None
_AGENT_ROUTER: AgentRouter | None = None
_FEEDBACK_STORE: BetaFeedbackStore | None = None
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCENARIO_PATH = _REPO_ROOT / "demo_data" / "scenarios" / "e2e_demo_scenarios.json"
SERVER_TRACE_ENABLED_ENV = "MULTIMODAL_AGENT_SERVER_TRACE_ENABLED"
LOCAL_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"


def get_agent_runtime() -> Any:
    global _RUNTIME
    if _RUNTIME is None:
        trace_store = (
            create_server_trace_store()
            if os.environ.get(SERVER_TRACE_ENABLED_ENV) == "1"
            else None
        )
        _RUNTIME = create_runtime(trace_store=trace_store)
    return _RUNTIME


def shutdown_agent_runtime() -> None:
    """Flush owned trace persistence and clear the process runtime singleton."""

    global _RUNTIME
    runtime = _RUNTIME
    _RUNTIME = None
    if runtime is not None:
        close_trace_store(getattr(runtime, "trace_store", None), timeout=1.0)


def get_assistant_runtime_app() -> AssistantRuntimeApp:
    return AssistantRuntimeApp(runtime_factory=get_agent_runtime)


def get_default_conversation_store(config=None):
    """Return the configured conversation store for API memory/session views."""

    return _get_default_conversation_store(config)


def get_agent_router() -> AgentRouter:
    global _AGENT_ROUTER
    if _AGENT_ROUTER is None:
        _AGENT_ROUTER = create_default_agent_router()
    return _AGENT_ROUTER


def get_beta_feedback_store() -> BetaFeedbackStore:
    global _FEEDBACK_STORE
    if _FEEDBACK_STORE is None:
        _FEEDBACK_STORE = BetaFeedbackStore()
    return _FEEDBACK_STORE


def get_trial_access_gate() -> TrialAccessGate:
    return trial_access_gate_from_env(base_dir=_REPO_ROOT)


async def _run_agent_through_gateway(request: UserRequest) -> AgentRunResponse:
    capture_id = gateway_runtime.new_gateway_http_response_capture_id()
    try:
        turn = await gateway_runtime.get_gateway_turn_facade().run_turn(
            GatewayTurnRequest(
                user_id=request.user_id,
                session_id=request.session_id,
                text=request.text or "",
                image_ids=list(request.image_ids),
                video_ids=list(request.video_ids),
                audio_id=request.audio_id,
                metadata=_gateway_http_metadata(request, capture_id),
            )
        )
    except GatewayTurnTimeout as exc:
        gateway_runtime.pop_gateway_http_response(capture_id)
        raise HTTPException(
            status_code=504,
            detail={
                "code": "GATEWAY_TURN_TIMEOUT",
                "message": str(exc),
                "recoverable": True,
            },
        ) from exc
    except GatewayTurnError as exc:
        gateway_runtime.pop_gateway_http_response(capture_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "GATEWAY_TURN_FAILED",
                "message": str(exc),
                "recoverable": False,
            },
        ) from exc

    response = gateway_runtime.pop_gateway_http_response(capture_id)
    if response is not None:
        return response
    raise _missing_gateway_http_response(turn)


def _gateway_http_metadata(request: UserRequest, capture_id: str) -> dict[str, Any]:
    metadata = dict(request.metadata)
    gateway_metadata = metadata.get("gateway")
    gateway_payload = dict(gateway_metadata) if isinstance(gateway_metadata, dict) else {}
    gateway_payload.update(gateway_runtime.gateway_http_capture_metadata(capture_id)["gateway"])
    gateway_payload["suppress_realtime_backend_source"] = True
    metadata["gateway"] = gateway_payload
    metadata["execution_strategy"] = request.execution_strategy
    return metadata


def _missing_gateway_http_response(turn: GatewayTurnResult) -> HTTPException:
    if turn.status == "error":
        error = turn.terminal_frame.get("error")
        detail = error if isinstance(error, dict) else {}
        return HTTPException(
            status_code=500,
            detail={
                "code": "GATEWAY_RUN_FAILED",
                "message": detail.get("message") or "Gateway run failed before HTTP response capture.",
                "error_type": detail.get("error_type"),
                "recoverable": False,
            },
        )
    return HTTPException(
        status_code=500,
        detail={
            "code": "GATEWAY_HTTP_RESPONSE_MISSING",
            "message": "Gateway run completed without a captured HTTP response.",
            "run_id": turn.run_id,
            "trace_id": turn.trace_id,
            "recoverable": False,
        },
    )


@router.post("/agent/run", response_model=AgentRunResponse)
async def run_agent(request: UserRequest, auth_context: AuthContext = Depends(get_auth_context)) -> AgentRunResponse:
    identity_resolution = _identity_from_request(request, auth_context=auth_context)
    _require_trial_access_for_identity(identity_resolution)
    request = _with_identity_metadata(request, identity_resolution)
    return await _run_agent_through_gateway(request)


@router.post("/agents/run", response_model=AgentRunResponse)
def run_agents(
    request: AgentRouteRequest,
    auth_context: AuthContext = Depends(get_auth_context),
) -> AgentRunResponse:
    identity_resolution = _identity_from_request(request, auth_context=auth_context)
    _require_trial_access_for_identity(identity_resolution)
    _record_auth_audit_event(identity_resolution, action="agents_run_identity")
    request = _with_identity_metadata(request, identity_resolution)
    return get_agent_router().run(request)


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
    """Return a redacted runtime summary for local demo/debug clients."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        **get_assistant_runtime_app().runtime_info(),
    }


@router.get("/demo/access", response_model=TrialAccessStatus)
def demo_access(user_id: str = Query(...)) -> TrialAccessStatus:
    """Validate a pilot trial user id before enabling demo/realtime access."""

    return get_trial_access_gate().check(user_id)


@router.post("/sessions", response_model=SessionRecord)
def create_session(
    session: SessionCreate,
    auth_context: AuthContext = Depends(get_auth_context),
) -> SessionRecord:
    _require_trial_access_for_identity(_identity_from_user_id(session.user_id, source="request_body", auth_context=auth_context))
    return get_assistant_runtime_app().create_session(session)


@router.get("/sessions", response_model=SessionList)
def list_sessions(
    user_id: str = Query(...),
    auth_context: AuthContext = Depends(get_auth_context),
) -> SessionList:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="query", auth_context=auth_context))
    return get_assistant_runtime_app().list_sessions(identity.user_id)


@router.get("/sessions/{session_id}", response_model=SessionRecord)
def get_session(
    session_id: str,
    user_id: str = Query(...),
    auth_context: AuthContext = Depends(get_auth_context),
) -> SessionRecord:
    identity = _require_trial_access_for_identity(
        _identity_from_user_id(user_id, session_id=session_id, source="query", auth_context=auth_context)
    )
    record = get_assistant_runtime_app().get_session(identity.user_id, session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="session not found")
    return record


@router.delete("/sessions/{session_id}", response_model=SessionDeleteResult)
def delete_session(
    session_id: str,
    user_id: str = Query(...),
    auth_context: AuthContext = Depends(get_auth_context),
) -> SessionDeleteResult:
    identity = _require_trial_access_for_identity(
        _identity_from_user_id(user_id, session_id=session_id, source="query", auth_context=auth_context)
    )
    deleted = 1 if get_assistant_runtime_app().delete_session(identity.user_id, session_id) else 0
    if deleted == 0:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionDeleteResult(user_id=identity.user_id, deleted={"sessions": deleted})


@router.get("/runs/{run_id}", response_model=RunSummary)
def get_run_summary(run_id: str) -> RunSummary:
    summary = get_assistant_runtime_app().trace_query().run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


@router.get("/runs/{run_id}/context", response_model=ContextReportQueryResult)
def get_run_context(run_id: str) -> ContextReportQueryResult:
    summary = get_assistant_runtime_app().trace_query().context_by_run(run_id)
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
    summary = get_assistant_runtime_app().trace_query().trace_summary(trace_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return summary


@router.get("/traces/{trace_id}/conversation", response_model=TraceConversationView)
def get_trace_conversation(trace_id: str, request: Request) -> TraceConversationView:
    if os.environ.get(LOCAL_TRACE_CONTENT_ENV) != "1":
        raise HTTPException(status_code=404, detail="trace conversation not found")
    if not _is_loopback_client(request):
        raise HTTPException(status_code=403, detail="trace conversation is available only on loopback")

    runtime = get_agent_runtime()
    events = runtime.trace_store.list_by_trace(trace_id)
    identity_event = next(
        (event for event in events if event.user_id and event.session_id),
        None,
    )
    if identity_event is None:
        raise HTTPException(status_code=404, detail="trace conversation not found")
    conversation = find_trace_conversation(
        get_default_conversation_store(runtime.config),
        user_id=identity_event.user_id,
        session_id=identity_event.session_id,
        trace_id=trace_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="trace conversation not found")
    return conversation


@router.get("/traces/{trace_id}/context", response_model=ContextReportQueryResult)
def get_trace_context(trace_id: str) -> ContextReportQueryResult:
    summary = get_assistant_runtime_app().trace_query().context_by_trace(trace_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return summary


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@router.get("/runs/{run_id}/tool-calls", response_model=ToolCallSummary)
def get_run_tool_calls(run_id: str) -> ToolCallSummary:
    summary = get_assistant_runtime_app().trace_query().tool_calls_by_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


@router.get("/control-plane/readiness", response_model=PilotReadinessReport)
def get_control_plane_readiness(auth_context: AuthContext = Depends(get_auth_context)) -> PilotReadinessReport:
    identity_policy = IdentityPolicy().evaluate(
        identity_source="auth_context" if auth_context.authenticated else "local_context",
        auth_bound_identity=auth_context.authenticated,
        production_required=require_auth_bound_identity(),
    )
    agent_router = get_agent_router()
    config = get_assistant_runtime_app().config
    report = PilotReadinessChecker().evaluate(
        directory=getattr(agent_router, "directory", None),
        runtime_profile=config.runtime_profile,
        auth_bound_identity=auth_context.authenticated,
        identity_policy=identity_policy,
        provider_readiness=build_provider_readiness_report(config),
        provider_budget=ProviderCallBudget(allow_real_provider=config.runtime_profile.allows_real_providers),
    )
    _record_control_plane_audit_event(
        audit_event(
            event_type="provider_opt_in_decision",
            component="provider_policy",
            action="evaluate_runtime_profile",
            outcome="allowed" if config.runtime_profile.allows_real_providers else "blocked_default",
            detail={
                "runtime_profile": config.runtime_profile.name,
                "allows_real_providers": config.runtime_profile.allows_real_providers,
                "requires_explicit_provider_config": config.runtime_profile.requires_explicit_provider_config,
            },
        )
    )
    return report


@router.get("/control-plane/audit/events", response_model=AgentAuditEventList)
def list_control_plane_audit_events(
    run_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    auth_context: AuthContext = Depends(get_auth_context),
) -> AgentAuditEventList:
    if user_id:
        identity = _require_trial_access_for_identity(
            _identity_from_user_id(user_id, session_id=session_id, source="query", auth_context=auth_context)
        )
        user_id = identity.user_id
        session_id = identity.session_id or session_id
    return _control_plane_query_service().audit_events(
        run_id=run_id,
        trace_id=trace_id,
        user_id=user_id,
        session_id=session_id,
        event_type=event_type,
        limit=limit,
    )


@router.get("/control-plane/runs/{run_id}/audit", response_model=AgentAuditEventList)
def get_control_plane_run_audit_events(
    run_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
) -> AgentAuditEventList:
    return _control_plane_query_service().audit_events_by_run(run_id, limit=limit)


@router.get("/control-plane/runs/{run_id}", response_model=AgentControlPlaneRunSummary)
def get_control_plane_run_summary(run_id: str) -> AgentControlPlaneRunSummary:
    summary = _control_plane_query_service().run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


@router.get("/control-plane/traces/{trace_id}")
def get_control_plane_trace_summary(trace_id: str) -> dict[str, Any]:
    summary = _control_plane_query_service().trace_summary(trace_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return summary


@router.get("/control-plane/runs/{run_id}/route", response_model=AgentControlPlaneRouteSummary)
def get_control_plane_route_summary(run_id: str) -> AgentControlPlaneRouteSummary:
    summary = _control_plane_query_service().route_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="gateway route not found")
    return summary


@router.get("/control-plane/runs/{run_id}/delegation-tree", response_model=AgentControlPlaneDelegationTree)
def get_control_plane_delegation_tree(run_id: str) -> AgentControlPlaneDelegationTree:
    summary = _control_plane_query_service().delegation_tree(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="gateway run not found")
    return summary


@router.get("/control-plane/runs/{run_id}/budget", response_model=AgentControlPlaneBudgetSummary)
def get_control_plane_budget_summary(run_id: str) -> AgentControlPlaneBudgetSummary:
    summary = _control_plane_query_service().budget_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


@router.get("/control-plane/runs/{run_id}/replay-preview", response_model=AgentControlPlaneReplayPreview)
def get_control_plane_replay_preview(run_id: str) -> AgentControlPlaneReplayPreview:
    summary = _control_plane_query_service().replay_preview(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="gateway replay preview not found")
    return summary


@router.get("/memory/users/{user_id}/items", response_model=MemoryAuditList)
def list_memory_items(
    user_id: str,
    memory_type: MemoryType | None = Query(default=None),
    include_content: bool = Query(default=False),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryAuditList:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    result = _memory_audit_service().list_items_for_identity(
        identity,
        memory_type=memory_type,
        include_content=include_content,
    )
    _record_memory_control_plane_event(
        identity,
        action="list_memory_items",
        event_type="memory_access",
        detail={
            "memory_type": memory_type,
            "include_content": include_content,
            "item_count": result.total,
        },
    )
    return result


@router.get("/memory/users/{user_id}/items/{memory_id}", response_model=MemoryAuditItem)
def get_memory_item(
    user_id: str,
    memory_id: str,
    include_content: bool = Query(default=True),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryAuditItem:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    item = _memory_audit_service().get_item_for_identity(
        identity,
        memory_id=memory_id,
        include_content=include_content,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    _record_memory_control_plane_event(
        identity,
        action="get_memory_item",
        event_type="memory_access",
        detail={
            "memory_id": memory_id,
            "include_content": include_content,
            "content_included": include_content,
        },
    )
    return item


@router.get("/memory/users/{user_id}/audit", response_model=MemoryAuditReport)
def audit_memory(
    user_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryAuditReport:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    result = _memory_audit_service().audit_for_identity(identity)
    _record_memory_control_plane_event(
        identity,
        action="audit_memory",
        event_type="memory_access",
        detail={
            "total": result.total,
            "duplicate_group_count": len(result.duplicate_groups),
            "warning_count": len(result.warnings),
            "expired_count": result.expired_count,
            "sensitive_count": result.sensitive_count,
            "profile_present": result.profile_present,
        },
    )
    return result


@router.get("/memory/users/{user_id}/events", response_model=MemoryAuditEventList)
def list_memory_events(
    user_id: str,
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryAuditEventList:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    return _memory_audit_service().events_for_identity(
        identity,
        event_type=event_type,
        limit=limit,
    )


@router.get("/memory/users/{user_id}/metrics", response_model=MemoryMetricsReport)
def get_memory_metrics(
    user_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryMetricsReport:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    return _memory_audit_service().metrics_for_identity(identity)


@router.get("/memory/users/{user_id}/confirmations", response_model=MemoryConfirmationList)
def list_memory_confirmations(
    user_id: str,
    include_resolved: bool = Query(default=False),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryConfirmationList:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    return _memory_audit_service().confirmations_for_identity(
        identity,
        include_resolved=include_resolved,
    )


@router.post("/memory/users/{user_id}/confirmations/{confirmation_id}/confirm", response_model=MemoryConfirmationResult)
def confirm_memory_confirmation(
    user_id: str,
    confirmation_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryConfirmationResult:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    try:
        result = _memory_audit_service().confirm_memory_for_identity(
            identity,
            confirmation_id=confirmation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="memory confirmation not found")
    return result


@router.post("/memory/users/{user_id}/confirmations/{confirmation_id}/reject", response_model=MemoryConfirmationResult)
def reject_memory_confirmation(
    user_id: str,
    confirmation_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryConfirmationResult:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    try:
        result = _memory_audit_service().reject_memory_for_identity(
            identity,
            confirmation_id=confirmation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="memory confirmation not found")
    return result


@router.get("/memory/users/{user_id}/profile/status", response_model=MemoryProfileRepairResult)
def get_memory_profile_status(
    user_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryProfileRepairResult:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    return _memory_audit_service().profile_status_for_identity(identity)


@router.post("/memory/users/{user_id}/profile/rebuild", response_model=MemoryProfileRepairResult)
def rebuild_memory_profile(
    user_id: str,
    dry_run: bool = Query(default=False),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryProfileRepairResult:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    return _memory_audit_service().rebuild_profile_for_identity(
        identity,
        dry_run=dry_run,
    )


@router.get("/memory/users/{user_id}/export", response_model=MemoryExport)
def export_memory(
    user_id: str,
    include_content: bool = Query(default=True),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryExport:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    result = _memory_audit_service().export_for_identity(
        identity,
        include_content=include_content,
    )
    _record_memory_control_plane_event(
        identity,
        action="export_memory",
        event_type="memory_export",
        detail={
            "include_content": include_content,
            "item_count": len(result.items),
        },
    )
    return result


@router.post("/memory/users/{user_id}/retention/sweep", response_model=MemoryRetentionSweepResult)
def sweep_expired_memory(
    user_id: str,
    hard_delete: bool = Query(default=False),
    dry_run: bool = Query(default=False),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryRetentionSweepResult:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    return _memory_audit_service().sweep_expired_for_identity(
        identity,
        hard_delete=hard_delete,
        dry_run=dry_run,
    )


@router.get("/memory/users/{user_id}/snapshot", response_model=MemorySnapshot)
def get_memory_snapshot(
    user_id: str,
    session_id: str | None = Query(default=None),
    query: str = Query(default=""),
    top_k: int = Query(default=5, ge=1, le=50),
    max_context_chars: int = Query(default=1000, ge=50, le=4000),
    include_content: bool = Query(default=False),
    include_superseded: bool = Query(default=False),
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemorySnapshot:
    identity = _require_trial_access_for_identity(
        _identity_from_user_id(user_id, session_id=session_id, source="path", auth_context=auth_context)
    )
    result = _memory_snapshot_service().snapshot_for_identity(
        identity,
        query=query,
        top_k=top_k,
        max_context_chars=max_context_chars,
        include_content=include_content,
        include_superseded=include_superseded,
    )
    _record_memory_control_plane_event(
        identity,
        action="memory_snapshot",
        event_type="memory_access",
        detail={
            "top_k": top_k,
            "max_context_chars": max_context_chars,
            "include_content": include_content,
            "include_superseded": include_superseded,
            "memory_context_total": result.memory_context.total,
            "memory_block_count": len(result.memory_context.blocks),
            "conversation_turn_count": result.conversation_history.total,
        },
    )
    return result


@router.delete("/memory/users/{user_id}/items/{memory_id}", response_model=MemoryDeleteResult)
def delete_memory_item(
    user_id: str,
    memory_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryDeleteResult:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    result = _memory_audit_service().delete_item_for_identity(
        identity,
        memory_id=memory_id,
    )
    if result.deleted.get("memory_items", 0) == 0:
        raise HTTPException(status_code=404, detail="memory item not found")
    _record_memory_control_plane_event(
        identity,
        action="delete_memory_item",
        event_type="memory_delete",
        detail={
            "memory_id": memory_id,
            "deleted": result.deleted,
        },
    )
    return result


@router.delete("/memory/users/{user_id}/sessions/{session_id}", response_model=MemoryDeleteResult)
def delete_memory_session(
    user_id: str,
    session_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> MemoryDeleteResult:
    identity = _require_trial_access_for_identity(
        _identity_from_user_id(user_id, session_id=session_id, source="path", auth_context=auth_context)
    )
    result = _memory_audit_service().delete_session_for_identity(
        identity,
    )
    _record_memory_control_plane_event(
        identity,
        action="delete_memory_session",
        event_type="memory_delete",
        detail={
            "session_id": session_id,
            "deleted": result.deleted,
        },
    )
    return result


@router.post("/beta/feedback", response_model=BetaFeedbackRecord)
def submit_beta_feedback(
    feedback: BetaFeedbackCreate,
    auth_context: AuthContext = Depends(get_auth_context),
) -> BetaFeedbackRecord:
    identity = _require_trial_access_for_identity(
        _identity_from_user_id(feedback.user_id, source="request_body", auth_context=auth_context)
    )
    _assert_run_belongs_to_user(feedback.run_id, identity.user_id)
    return get_beta_feedback_store().append(feedback)


@router.get("/beta/evaluations", response_model=BetaEvaluationExport)
def export_beta_evaluations(
    user_id: str | None = Query(default=None),
    auth_context: AuthContext = Depends(get_auth_context),
) -> BetaEvaluationExport:
    store = get_beta_feedback_store()
    identity = (
        _require_trial_access_for_identity(_identity_from_user_id(user_id, source="query", auth_context=auth_context))
        if user_id
        else None
    )
    records = store.list_by_user(identity.user_id) if identity is not None else store.read_all()
    trace_service = get_assistant_runtime_app().trace_query()
    items: list[BetaEvaluationItem] = []
    for record in records:
        summary = trace_service.run_summary(record.run_id)
        run_payload = summary.model_dump(mode="json") if summary is not None else {"run_id": record.run_id, "missing": True}
        items.append(BetaEvaluationItem(feedback=record, run=run_payload))
    return BetaEvaluationExport(
        user_id=identity.user_id if identity is not None else None,
        summary=summarize_feedback(records, user_id=identity.user_id if identity is not None else None),
        items=items,
    )


@router.delete("/beta/users/{user_id}/data")
def delete_beta_user_data(
    user_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    identity = _require_trial_access_for_identity(_identity_from_user_id(user_id, source="path", auth_context=auth_context))
    user_id = identity.user_id
    runtime_deleted = get_assistant_runtime_app().delete_user_runtime_data(user_id)
    feedback_deleted = get_beta_feedback_store().delete_by_user(user_id)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "user_id": user_id,
        "deleted": {
            "memory_items": runtime_deleted["memory_items"],
            "run_history_records": runtime_deleted["run_history_records"],
            "tool_history_records": runtime_deleted["tool_history_records"],
            "trace_events": runtime_deleted["trace_events"],
            "feedback_records": feedback_deleted,
            "conversation_sessions": runtime_deleted["conversation_sessions"],
            "session_records": runtime_deleted["session_records"],
        },
    }


def _assert_run_belongs_to_user(run_id: str, user_id: str) -> None:
    summary = get_assistant_runtime_app().trace_query().run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    if summary.user_id != user_id:
        raise HTTPException(status_code=403, detail="run does not belong to user")


def _memory_audit_service() -> MemoryAuditService:
    return get_assistant_runtime_app().memory_audit_service()


def _memory_snapshot_service() -> MemorySnapshotService:
    runtime = get_agent_runtime()
    conversation_store = get_default_conversation_store(runtime.config)
    return MemorySnapshotService(
        memory_manager=runtime.memory_manager,
        session_store=runtime.session_store,
        conversation_store=conversation_store,
        storage=MemoryStorageSnapshot(
            memory_store=type(runtime.memory_store).__name__,
            session_store=type(runtime.session_store).__name__,
            conversation_store=type(conversation_store).__name__,
            checkpointer=type(runtime.checkpointer).__name__ if runtime.checkpointer is not None else "none",
        ),
        config=runtime.config,
    )


def _control_plane_query_service() -> AgentControlPlaneQueryService:
    return AgentControlPlaneQueryService(
        trace_query=get_assistant_runtime_app().trace_query(),
        router_store=_control_plane_store(),
    )


def _control_plane_store():
    return getattr(get_agent_router(), "control_plane_store", None)


def _record_control_plane_audit_event(event: AgentAuditEvent) -> None:
    store = _control_plane_store()
    if store is not None:
        store.append_audit_event(event)


def _record_auth_audit_event(resolution: ResolvedRequestIdentity, *, action: str) -> None:
    _record_control_plane_audit_event(
        audit_event(
            event_type="auth_decision",
            component="api_identity",
            action=action,
            outcome="allowed" if resolution.auth_bound else "warning",
            user_id=resolution.identity.user_id,
            session_id=resolution.identity.session_id,
            detail=resolution.metadata(),
        )
    )


def _record_memory_control_plane_event(
    identity: RequestIdentity,
    *,
    action: str,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    _record_control_plane_audit_event(
        audit_event(
            event_type=event_type,
            component="memory_api",
            action=action,
            outcome="allowed",
            user_id=identity.user_id,
            session_id=identity.session_id,
            detail=detail,
        )
    )


def _identity_from_request(
    request: UserRequest,
    *,
    auth_context: AuthContext | None = None,
) -> ResolvedRequestIdentity:
    metadata = request.metadata or {}
    try:
        resolution = resolve_request_identity(
            user_id=request.user_id,
            session_id=request.session_id,
            source="request_body",
            tenant_id=_metadata_string(metadata, "tenant_id"),
            project_id=_metadata_string(metadata, "project_id"),
            auth_context=auth_context,
        )
        return _enforce_identity_policy(resolution)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _identity_from_user_id(
    user_id: str | None,
    *,
    session_id: str | None = None,
    source: ApiIdentitySource,
    auth_context: AuthContext | None = None,
) -> ResolvedRequestIdentity:
    try:
        resolution = resolve_request_identity(
            user_id=user_id or "",
            session_id=session_id,
            source=source,
            auth_context=auth_context,
        )
        return _enforce_identity_policy(resolution)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _enforce_identity_policy(resolution: ResolvedRequestIdentity) -> ResolvedRequestIdentity:
    try:
        enforce_identity_policy(
            resolution,
            production_required=require_auth_bound_identity(),
        )
    except IdentityPolicyError as exc:
        raise HTTPException(status_code=403, detail=exc.detail()) from exc
    return resolution


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _with_identity_metadata(request: UserRequest, resolution: ResolvedRequestIdentity):
    metadata = _public_request_metadata(request.metadata)
    metadata.setdefault("request_identity", resolution.metadata())
    return request.model_copy(
        update={
            "user_id": resolution.identity.user_id,
            "session_id": resolution.identity.session_id or request.session_id,
            "metadata": metadata,
        }
    )


def _public_request_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    safe_metadata = dict(metadata)
    for key in ("system_prompt_profile", "channel", "source"):
        safe_metadata.pop(key, None)
    return safe_metadata


def _require_trial_access_for_identity(resolution: ResolvedRequestIdentity) -> RequestIdentity:
    status = resolution.trial_access(get_trial_access_gate())
    if not status.allowed:
        raise HTTPException(status_code=403, detail=status.reason or "trial user is not allowed")
    return resolution.identity


def _require_trial_access(user_id: str) -> None:
    _require_trial_access_for_identity(_identity_from_user_id(user_id, source="local_context"))
