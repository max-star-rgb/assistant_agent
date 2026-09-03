"""Optional local multi-agent router entrypoint."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from assistant_agent.identifiers import new_run_id
from assistant_agent.multi_agent.models import (
    DEFAULT_AGENT_ID,
    AgentCommunicationError,
    AgentInstance,
)
from assistant_agent.multi_agent.router_models import (
    PROTOCOL_VERSION,
    AgentRunResponse,
    AgentCollaborationMode,
    AgentRouteDecision,
    AgentRouteDelegatedTaskSummary,
    AgentRouteMetadata,
    AgentRouteReason,
    AgentRouteRequest,
    api_error,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.multi_agent.agent_communication import (
    AgentCommunicationService,
    create_local_agent_communication_service,
)
from assistant_agent.multi_agent.agent_control_plane import (
    AgentControlPlaneStore,
    InMemoryAgentControlPlaneStore,
    audit_events_from_agent_router_record,
    build_agent_router_run_record,
)
from assistant_agent.multi_agent.agent_directory import AgentDirectory
from assistant_agent.multi_agent.agent_routing_policy import AgentRoutingPolicy
from assistant_agent.observability.trace_store import new_trace_id


WORKER_AGENT_ID = "agent.worker"
ROUTER_METADATA_KEY = "agent_router"


class AgentRouter:
    """Route inbound requests to local agent runtime instances."""

    def __init__(
        self,
        runtimes: Mapping[str, Any],
        *,
        directory: AgentDirectory | None = None,
        communication_service: AgentCommunicationService | None = None,
        controller_agent_id: str = DEFAULT_AGENT_ID,
        controller_runtime: Any | None = None,
        routing_policy: AgentRoutingPolicy | None = None,
        routing_table: Mapping[str, str] | None = None,
        control_plane_store: AgentControlPlaneStore | None = None,
    ) -> None:
        if not runtimes:
            raise ValueError("at least one agent runtime is required")
        if routing_policy is not None and routing_table:
            raise ValueError("routing_table cannot be provided with routing_policy")
        self.runtimes = dict(runtimes)
        self.directory = directory or create_local_agent_communication_service(self.runtimes).directory
        self.communication_service = communication_service
        self.controller_agent_id = controller_agent_id
        self.controller_runtime = controller_runtime or self.runtimes.get(controller_agent_id)
        self.routing_policy = routing_policy or AgentRoutingPolicy(
            controller_agent_id=controller_agent_id,
            routing_table=routing_table,
        )
        self.control_plane_store = control_plane_store or InMemoryAgentControlPlaneStore()

    def run(
        self,
        request: AgentRouteRequest | UserRequest,
        *,
        event_sink: Any | None = None,
        cancel_token: Any | None = None,
    ) -> AgentRunResponse:
        """Run one request through the selected local agent."""

        started_at = time.monotonic()
        route_request = _coerce_route_request(request)
        mode = route_request.effective_collaboration_mode()
        route_decision = self.routing_policy.resolve(
            route_request,
            directory=self.directory,
            source_agent_id=self.controller_agent_id,
        )
        route = route_decision.route
        if route.status != "routed" or route.instance is None:
            error = route.error or AgentCommunicationError(
                code="agent_route_failed",
                message="Agent route failed.",
                recoverable=True,
            )
            response = _failed_response(
                route_request,
                error=error,
                mode=mode,
                route_reason=route_decision.reason,
            )
            self._record_control_plane(route_request, response=response, started_at=started_at)
            return response

        agent_id = route.instance.agent_id
        runtime = self.controller_runtime if route_decision.use_controller_runtime else self.runtimes.get(agent_id)
        if runtime is None:
            response = _failed_response(
                route_request,
                error=AgentCommunicationError(
                    code="agent_runtime_not_found",
                    message=f"No local runtime registered for agent: {agent_id}",
                    detail={"agent_id": agent_id},
                    recoverable=True,
                ),
                mode=mode,
                agent_id=agent_id,
                route_reason=route_decision.reason,
            )
            self._record_control_plane(route_request, response=response, started_at=started_at)
            return response

        runtime_request = route_request.to_user_request(
            metadata=_request_metadata(route_request, agent_id=agent_id, mode=mode)
        )
        response = runtime.invoke(
            runtime_request,
            event_sink=event_sink,
            cancel_token=cancel_token,
        )
        if not isinstance(response, AgentRunResponse):
            raise TypeError("local agent invoker must return AgentRunResponse")
        response = _augment_response(
            response,
            request=route_request,
            agent_id=agent_id,
            mode=mode,
            route_reason=route_decision.reason,
            route_instance=route.instance,
            runtime=runtime,
        )
        self._record_control_plane(route_request, response=response, started_at=started_at)
        return response

    def _record_control_plane(
        self,
        request: AgentRouteRequest,
        *,
        response: AgentRunResponse,
        started_at: float,
    ) -> None:
        latency_ms = int((time.monotonic() - started_at) * 1000)
        record = build_agent_router_run_record(
            request=request,
            response=response,
            latency_ms=latency_ms,
        )
        self.control_plane_store.record(record)
        for event in audit_events_from_agent_router_record(record):
            self.control_plane_store.append_audit_event(event)


def _coerce_route_request(request: AgentRouteRequest | UserRequest) -> AgentRouteRequest:
    if isinstance(request, AgentRouteRequest):
        return request
    return AgentRouteRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        text=request.text,
        image_ids=list(request.image_ids),
        video_ids=list(request.video_ids),
        audio_id=request.audio_id,
        metadata=dict(request.metadata),
    )


def _request_metadata(
    request: AgentRouteRequest,
    *,
    agent_id: str,
    mode: AgentCollaborationMode,
) -> dict[str, Any]:
    metadata = dict(request.metadata)
    router_info = {
        "agent_id": agent_id,
        "collaboration_mode": mode,
        "target_agent_id": request.target_agent_id,
        "capability": request.capability,
    }
    metadata[ROUTER_METADATA_KEY] = router_info
    return metadata


def _augment_response(
    response: AgentRunResponse,
    *,
    request: AgentRouteRequest,
    agent_id: str,
    mode: AgentCollaborationMode,
    route_reason: AgentRouteReason,
    route_instance: AgentInstance,
    runtime: Any,
) -> AgentRunResponse:
    delegation_enabled = "delegate_to_agent" in _runtime_tool_names(runtime)
    metadata = AgentRouteMetadata(
        route_decision=AgentRouteDecision(
            selected_agent_id=agent_id,
            requested_target_agent_id=request.target_agent_id,
            requested_capability=request.capability,
            collaboration_mode=mode,
            reason=route_reason,
            status="routed",
            delegation_enabled=delegation_enabled,
        ),
        delegated_tasks=_delegated_task_summary(response),
        route=route_instance.model_dump(mode="json"),
    )
    router_info = metadata.public_payload()
    data = dict(response.data)
    data[ROUTER_METADATA_KEY] = router_info
    runtime_info = dict(response.runtime_info)
    runtime_info[ROUTER_METADATA_KEY] = router_info
    return response.model_copy(update={"data": data, "runtime_info": runtime_info}, deep=True)


def _failed_response(
    request: AgentRouteRequest,
    *,
    error: AgentCommunicationError,
    mode: AgentCollaborationMode,
    route_reason: AgentRouteReason,
    agent_id: str | None = None,
) -> AgentRunResponse:
    metadata = AgentRouteMetadata(
        route_decision=AgentRouteDecision(
            selected_agent_id=agent_id,
            requested_target_agent_id=request.target_agent_id,
            requested_capability=request.capability,
            collaboration_mode=mode,
            reason=route_reason,
            status="failed",
            error_code=error.code,
            error_message=error.message,
        ),
    )
    router_info = metadata.public_payload()
    return AgentRunResponse(
        protocol_version=PROTOCOL_VERSION,
        run_id=new_run_id(),
        trace_id=new_trace_id(),
        status="failed",
        intent=None,
        response_text=error.message,
        data={ROUTER_METADATA_KEY: router_info},
        runtime_info={ROUTER_METADATA_KEY: router_info},
        current_stage="failed",
        blocked_reason=error.message,
        errors=[
            api_error(
                error.code,
                error.message,
                detail=error.detail,
                recoverable=error.recoverable,
            )
        ],
    )


def _runtime_tool_names(runtime: Any) -> list[str]:
    registry = getattr(runtime, "registry", None)
    if registry is None or not hasattr(registry, "list"):
        return []
    try:
        names = registry.list()
    except Exception:  # pragma: no cover - defensive metadata boundary
        return []
    return [str(name) for name in names]


def _delegated_task_summary(response: AgentRunResponse) -> list[AgentRouteDelegatedTaskSummary]:
    tasks: list[AgentRouteDelegatedTaskSummary] = []
    for result in response.tool_results:
        if result.get("tool_name") != "delegate_to_agent":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        errors = data.get("errors") if isinstance(data, dict) else []
        artifacts = data.get("artifacts") if isinstance(data, dict) else []
        tasks.append(
            AgentRouteDelegatedTaskSummary(
                task_id=data.get("task_id"),
                target_agent_id=data.get("target_agent_id"),
                status=data.get("status"),
                run_id=data.get("run_id"),
                trace_id=data.get("trace_id"),
                artifact_count=len(artifacts) if isinstance(artifacts, list) else 0,
                error_codes=[
                    error.get("code")
                    for error in errors
                    if isinstance(error, dict) and error.get("code")
                ],
            )
        )
    return tasks
