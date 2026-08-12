"""Built-in generic Workflow definitions."""

from __future__ import annotations

from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
    materialize_runtime_plan,
)
from assistant_agent.workflows.models import (
    WorkflowPlannerProposal,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
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


def default_workflow_definitions() -> WorkflowDefinitionCatalog:
    return WorkflowDefinitionCatalog([
        LongHorizonWorkflowDefinition(),
        DeepResearchWorkflowDefinition(),
    ])
