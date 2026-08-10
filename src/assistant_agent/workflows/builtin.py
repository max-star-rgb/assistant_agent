"""Built-in generic Workflow definitions."""

from __future__ import annotations

from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
)
from assistant_agent.workflows.models import (
    WorkflowPlanVersion,
    WorkflowSubmission,
    WorkflowWorkItem,
)
from assistant_agent.workflows.research.definition import DeepResearchWorkflowDefinition


class LongHorizonWorkflowDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="long_horizon",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        if submission.workflow_type != self.descriptor.workflow_type:
            raise ValueError("submission type does not match definition")

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
        else:
            work_items = [
                WorkflowWorkItem(
                    work_item_id="analyze",
                    kind="analyze",
                    display_title="正在分析任务目标与约束",
                    objective=f"分析目标、交付物和约束：{submission.objective}",
                ),
                WorkflowWorkItem(
                    work_item_id="execute",
                    kind="execute",
                    display_title="正在执行主要任务",
                    objective=f"完成主要工作：{submission.objective}",
                    depends_on=["analyze"],
                ),
                WorkflowWorkItem(
                    work_item_id="verify",
                    kind="verify",
                    display_title="正在核验交付结果",
                    objective="根据交付物和约束验证结果，列出仍存在的缺口。",
                    depends_on=["execute"],
                ),
                WorkflowWorkItem(
                    work_item_id="deliver",
                    kind="deliver",
                    display_title="正在整理最终交付物",
                    objective="合成最终交付物，并明确限制和未决项。",
                    depends_on=["verify"],
                ),
            ]
        return WorkflowPlanVersion(
            workflow_id=workflow_id,
            version=1,
            definition_version=self.descriptor.definition_version,
            revision_reason="initial_submission",
            work_items=work_items,
        )


def default_workflow_definitions() -> WorkflowDefinitionCatalog:
    return WorkflowDefinitionCatalog([
        LongHorizonWorkflowDefinition(),
        DeepResearchWorkflowDefinition(),
    ])
