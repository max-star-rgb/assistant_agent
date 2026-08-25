"""Native read-only Tools for progressively loading registered project Skills."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.skills.native import (
    list_skill_reference_ids,
    read_skill_content,
    read_skill_reference,
    skill_metadata_by_name,
)
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    invoke_native_tool,
)
from assistant_agent.tools.plugins.builtin.skill_loading.models import (
    LoadSkillReferenceRequest,
    LoadSkillReferenceResult,
    LoadSkillRequest,
    LoadSkillResult,
)
def create_load_skill_tool(
    *,
    backend: BackendProtocol,
    loaded_state_key: str = "loaded_skill_ids",
    reference_grants_state_key: str = "skill_reference_grants",
) -> BaseTool:
    """Create the native Tool that loads one model-invocable Skill."""

    @tool(LOAD_SKILL_TOOL_NAME)
    def load_skill(
        skill_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[a-z0-9][a-z0-9-]*$",
                description="与当前用户请求匹配的已注册专项指引标识。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> Command:
        """当当前用户请求符合可用专项指引时，按 skill_id 取得推进该请求所需的补充规则。调用前若生成用户可见文字，只自然说明正在推进的用户目标；不要把内部能力选择、指引获取、工具调用或其他准备机制本身当作进度内容。不要取得无关指引；成功结果会返回可按需读取的 reference_ids；不接受路径或未注册资源。"""

        content, artifact = invoke_native_tool(
            LOAD_SKILL_TOOL_NAME,
            lambda: _execute_load_skill(
                backend,
                LoadSkillRequest(skill_id=skill_id),
                runtime.state,
            ),
        )
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=content,
                        artifact=artifact,
                        name=LOAD_SKILL_TOOL_NAME,
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
                loaded_state_key: [skill_id],
                reference_grants_state_key: {
                    skill_id: list(artifact.get("reference_ids", ())),
                },
            }
        )

    return configure_builtin_tool(load_skill, "read")


def create_load_skill_reference_tool(
    *,
    backend: BackendProtocol,
    reference_grants_state_key: str = "skill_reference_grants",
) -> BaseTool:
    """Create the native Tool that reads a reference granted by ``load_skill``."""

    @tool(LOAD_SKILL_REFERENCE_TOOL_NAME, response_format="content_and_artifact")
    def load_skill_reference(
        skill_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[a-z0-9][a-z0-9-]*$",
                description="已注册的内部工作流标识。",
            ),
        ],
        reference_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[a-z0-9][a-z0-9-]*$",
                description="load_skill 返回的已注册 reference 标识。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """仅在已加载 Skill 的专项细节确有必要时，按 skill_id 和本轮 load_skill 实际返回的 reference_id 静默读取参考正文；不得猜测标识，也不接受路径或越权资源。"""

        return invoke_native_tool(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            lambda: _execute_load_skill_reference(
                backend,
                LoadSkillReferenceRequest(
                    skill_id=skill_id,
                    reference_id=reference_id,
                ),
                runtime.state,
                reference_grants_state_key,
            ),
        )

    return configure_builtin_tool(load_skill_reference, "read")


def _execute_load_skill(
    backend: BackendProtocol,
    input: LoadSkillRequest,
    state: Any,
) -> ToolResult:
    metadata = skill_metadata_by_name(state, input.skill_id)
    if metadata is None:
        return _failure(
            LOAD_SKILL_TOOL_NAME,
            "skill_not_found",
            "未找到已注册的内部工作流。",
        )
    content = read_skill_content(backend, metadata)
    if content is None:
        return _failure(
            LOAD_SKILL_TOOL_NAME,
            "skill_unavailable",
            "已注册的内部工作流当前不可读取。",
        )
    reference_ids = list_skill_reference_ids(backend, metadata)
    result = LoadSkillResult(
        status="succeeded",
        skill_id=metadata["name"],
        content=content,
        reference_ids=reference_ids,
    )
    data = result.model_dump(mode="json")
    return ToolResult(
        tool_name=LOAD_SKILL_TOOL_NAME,
        success=True,
        data=data,
        model_observation={
            "status": result.status,
            "summary": "内部工作流已加载。",
            "skill_id": result.skill_id,
            "content": result.content,
            "reference_ids": result.reference_ids,
        },
        trace_summary={
            "status": result.status,
            "skill_id": result.skill_id,
            "content_chars": len(result.content),
            "reference_count": len(result.reference_ids),
        },
    )


def _execute_load_skill_reference(
    backend: BackendProtocol,
    input: LoadSkillReferenceRequest,
    state: Any,
    reference_grants_state_key: str,
) -> ToolResult:
    metadata = skill_metadata_by_name(state, input.skill_id)
    if metadata is None:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_not_found",
            "未找到已注册的内部工作流。",
        )
    allowed_reference_ids = _reference_grants(
        state,
        reference_grants_state_key,
    ).get(metadata["name"], [])
    if input.reference_id not in allowed_reference_ids:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_reference_not_loaded",
            "该 reference 未由本次运行中成功的 load_skill 返回。",
        )
    content = read_skill_reference(
        backend,
        metadata,
        input.reference_id,
    )
    if content is None:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_reference_unavailable",
            "已注册的 Skill reference 当前不可读取。",
        )
    result = LoadSkillReferenceResult(
        status="succeeded",
        skill_id=metadata["name"],
        reference_id=input.reference_id,
        content=content,
    )
    data = result.model_dump(mode="json")
    return ToolResult(
        tool_name=LOAD_SKILL_REFERENCE_TOOL_NAME,
        success=True,
        data=data,
        model_observation={
            "status": result.status,
            "summary": f"已加载 {metadata['name']} 的 reference：{input.reference_id}。",
            "skill_id": result.skill_id,
            "reference_id": result.reference_id,
            "content": result.content,
        },
        trace_summary={
            "status": result.status,
            "skill_id": result.skill_id,
            "reference_id": result.reference_id,
            "content_chars": len(result.content),
        },
    )


def _failure(tool_name: str, code: str, message: str) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error=code,
        model_observation={
            "status": "failed",
            "summary": message,
            "errors": [
                {
                    "code": code,
                    "message": message,
                    "recoverable": False,
                }
            ],
        },
        trace_summary={
            "status": "failed",
            "error_code": code,
        },
    )


def _reference_grants(
    state: object,
    state_key: str,
) -> dict[str, list[str]]:
    if not isinstance(state, Mapping):
        return {}
    raw = state.get(state_key)
    if not isinstance(raw, Mapping):
        return {}
    return {
        skill_id: [
            reference_id
            for reference_id in reference_ids
            if isinstance(reference_id, str) and reference_id
        ]
        for skill_id, reference_ids in raw.items()
        if isinstance(skill_id, str)
        and skill_id
        and isinstance(reference_ids, (list, tuple))
    }


__all__ = [
    "create_load_skill_reference_tool",
    "create_load_skill_tool",
]
