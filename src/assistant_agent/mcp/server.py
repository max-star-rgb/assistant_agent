"""Offline MCP skeleton backed by the existing agent runtime and tool registry."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


MCP_TOOL_NAMES = ("agent_run", "tool_list", "tool_run", "demo_flow_run")


class MCPToolEnvelope(BaseModel):
    """Stable envelope returned by offline MCP tool calls."""

    status: str = Field(pattern="^(succeeded|failed)$")
    tool: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=lambda: {"offline": True})


class OfflineMCPServer:
    """Small in-process MCP skeleton for local smoke tests and packaging."""

    def __init__(
        self,
        runtime: AgentGraphRuntime | None = None,
        registry: ToolRegistry | None = None,
        config: ProviderConfig | None = None,
    ) -> None:
        self.config = config or ProviderConfig()
        self.registry = registry or create_default_registry(self.config)
        self.runtime = runtime or AgentGraphRuntime(config=self.config, registry=self.registry)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP-visible tool definitions."""

        return [
            {
                "name": "agent_run",
                "description": "Run AgentGraphRuntime with mock/local defaults.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "User id for isolation."},
                        "session_id": {"type": "string", "description": "Session id for conversation state."},
                        "text": {"type": "string", "description": "User request text."},
                        "image_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional image references.",
                        },
                        "video_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional video references.",
                        },
                        "metadata": {"type": "object", "description": "Optional request metadata."},
                    },
                    "required": [],
                },
                "offline": True,
            },
            {
                "name": "tool_list",
                "description": "List registered local ToolRegistry tools.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                "offline": True,
            },
            {
                "name": "tool_run",
                "description": "Run one registered mock/local ToolRegistry tool.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "Registered ToolRegistry tool name.",
                        },
                        "input": {
                            "type": "object",
                            "description": "Tool input matching the registry tool schema.",
                        },
                    },
                    "required": ["tool_name"],
                },
                "offline": True,
            },
            {
                "name": "demo_flow_run",
                "description": "Run one offline demo scenario through existing demo flow logic.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "scenario_id": {
                            "type": "string",
                            "description": "Optional demo scenario id.",
                        },
                    },
                    "required": [],
                },
                "offline": True,
            },
        ]

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> MCPToolEnvelope:
        """Call one offline MCP tool and return a sanitized envelope."""

        args = arguments or {}
        try:
            if tool_name == "agent_run":
                return self._agent_run(args)
            if tool_name == "tool_list":
                return self._tool_list()
            if tool_name == "tool_run":
                return self._tool_run(args)
            if tool_name == "demo_flow_run":
                return self._demo_flow_run(args)
            return self._failed(tool_name, "mcp_tool_not_found", f"Unknown MCP tool: {tool_name}")
        except Exception as exc:  # pragma: no cover - defensive envelope guard
            return self._failed(tool_name, "mcp_tool_failed", exc)

    def _agent_run(self, args: dict[str, Any]) -> MCPToolEnvelope:
        request = UserRequest(
            user_id=str(args.get("user_id") or "mcp_user"),
            session_id=str(args.get("session_id") or "mcp_session"),
            text=args.get("text"),
            image_ids=list(args.get("image_ids") or []),
            video_ids=list(args.get("video_ids") or []),
            audio_id=args.get("audio_id"),
            metadata=dict(args.get("metadata") or {}),
        )
        state = self.runtime.run_state(request)
        response = state.response
        return self._succeeded(
            "agent_run",
            {
                "status": state.status,
                "intent": state.intent.intent if state.intent else None,
                "response_text": response.message if response else "",
                "tool_sequence": [call.tool_name for call in state.tool_calls],
                "run_id": state.run_id,
                "trace_id": state.trace_id,
                "output_refs": response.output_refs if response else [],
                "errors": [
                    {
                        "source": error.source,
                        "message": sanitize_error_message(error.message),
                        "details": sanitize_error_detail(error.details),
                    }
                    for error in state.errors
                ],
            },
        )

    def _tool_list(self) -> MCPToolEnvelope:
        return self._succeeded(
            "tool_list",
            {
                "mcp_tools": self.list_tools(),
                "registry_tools": self.registry.list(),
                "registry_tool_specs": [spec.model_dump(mode="json") for spec in self.registry.list_specs()],
            },
        )

    def _tool_run(self, args: dict[str, Any]) -> MCPToolEnvelope:
        name = str(args.get("tool_name") or "")
        if not name:
            return self._failed("tool_run", "mcp_missing_tool_name", "tool_name is required")
        request = UserRequest(
            user_id=str(args.get("user_id") or "mcp_user"),
            session_id=str(args.get("session_id") or "mcp_session"),
            text=args.get("text") or f"MCP tool_run: {name}",
            image_ids=list(args.get("image_ids") or []),
            video_ids=list(args.get("video_ids") or []),
            metadata=dict(args.get("metadata") or {}),
        )
        state = AgentState.from_request(request)
        decision = AssistantDecision(type="tool_call", tool_name=name, tool_input=dict(args.get("input") or {}))
        validation = ActionValidator().validate(
            decision=decision,
            registry=self.registry,
            request=request,
            state=state,
        )
        if not validation.accepted:
            return MCPToolEnvelope(
                status="failed",
                tool="tool_run",
                data={
                    "validator_result": validation.model_dump(mode="json"),
                    "run_id": state.run_id,
                    "trace_id": state.trace_id,
                },
                errors=[{"code": validation.code, "message": sanitize_error_message(validation.message)}],
                metadata={"offline": True, "registry_tool": name},
            )
        result = ToolExecutor(
            registry=self.registry,
        ).run_tool(
            state,
            "mcp_tool_run",
            name,
            dict(args.get("input") or {}),
            trace_store=self.runtime.trace_store,
            trace_id=state.trace_id,
            node_name="mcp_tool_run",
        )
        payload = result.model_dump(mode="json")
        status = "succeeded" if result.success else "failed"
        errors = [] if result.success else [{"code": "tool_failed", "message": sanitize_error_message(result.error)}]
        return MCPToolEnvelope(
            status=status,
            tool="tool_run",
            data=_sanitize_payload(
                {
                    **payload,
                    "run_id": state.run_id,
                    "trace_id": state.trace_id,
                }
            ),
            errors=errors,
            metadata={"offline": True, "registry_tool": name},
        )

    def _demo_flow_run(self, args: dict[str, Any]) -> MCPToolEnvelope:
        from scripts.run_demo_flows import run_demo_flows

        scenario_id = args.get("scenario_id")
        summary = run_demo_flows(str(scenario_id) if scenario_id else None)
        status = "succeeded" if summary.get("failed") == 0 else "failed"
        return MCPToolEnvelope(status=status, tool="demo_flow_run", data=_sanitize_payload(summary), metadata={"offline": True})

    def _succeeded(self, tool_name: str, data: dict[str, Any]) -> MCPToolEnvelope:
        return MCPToolEnvelope(status="succeeded", tool=tool_name, data=_sanitize_payload(data), metadata={"offline": True})

    def _failed(self, tool_name: str, code: str, message: object) -> MCPToolEnvelope:
        return MCPToolEnvelope(
            status="failed",
            tool=tool_name,
            errors=[{"code": code, "message": sanitize_error_message(message), "recoverable": False}],
            metadata={"offline": True},
        )


def _sanitize_payload(value: Any) -> Any:
    return sanitize_error_detail(value)
