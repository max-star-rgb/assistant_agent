"""Read-only Tools for progressively loading registered project Skills."""

from __future__ import annotations

from pathlib import Path

from assistant_agent.skills.loading import (
    SkillDescriptor,
    default_repo_root,
    load_repo_skill_descriptors,
    read_registered_skill_reference,
    render_skill_guidance,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.skill_loading.models import (
    LoadSkillReferenceRequest,
    LoadSkillReferenceResult,
    LoadSkillRequest,
    LoadSkillResult,
)


MAX_SKILL_REFERENCE_CHARS = 20_000


class LoadSkillTool(ToolBase):
    name = LOAD_SKILL_TOOL_NAME
    description = (
        "当当前任务符合 skill_index 中某张 Skill 卡片时，在调用其相关业务工具前按 "
        "skill_id 静默加载完整工作流正文；不要加载无关 Skill 或向用户播报加载过程。"
        "成功结果会返回可按需读取的 reference_ids；不接受路径或未注册资源。"
    )
    input_schema = LoadSkillRequest
    output_schema = LoadSkillResult
    category = "read"
    repeat_policy = "distinct_inputs"

    def __init__(self, *, root: str | Path | None = None) -> None:
        super().__init__()
        self.root = Path(root).resolve() if root is not None else default_repo_root()

    def _execute(self, input: LoadSkillRequest, context: ToolContext) -> ToolResult:
        descriptor = _descriptor(
            self.root,
            input.skill_id,
            model_invocable=True,
        )
        if descriptor is None:
            return _failure(
                self.name,
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
            tool_name=self.name,
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


class LoadSkillReferenceTool(ToolBase):
    name = LOAD_SKILL_REFERENCE_TOOL_NAME
    description = (
        "仅在已加载 Skill 的专项细节确有必要时，按 skill_id 和本轮 load_skill 实际返回的 "
        "reference_id 静默读取参考正文；不得猜测标识，也不接受路径或越权资源。"
    )
    input_schema = LoadSkillReferenceRequest
    output_schema = LoadSkillReferenceResult
    category = "read"
    repeat_policy = "distinct_inputs"

    def __init__(self, *, root: str | Path | None = None) -> None:
        super().__init__()
        self.root = Path(root).resolve() if root is not None else default_repo_root()

    def _execute(
        self,
        input: LoadSkillReferenceRequest,
        context: ToolContext,
    ) -> ToolResult:
        descriptor = _descriptor(self.root, input.skill_id)
        if descriptor is None:
            return _failure(
                self.name,
                "skill_not_found",
                "未找到已注册的内部工作流。",
            )
        allowed_reference_ids = context.skill_reference_grants.get(
            descriptor.name,
            [],
        )
        if input.reference_id not in allowed_reference_ids:
            return _failure(
                self.name,
                "skill_reference_not_loaded",
                "该 reference 未由本次运行中成功的 load_skill 返回。",
            )
        reference_path = descriptor.references.get(input.reference_id)
        if reference_path is None:
            return _failure(
                self.name,
                "skill_reference_not_found",
                "未找到已注册的 Skill reference。",
            )
        content = read_registered_skill_reference(
            self.root,
            descriptor,
            input.reference_id,
            max_chars=MAX_SKILL_REFERENCE_CHARS,
        )
        if content is None:
            return _failure(
                self.name,
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
            tool_name=self.name,
            success=True,
            data=data,
            model_observation={
                "status": result.status,
                "summary": (
                    f"已加载 {descriptor.name} 的 reference：{input.reference_id}。"
                ),
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
