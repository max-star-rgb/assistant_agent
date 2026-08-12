"""Offline MCP skeleton backed by the existing agent runtime and tool registry."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.runtime import AgentGraphRuntime
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
                "description": (
                    "使用 mock/local 配置运行一次完整 AgentGraphRuntime 请求；可携带文本和"
                    "媒体引用，返回运行状态、回复、工具序列、标识及已清理错误。仅用于离线"
                    "开发与验证，不调用真实 Provider。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "用于身份隔离的用户 ID。"},
                        "session_id": {"type": "string", "description": "用于关联对话状态的会话 ID。"},
                        "text": {"type": "string", "description": "用户请求文本。"},
                        "image_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选图片引用列表。",
                        },
                        "video_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选视频引用列表。",
                        },
                        "metadata": {"type": "object", "description": "可选请求元数据。"},
                    },
                    "required": [],
                },
                "offline": True,
            },
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
