"""Native read-only Tools for progressively loading registered project Skills."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.skills.loading import (
    SkillDescriptor,
    default_repo_root,
    load_repo_skill_descriptors,
    read_registered_skill_reference,
    render_skill_guidance,
)
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import (
    builtin_tool_metadata,
    invoke_native_tool,
)
from assistant_agent.tools.plugins.builtin.skill_loading.models import (
    LoadSkillReferenceRequest,
    LoadSkillReferenceResult,
    LoadSkillRequest,
    LoadSkillResult,
)
from assistant_agent.tools.runtime import ToolContext, tool_context


MAX_SKILL_REFERENCE_CHARS = 20_000


def create_load_skill_tool(*, root: str | Path | None = None) -> BaseTool:
    """Create the native Tool that activates one model-invocable Skill."""

    resolved_root = Path(root).resolve() if root is not None else default_repo_root()

    @tool(LOAD_SKILL_TOOL_NAME, response_format="content_and_artifact")
    def load_skill(
        skill_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[a-z0-9][a-z0-9-]*$",
                description="已注册的内部工作流标识。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """加载一个已注册的内部工作流，不接受路径或未注册资源。"""

        request = LoadSkillRequest(skill_id=skill_id)
        return invoke_native_tool(
            LOAD_SKILL_TOOL_NAME,
            lambda: _execute_load_skill(resolved_root, request),
        )

    load_skill.metadata = builtin_tool_metadata("read")
    return load_skill


def create_load_skill_reference_tool(*, root: str | Path | None = None) -> BaseTool:
    """Create the native Tool that reads a reference granted by ``load_skill``."""

    resolved_root = Path(root).resolve() if root is not None else default_repo_root()

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
        """读取当前 run 已授予的 Skill reference，不接受路径或模型声明的 grant。"""

        request = LoadSkillReferenceRequest(
            skill_id=skill_id,
            reference_id=reference_id,
        )
        return invoke_native_tool(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            lambda: _execute_load_skill_reference(
                resolved_root,
                request,
                tool_context(runtime),
            ),
        )

    load_skill_reference.metadata = builtin_tool_metadata("read")
    return load_skill_reference


def _execute_load_skill(root: Path, input: LoadSkillRequest) -> ToolResult:
    descriptor = _descriptor(root, input.skill_id, model_invocable=True)
    if descriptor is None:
        return _failure(
            LOAD_SKILL_TOOL_NAME,
            "skill_not_found",
            "未找到已注册的内部工作流。",
        )
    content = render_skill_guidance(descriptor)
    result = LoadSkillResult(
        status="succeeded",
        skill_id=descriptor.name,
        content=content,
        reference_ids=list(descriptor.references),
        granted_tools=list(descriptor.governed_tools),
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
            "reference_ids": result.reference_ids,
            "granted_tools": result.granted_tools,
            "unavailable_tools": result.unavailable_tools,
        },
        trace_summary={
            "status": result.status,
            "skill_id": result.skill_id,
            "content_chars": len(result.content),
            "reference_count": len(result.reference_ids),
            "granted_tools": result.granted_tools,
            "unavailable_tools": result.unavailable_tools,
        },
    )


def _execute_load_skill_reference(
    root: Path,
    input: LoadSkillReferenceRequest,
    context: ToolContext,
) -> ToolResult:
    descriptor = _descriptor(root, input.skill_id)
    if descriptor is None:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_not_found",
            "未找到已注册的内部工作流。",
        )
    allowed_reference_ids = context.skill_reference_grants.get(descriptor.name, [])
    if input.reference_id not in allowed_reference_ids:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_reference_not_loaded",
            "该 reference 未由本次运行中成功的 load_skill 返回。",
        )
    if input.reference_id not in descriptor.references:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_reference_not_found",
            "未找到已注册的 Skill reference。",
        )
    content = read_registered_skill_reference(
        root,
        descriptor,
        input.reference_id,
        max_chars=MAX_SKILL_REFERENCE_CHARS,
    )
    if content is None:
        return _failure(
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "skill_reference_unavailable",
            "已注册的 Skill reference 当前不可读取。",
        )
    result = LoadSkillReferenceResult(
        status="succeeded",
        skill_id=descriptor.name,
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
            "summary": f"已加载 {descriptor.name} 的 reference：{input.reference_id}。",
            "skill_id": result.skill_id,
            "reference_id": result.reference_id,
        },
        trace_summary={
            "status": result.status,
            "skill_id": result.skill_id,
            "reference_id": result.reference_id,
            "content_chars": len(result.content),
        },
    )


def _descriptor(
    root: Path,
    skill_id: str,
    *,
    model_invocable: bool = False,
) -> SkillDescriptor | None:
    catalog = load_repo_skill_descriptors(root)
    return next(
        (
            descriptor
            for descriptor in catalog.descriptors
            if descriptor.name == skill_id
            and (
                not model_invocable
                or (
                    descriptor.activation == "model"
                    and not descriptor.disable_model_invocation
                )
            )
        ),
        None,
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


__all__ = [
    "create_load_skill_reference_tool",
    "create_load_skill_tool",
]
