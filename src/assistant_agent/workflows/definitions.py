"""Workflow definition extension contract."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.workflows.models import WorkflowPlanVersion, WorkflowSubmission


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


class WorkflowDefinition(Protocol):
    descriptor: WorkflowDefinitionDescriptor

    def validate_submission(self, submission: WorkflowSubmission) -> None: ...

    def build_initial_plan(
        self,
        *,
        workflow_id: str,
        submission: WorkflowSubmission,
    ) -> WorkflowPlanVersion: ...


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
