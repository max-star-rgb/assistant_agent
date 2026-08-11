"""Deep Research as the first vertical WorkflowDefinition."""

from __future__ import annotations

from assistant_agent.workflows.definitions import WorkflowDefinitionDescriptor
from assistant_agent.workflows.constraints import resolve_constraint_bindings
from assistant_agent.workflows.models import (
    WorkflowConstraintBinding,
    WorkflowPlanVersion,
    WorkflowSubmission,
    WorkflowWorkItem,
)


class DeepResearchWorkflowDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="deep_research",
        definition_version="2",
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
        if submission.initial_workstreams:
            work_items = [
                WorkflowWorkItem(
                    work_item_id=seed.seed_id,
                    kind=seed.kind,
                    display_title=seed.display_title,
                    objective=seed.objective,
                    depends_on=list(seed.depends_on),
                    input_artifact_refs=list(seed.input_artifact_refs),
                    acceptance_contract=dict(seed.acceptance_contract),
                )
                for seed in submission.initial_workstreams
            ]
            return WorkflowPlanVersion(
                workflow_id=workflow_id,
                version=1,
                definition_version=self.descriptor.definition_version,
                revision_reason="deep_research_agent_plan",
                work_items=work_items,
                constraint_bindings=resolve_constraint_bindings(
                    submission=submission,
                    work_items=work_items,
                ),
            )
        source_target = int(submission.inputs.get("source_target", 15))
        questions = submission.inputs.get("research_questions", [])
        question_text = "；".join(str(item) for item in questions) or submission.objective
        work_items = [
            WorkflowWorkItem(
                work_item_id="scope",
                kind="scope",
                display_title="正在界定研究范围与问题",
                objective=f"界定研究范围、问题和排除项：{question_text}",
            ),
            WorkflowWorkItem(
                work_item_id="collect_sources",
                kind="collect_sources",
                display_title="正在收集并核实可信来源",
                objective=(
                    f"收集约 {source_target} 个可信且多样的来源线索，"
                    "尽量在模型输出中保留来源信息和摘要。"
                ),
                depends_on=["scope"],
                acceptance_contract={
                    "target_sources": source_target,
                    "source_verification": "best_effort",
                },
            ),
            WorkflowWorkItem(
                work_item_id="extract_evidence",
                kind="extract_evidence",
                display_title="正在提取证据、冲突与不确定性",
                objective="从可用来源线索中提取证据、冲突与不确定性。",
                depends_on=["collect_sources"],
                acceptance_contract={"source_refs": "best_effort"},
            ),
            WorkflowWorkItem(
                work_item_id="outline",
                kind="outline",
                display_title="正在整理研究报告结构",
                objective="根据问题和证据建立完整报告大纲。",
                depends_on=["extract_evidence"],
            ),
            WorkflowWorkItem(
                work_item_id="draft",
                kind="draft",
                display_title="正在撰写带引用的研究报告",
                objective="按大纲和证据撰写带引用的报告草稿。",
                depends_on=["outline"],
                acceptance_contract={"citations": "best_effort"},
            ),
            WorkflowWorkItem(
                work_item_id="verify",
                kind="verify",
                display_title="正在核验引用与结论",
                objective="验证交付物完整性、claim/evidence 对齐和引用覆盖。",
                depends_on=["draft"],
                acceptance_contract={
                    "unresolved_claims_target": 0,
                    "verification": "best_effort",
                },
            ),
            WorkflowWorkItem(
                work_item_id="synthesize",
                kind="synthesize",
                display_title="正在生成最终报告与执行摘要",
                objective="合成最终报告、执行摘要、限制和来源列表。",
                depends_on=["verify"],
            ),
        ]
        definition_bindings = [
            WorkflowConstraintBinding(
                constraint_id="evidence-source-count",
                statement=(
                    f"已核验证据集合包含至少 {source_target} 个可信且多样的来源。"
                ),
                owner_work_item_ids=["collect_sources"],
                verifier_work_item_id="verify",
                severity="required",
            ),
            WorkflowConstraintBinding(
                constraint_id="final-source-count",
                statement=f"最终报告引用至少 {source_target} 个可信且多样的来源。",
                owner_work_item_ids=["synthesize"],
                verifier_work_item_id="synthesize",
                severity="required",
            ),
        ]
        return WorkflowPlanVersion(
            workflow_id=workflow_id,
            version=1,
            definition_version=self.descriptor.definition_version,
            revision_reason="deep_research_initial",
            work_items=work_items,
            constraint_bindings=resolve_constraint_bindings(
                submission=submission,
                work_items=work_items,
                definition_bindings=definition_bindings,
            ),
        )
