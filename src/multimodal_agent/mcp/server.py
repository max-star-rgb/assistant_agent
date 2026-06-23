"""Offline MCP skeleton backed by the existing agent runtime and tool registry."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from multimodal_agent.tools.registry import ToolRegistry, create_default_registry


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
                "input_schema": {
                    "fields": {
                        "user_id": {"type": "string", "description": "User id for isolation.", "required": False},
                        "session_id": {"type": "string", "description": "Session id for conversation state.", "required": False},
                        "text": {"type": "string", "description": "User request text.", "required": False},
                        "image_ids": {"type": "array", "description": "Optional image references.", "required": False},
                        "video_ids": {"type": "array", "description": "Optional video references.", "required": False},
                        "metadata": {"type": "object", "description": "Optional request metadata.", "required": False},
                    }
                },
                "offline": True,
            },
            {
                "name": "tool_list",
                "description": "List registered local ToolRegistry tools.",
                "input_schema": {"fields": {}},
                "offline": True,
            },
            {
                "name": "tool_run",
                "description": "Run one registered mock/local ToolRegistry tool.",
                "input_schema": {
                    "fields": {
                        "tool_name": {"type": "string", "description": "Registered ToolRegistry tool name.", "required": True},
                        "input": {"type": "object", "description": "Tool input matching the registry tool schema.", "required": False},
                    }
                },
                "offline": True,
            },
            {
                "name": "demo_flow_run",
                "description": "Run one offline demo scenario through existing demo flow logic.",
                "input_schema": {
                    "fields": {
                        "scenario_id": {"type": "string", "description": "Optional demo scenario id.", "required": False},
                    }
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
        result = self.registry.run(name, dict(args.get("input") or {}))
        payload = result.model_dump(mode="json")
        status = "succeeded" if result.success else "failed"
        errors = [] if result.success else [{"code": "tool_failed", "message": sanitize_error_message(result.error)}]
        return MCPToolEnvelope(
            status=status,
            tool="tool_run",
            data=_sanitize_payload(payload),
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
