"""Workflow definition extension contract."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.workflows.models import (
    WorkflowPlanProposal,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
    WorkflowWorkItem,
)


class WorkflowDefinitionError(RuntimeError):
    pass


class DuplicateWorkflowDefinition(WorkflowDefinitionError):
    pass


class UnknownWorkflowDefinition(WorkflowDefinitionError):
    pass


class WorkflowDefinitionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    definition_version: str = Field(min_length=1, max_length=80)
    planner_display_title: str = Field(
        default="正在制定执行计划",
        min_length=1,
        max_length=160,
    )
    planner_objective: str = Field(
        default="根据工作流目标、交付物和约束生成可执行 DAG。",
        min_length=1,
        max_length=4_000,
    )


class WorkflowDefinition(Protocol):
    descriptor: WorkflowDefinitionDescriptor

    def validate_submission(self, submission: WorkflowSubmission) -> None: ...

    def materialize_plan(
        self,
        *,
        workflow: WorkflowRecord,
        proposal: WorkflowPlanProposal,
    ) -> WorkflowPlanVersion: ...


def build_bootstrap_plan(
    *,
    workflow_id: str,
    descriptor: WorkflowDefinitionDescriptor,
) -> WorkflowPlanVersion:
    """Build the sole admissible initial plan: one durable planner work item."""

    return WorkflowPlanVersion(
        workflow_id=workflow_id,
        version=1,
        definition_version=descriptor.definition_version,
        revision_reason="workflow_planner_pending",
        work_items=[
            WorkflowWorkItem(
                work_item_id="plan",
                kind="plan",
                display_title=descriptor.planner_display_title,
                objective=descriptor.planner_objective,
                acceptance_contract={"output_schema": "workflow_plan_v1"},
            )
        ],
    )


def materialize_work_items(
    proposal: WorkflowPlanProposal,
) -> list[WorkflowWorkItem]:
    return [
        WorkflowWorkItem(
            work_item_id=seed.seed_id,
            kind=seed.kind,
            display_title=seed.display_title,
            objective=seed.objective,
            depends_on=list(seed.depends_on),
            input_artifact_refs=list(seed.input_artifact_refs),
            acceptance_contract=dict(seed.acceptance_contract),
        )
        for seed in proposal.workstreams
    ]


class WorkflowDefinitionCatalog:
    def __init__(self, definitions: Iterable[WorkflowDefinition] = ()) -> None:
        self._definitions: dict[str, WorkflowDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: WorkflowDefinition) -> None:
        workflow_type = definition.descriptor.workflow_type
        if workflow_type in self._definitions:
            raise DuplicateWorkflowDefinition(workflow_type)
        self._definitions[workflow_type] = definition

    def require(self, workflow_type: str) -> WorkflowDefinition:
        try:
            return self._definitions[workflow_type]
        except KeyError as exc:
            raise UnknownWorkflowDefinition(workflow_type) from exc

    def list_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))
