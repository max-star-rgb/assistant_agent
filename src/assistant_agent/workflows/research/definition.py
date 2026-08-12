"""Deep Research as the first vertical WorkflowDefinition."""

from __future__ import annotations

from assistant_agent.workflows.definitions import (
    WorkflowDefinitionDescriptor,
    materialize_runtime_plan,
)
from assistant_agent.workflows.models import (
    WorkflowPlannerProposal,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
)


class DeepResearchWorkflowDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="deep_research",
        definition_version="3",
        planner_objective="为当前研究目标生成可执行 DAG、步骤验收契约和约束责任绑定。",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        if submission.workflow_type != self.descriptor.workflow_type:
            raise ValueError("submission type does not match definition")
        questions = submission.inputs.get("research_questions", [])
        if not isinstance(questions, list) or any(
            not isinstance(item, str) or not item.strip() for item in questions
        ):
            raise ValueError("research_questions must be a list of non-empty strings")

    def materialize_plan(
        self,
        *,
        workflow: WorkflowRecord,
        proposal: WorkflowPlannerProposal,
    ) -> WorkflowPlanVersion:
        return materialize_runtime_plan(
            workflow=workflow,
            proposal=proposal,
            definition_version=self.descriptor.definition_version,
        )
