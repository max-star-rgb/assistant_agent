"""Offline MCP skeleton for direct local Tool development."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.providers.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_operation_barrier import (
    new_nonresumable_operation_scope_id,
    normalized_tool_input_digest,
    stable_assistant_thread_id,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry


MCP_TOOL_NAMES = ("tool_list", "tool_run", "demo_flow_run")


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
        registry: ToolRegistry | None = None,
        config: ProviderConfig | None = None,
    ) -> None:
        self.config = config or ProviderConfig()
        self.registry = registry or create_default_registry(self.config)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP-visible tool definitions."""

        return [
            {
                "name": "tool_list",
                "description": (
                    "列出当前离线 MCP 服务自身的工具，以及本地 ToolRegistry 已注册的工具名"
                    "和 ToolSpec。只读，不执行任何业务工具。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                "offline": True,
            },
            {
                "name": "tool_run",
                "description": (
                    "按工具名和匹配其 schema 的输入，通过 Validator、Executor 与 Registry"
                    " 治理链执行一个已注册的 mock/local 工具；返回执行结果或结构化拒绝原因。"
                    "不能调用当前 Registry 中不存在或未获准的工具。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "ToolRegistry 中已注册的工具名。",
                        },
                        "input": {
                            "type": "object",
                            "description": "符合目标工具输入 schema 的参数对象。",
                        },
                    },
                    "required": ["tool_name"],
                },
                "offline": True,
            },
            {
                "name": "demo_flow_run",
                "description": (
                    "按可选 scenario_id 运行一个现有离线 demo 场景，并返回 demo flow 的"
                    "结构化摘要。仅用于本地演示，不代表真实 Provider 或外部服务结果。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "scenario_id": {
                            "type": "string",
                            "description": "可选的离线 demo 场景 ID。",
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
            if tool_name == "tool_list":
                return self._tool_list()
            if tool_name == "tool_run":
                return self._tool_run(args)
            if tool_name == "demo_flow_run":
                return self._demo_flow_run(args)
            return self._failed(tool_name, "mcp_tool_not_found", f"Unknown MCP tool: {tool_name}")
        except Exception as exc:  # pragma: no cover - defensive envelope guard
            return self._failed(tool_name, "mcp_tool_failed", exc)

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
            operation_scope_id=new_nonresumable_operation_scope_id(
                thread_id=stable_assistant_thread_id(
                    agent_id=state.agent_id,
                    user_id=state.user_id,
                    session_id=state.session_id,
                ),
                tool_name=name,
                normalized_input_digest=normalized_tool_input_digest(
                    validation.validated_input.model_dump(mode="json")
                ),
            ),
            operation_thread_id=stable_assistant_thread_id(
                agent_id=state.agent_id,
                user_id=state.user_id,
                session_id=state.session_id,
            ),
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
