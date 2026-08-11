"""Built-in generic Workflow definitions."""

from __future__ import annotations

from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
    materialize_work_items,
)
from assistant_agent.workflows.models import (
    WorkflowPlanProposal,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
)
from assistant_agent.workflows.constraints import resolve_constraint_bindings
from assistant_agent.workflows.research.definition import DeepResearchWorkflowDefinition


class LongHorizonWorkflowDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="long_horizon",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        if submission.workflow_type != self.descriptor.workflow_type:
            raise ValueError("submission type does not match definition")

    def materialize_plan(
        self,
        *,
        workflow: WorkflowRecord,
        proposal: WorkflowPlanProposal,
    ) -> WorkflowPlanVersion:
        work_items = materialize_work_items(proposal)
        constraint_bindings = resolve_constraint_bindings(
            constraints=workflow.constraints,
            work_items=work_items,
            proposal_bindings=proposal.constraint_bindings,
        )
        return WorkflowPlanVersion(
            workflow_id=workflow.workflow_id,
            version=workflow.current_plan_version + 1,
            definition_version=self.descriptor.definition_version,
            revision_reason="runtime_planner",
            work_items=work_items,
            constraint_bindings=constraint_bindings,
        )


def default_workflow_definitions() -> WorkflowDefinitionCatalog:
    return WorkflowDefinitionCatalog([
        LongHorizonWorkflowDefinition(),
        DeepResearchWorkflowDefinition(),
    ])
