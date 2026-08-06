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
        "读取“可用 Skill”索引对应的完整项目工作流；适用条件与当前任务相符时调用，"
        "即使任务简单或预计只需一个业务工具也不跳过。结果会列出可按需读取的 reference_ids。"
    )
    input_schema = LoadSkillRequest
    output_schema = LoadSkillResult
    category = "read"
    repeat_policy = "distinct_inputs"

    def __init__(self, *, root: str | Path | None = None) -> None:
        self.root = Path(root).resolve() if root is not None else default_repo_root()

    def _run(self, input: LoadSkillRequest, context: ToolContext) -> ToolResult:
        descriptor = _descriptor(self.root, input.skill_id)
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
            },
            trace_summary={
                "status": result.status,
                "skill_id": result.skill_id,
                "content_chars": len(result.content),
                "reference_count": len(result.reference_ids),
            },
        )


class LoadSkillReferenceTool(ToolBase):
    name = LOAD_SKILL_REFERENCE_TOOL_NAME
    description = (
        "读取 load_skill 已返回的专项参考；仅使用其 reference_ids 中的标识，"
        "且只在完整工作流需要额外细节时调用，不接受文件路径。"
    )
    input_schema = LoadSkillReferenceRequest
    output_schema = LoadSkillReferenceResult
    category = "read"
    repeat_policy = "distinct_inputs"

    def __init__(self, *, root: str | Path | None = None) -> None:
        self.root = Path(root).resolve() if root is not None else default_repo_root()

    def _run(
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
                    f"已加载 {descriptor.name} 的 reference："
                    f"{input.reference_id}。"
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


def _descriptor(root: Path, skill_id: str) -> SkillDescriptor | None:
    catalog = load_repo_skill_descriptors(root)
    return next(
        (
            descriptor
            for descriptor in catalog.descriptors
            if descriptor.name == skill_id
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
