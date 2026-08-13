"""Workflow definition extension contract."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.workflows.models import (
    WorkflowConstraintProposal,
    WorkflowDeliverableBinding,
    WorkflowPlannerProposal,
    WorkflowPlanProposal,
    WorkflowPlanV2Proposal,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
    WorkflowWorkItem,
)
from assistant_agent.workflows.constraints import resolve_constraint_bindings


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
        default="workflow.planner",
        min_length=1,
        max_length=160,
    )
    planner_objective: str = Field(
        default="根据工作流目标、交付物和约束生成可执行 DAG。",
        min_length=1,
        max_length=4_000,
    )


class WorkflowPlanMaterializationInput(BaseModel):
    """Minimum trusted facts needed to turn an untrusted proposal into a plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=512)
    current_plan_version: int = Field(ge=1)
    deliverables: tuple[str, ...] = Field(min_length=1, max_length=32)
    constraints: tuple[str, ...] = Field(default=(), max_length=64)


class WorkflowDefinition(Protocol):
    descriptor: WorkflowDefinitionDescriptor

    def validate_submission(self, submission: WorkflowSubmission) -> None: ...

    def materialize_plan(
        self,
        *,
        workflow: WorkflowRecord,
        proposal: WorkflowPlannerProposal,
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
                acceptance_contract={"output_schema": "workflow_plan_v2"},
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


def materialize_planner_proposal(
    proposal: WorkflowPlannerProposal,
    *,
    requested_deliverables: list[str],
) -> tuple[
    list[WorkflowWorkItem],
    list[WorkflowConstraintProposal],
    list[WorkflowDeliverableBinding],
]:
    """Normalize v1/v2 planner wire contracts into persisted generic models."""

    if isinstance(proposal, WorkflowPlanV2Proposal):
        proposed_deliverables = {
            item.deliverable: item for item in proposal.deliverable_bindings
        }
        if (
            len(requested_deliverables) != len(set(requested_deliverables))
            or set(proposed_deliverables) != set(requested_deliverables)
        ):
            raise ValueError("planner proposal has invalid deliverable coverage")
        work_items = [
            WorkflowWorkItem(
                work_item_id=node.node_id,
                kind="agent",
                display_title=node.display_title,
                objective=node.objective,
                depends_on=list(node.depends_on),
                acceptance_contract=node.acceptance_contract.model_copy(deep=True),
            )
            for node in proposal.nodes
        ]
        constraints = [
            WorkflowConstraintProposal(
                constraint_id=item.constraint_id,
                statement=item.statement,
                owner_work_item_ids=list(item.owner_node_ids),
                verifier_work_item_id=item.verifier_node_id,
                severity=item.severity,
            )
            for item in proposal.constraint_bindings
        ]
        deliverables = [
            WorkflowDeliverableBinding(
                deliverable=deliverable,
                producer_work_item_id=(
                    proposed_deliverables[deliverable].producer_node_id
                ),
            )
            for deliverable in requested_deliverables
        ]
        return work_items, constraints, deliverables

    work_items = materialize_work_items(proposal)
    dependency_ids = {
        dependency for item in work_items for dependency in item.depends_on
    }
    terminal_ids = [
        item.work_item_id
        for item in work_items
        if item.work_item_id not in dependency_ids
    ]
    fallback_producer_id = terminal_ids[-1]
    return (
        work_items,
        list(proposal.constraint_bindings),
        [
            WorkflowDeliverableBinding(
                deliverable=deliverable,
                producer_work_item_id=fallback_producer_id,
            )
            for deliverable in requested_deliverables
        ],
    )


def materialize_runtime_plan(
    *,
    workflow: WorkflowRecord | WorkflowPlanMaterializationInput,
    proposal: WorkflowPlannerProposal,
    definition_version: str,
) -> WorkflowPlanVersion:
    """Build one admitted-plan candidate without definition-specific node semantics."""

    work_items, proposal_constraints, deliverable_bindings = (
        materialize_planner_proposal(
            proposal,
            requested_deliverables=list(workflow.deliverables),
        )
    )
    if isinstance(proposal, WorkflowPlanV2Proposal):
        requested_statements = list(workflow.constraints)
        proposed_statements = [item.statement for item in proposal_constraints]
        if (
            len(requested_statements) != len(set(requested_statements))
            or len(proposed_statements) != len(set(proposed_statements))
            or not set(requested_statements).issubset(proposed_statements)
        ):
            raise ValueError("planner proposal has invalid constraint coverage")
        proposed_by_statement = {
            item.statement: item for item in proposal_constraints
        }
        if any(
            proposed_by_statement[statement].severity != "required"
            or proposed_by_statement[statement].verifier_work_item_id is None
            for statement in requested_statements
        ):
            raise ValueError("trusted constraint must be required and verified")
        prose_constraints: list[str] = []
    else:
        prose_constraints = list(workflow.constraints)
    return WorkflowPlanVersion(
        workflow_id=workflow.workflow_id,
        version=workflow.current_plan_version + 1,
        definition_version=definition_version,
        revision_reason="runtime_planner",
        work_items=work_items,
        constraint_bindings=resolve_constraint_bindings(
            constraints=prose_constraints,
            work_items=work_items,
            proposal_bindings=proposal_constraints,
        ),
        deliverable_bindings=deliverable_bindings,
    )


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
