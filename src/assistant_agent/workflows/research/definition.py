"""Deep Research as the first vertical WorkflowDefinition."""

from __future__ import annotations

from assistant_agent.workflows.definitions import WorkflowDefinitionDescriptor
from assistant_agent.workflows.models import (
    WorkflowPlanVersion,
    WorkflowSubmission,
    WorkflowWorkItem,
)


class DeepResearchWorkflowDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="deep_research",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        if submission.workflow_type != self.descriptor.workflow_type:
            raise ValueError("submission type does not match definition")
        questions = submission.inputs.get("research_questions", [])
        if not isinstance(questions, list) or any(
            not isinstance(item, str) or not item.strip() for item in questions
        ):
            raise ValueError("research_questions must be a list of non-empty strings")
        source_target = submission.inputs.get("source_target", 15)
        if not isinstance(source_target, int) or not 3 <= source_target <= 100:
            raise ValueError("source_target must be between 3 and 100")

    def build_initial_plan(
        self, *, workflow_id: str, submission: WorkflowSubmission
    ) -> WorkflowPlanVersion:
        source_target = int(submission.inputs.get("source_target", 15))
        questions = submission.inputs.get("research_questions", [])
        question_text = "；".join(str(item) for item in questions) or submission.objective
        work_items = [
            WorkflowWorkItem(
                work_item_id="scope",
                kind="scope",
                objective=f"界定研究范围、问题和排除项：{question_text}",
            ),
            WorkflowWorkItem(
                work_item_id="collect_sources",
                kind="collect_sources",
                objective=f"收集约 {source_target} 个可信且多样的来源，保存来源引用和摘要。",
                depends_on=["scope"],
                acceptance_contract={"minimum_sources": max(3, source_target // 2)},
            ),
            WorkflowWorkItem(
                work_item_id="extract_evidence",
                kind="extract_evidence",
                objective="从来源中提取可追溯证据、冲突与不确定性。",
                depends_on=["collect_sources"],
                acceptance_contract={"requires_source_refs": True},
            ),
            WorkflowWorkItem(
                work_item_id="outline",
                kind="outline",
                objective="根据问题和证据建立完整报告大纲。",
                depends_on=["extract_evidence"],
            ),
            WorkflowWorkItem(
                work_item_id="draft",
                kind="draft",
                objective="按大纲和证据撰写带引用的报告草稿。",
                depends_on=["outline"],
                acceptance_contract={"requires_citations": True},
            ),
            WorkflowWorkItem(
                work_item_id="verify",
                kind="verify",
                objective="验证交付物完整性、claim/evidence 对齐和引用覆盖。",
                depends_on=["draft"],
                acceptance_contract={"unresolved_claims": 0},
            ),
            WorkflowWorkItem(
                work_item_id="synthesize",
                kind="synthesize",
                objective="合成最终报告、执行摘要、限制和来源列表。",
                depends_on=["verify"],
            ),
        ]
        return WorkflowPlanVersion(
            workflow_id=workflow_id,
            version=1,
            definition_version=self.descriptor.definition_version,
            revision_reason="deep_research_initial",
            work_items=work_items,
        )
