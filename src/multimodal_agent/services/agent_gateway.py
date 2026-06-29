"""Optional local multi-agent gateway entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.agent.state import new_run_id
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentCommunicationError,
    AgentInstance,
)
from multimodal_agent.schemas.agent_gateway import (
    AgentCollaborationMode,
    AgentGatewayDelegatedTaskSummary,
    AgentGatewayRouteDecision,
    AgentGatewayRouteReason,
    AgentGatewayRunMetadata,
    AgentGatewayRunRequest,
)
from multimodal_agent.schemas.api import AgentRunResponse, PROTOCOL_VERSION, api_error
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.agent_communication import (
    AgentCommunicationService,
    create_local_agent_communication_service,
)
from multimodal_agent.services.agent_directory import AgentDirectory, default_agent_instance
from multimodal_agent.services.agent_routing_policy import AgentRoutingPolicy
from multimodal_agent.services.assistant_run_service import run_assistant_request, resolve_runtime_config
from multimodal_agent.services.trace_store import new_trace_id
from multimodal_agent.services.video_context import InMemoryVideoContextStore
from multimodal_agent.tools.registry import create_default_registry


WORKER_AGENT_ID = "agent.worker"


class AgentGateway:
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

    def run(self, request: AgentGatewayRunRequest | UserRequest) -> AgentRunResponse:
        """Run one request through the selected local agent."""

        gateway_request = _coerce_gateway_request(request)
        mode = gateway_request.effective_collaboration_mode()
        route_decision = self.routing_policy.resolve(
            gateway_request,
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
            return _failed_response(
                gateway_request,
                error=error,
                mode=mode,
                route_reason=route_decision.reason,
            )

        agent_id = route.instance.agent_id
        runtime = self.controller_runtime if route_decision.use_controller_runtime else self.runtimes.get(agent_id)
        if runtime is None:
            return _failed_response(
                gateway_request,
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

        runtime_request = gateway_request.to_user_request(
            metadata=_request_metadata(gateway_request, agent_id=agent_id, mode=mode)
        )
        response = run_assistant_request(runtime_request, runtime=runtime).api_response()
        return _augment_response(
            response,
            request=gateway_request,
            agent_id=agent_id,
            mode=mode,
            route_reason=route_decision.reason,
            route_instance=route.instance,
            runtime=runtime,
        )


def create_default_agent_gateway(
    *,
    config: ProviderConfig | None = None,
    load_env: bool = True,
    worker_agent_id: str = WORKER_AGENT_ID,
) -> AgentGateway:
    """Create the default offline/local gateway with one controller and one worker."""

    resolved_config = resolve_runtime_config(config=config, load_env=load_env)
    default_runtime = AgentGraphRuntime(config=resolved_config)
    worker_runtime = AgentGraphRuntime(config=resolved_config)
    instances = [
        default_agent_instance(),
        AgentInstance(
            agent_id=worker_agent_id,
            display_name="Worker Agent",
            description="Local same-process worker runtime for explicit gateway routing.",
            capabilities=["chat", "tool_calling"],
            transports=["local"],
            metadata={"worker": True, "offline": True, "local": True},
        ),
    ]
    communication_service = create_local_agent_communication_service(
        {worker_agent_id: worker_runtime},
        instances=instances,
    )
    controller_video_context = InMemoryVideoContextStore()
    controller_registry = create_default_registry(
        resolved_config,
        video_context_store=controller_video_context,
        enable_agent_delegation=True,
        agent_communication_service=communication_service,
    )
    controller_runtime = AgentGraphRuntime(
        config=resolved_config,
        registry=controller_registry,
        video_context_store=controller_video_context,
    )
    return AgentGateway(
        {
            DEFAULT_AGENT_ID: default_runtime,
            worker_agent_id: worker_runtime,
        },
        directory=communication_service.directory,
        communication_service=communication_service,
        controller_runtime=controller_runtime,
    )


def _coerce_gateway_request(request: AgentGatewayRunRequest | UserRequest) -> AgentGatewayRunRequest:
    if isinstance(request, AgentGatewayRunRequest):
        return request
    return AgentGatewayRunRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        text=request.text,
        image_ids=list(request.image_ids),
        video_ids=list(request.video_ids),
        audio_id=request.audio_id,
        execution_strategy=request.execution_strategy,
        metadata=dict(request.metadata),
    )


def _request_metadata(
    request: AgentGatewayRunRequest,
    *,
    agent_id: str,
    mode: AgentCollaborationMode,
) -> dict[str, Any]:
    metadata = dict(request.metadata)
    metadata["agent_gateway"] = {
        "agent_id": agent_id,
        "collaboration_mode": mode,
        "target_agent_id": request.target_agent_id,
        "capability": request.capability,
    }
    return metadata


def _augment_response(
    response: AgentRunResponse,
    *,
    request: AgentGatewayRunRequest,
    agent_id: str,
    mode: AgentCollaborationMode,
    route_reason: AgentGatewayRouteReason,
    route_instance: AgentInstance,
    runtime: Any,
) -> AgentRunResponse:
    delegation_enabled = "delegate_to_agent" in _runtime_tool_names(runtime)
    metadata = AgentGatewayRunMetadata(
        route_decision=AgentGatewayRouteDecision(
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
    gateway_info = metadata.public_payload()
    data = dict(response.data)
    data["agent_gateway"] = gateway_info
    runtime_info = dict(response.runtime_info)
    runtime_info["agent_gateway"] = gateway_info
    return response.model_copy(update={"data": data, "runtime_info": runtime_info}, deep=True)


def _failed_response(
    request: AgentGatewayRunRequest,
    *,
    error: AgentCommunicationError,
    mode: AgentCollaborationMode,
    route_reason: AgentGatewayRouteReason,
    agent_id: str | None = None,
) -> AgentRunResponse:
    metadata = AgentGatewayRunMetadata(
        route_decision=AgentGatewayRouteDecision(
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
    gateway_info = metadata.public_payload()
    return AgentRunResponse(
        protocol_version=PROTOCOL_VERSION,
        run_id=new_run_id(),
        trace_id=new_trace_id(),
        status="failed",
        intent=None,
        response_text=error.message,
        data={"agent_gateway": gateway_info},
        runtime_info={"agent_gateway": gateway_info},
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


def _delegated_task_summary(response: AgentRunResponse) -> list[AgentGatewayDelegatedTaskSummary]:
    tasks: list[AgentGatewayDelegatedTaskSummary] = []
    for result in response.tool_results:
        if result.get("tool_name") != "delegate_to_agent":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        errors = data.get("errors") if isinstance(data, dict) else []
        artifacts = data.get("artifacts") if isinstance(data, dict) else []
        tasks.append(
            AgentGatewayDelegatedTaskSummary(
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
