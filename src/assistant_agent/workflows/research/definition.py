"""Deep Research as the first vertical WorkflowDefinition."""

from __future__ import annotations

from assistant_agent.workflows.definitions import (
    WorkflowDefinitionDescriptor,
    materialize_work_items,
)
from assistant_agent.workflows.constraints import resolve_constraint_bindings
from assistant_agent.workflows.models import (
    WorkflowConstraintProposal,
    WorkflowPlanProposal,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
    WorkflowWorkItem,
)


class DeepResearchWorkflowDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="deep_research",
        definition_version="3",
        planner_display_title="正在制定研究计划",
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
        source_target = submission.inputs.get("source_target", 15)
        if not isinstance(source_target, int) or not 3 <= source_target <= 100:
            raise ValueError("source_target must be between 3 and 100")

    def materialize_plan(
        self,
        *,
        workflow: WorkflowRecord,
        proposal: WorkflowPlanProposal,
    ) -> WorkflowPlanVersion:
        work_items = materialize_work_items(proposal)
        source_target = int(workflow.inputs.get("source_target", 15))
        definition_bindings = _source_constraint_bindings(
            work_items,
            source_target=source_target,
        )
        return WorkflowPlanVersion(
            workflow_id=workflow.workflow_id,
            version=workflow.current_plan_version + 1,
            definition_version=self.descriptor.definition_version,
            revision_reason="runtime_planner",
            work_items=work_items,
            constraint_bindings=resolve_constraint_bindings(
                constraints=workflow.constraints,
                work_items=work_items,
                proposal_bindings=proposal.constraint_bindings,
                definition_bindings=definition_bindings,
            ),
        )


_SOURCE_COLLECTION_KINDS = {
    "collect",
    "collect_sources",
    "research",
    "search",
    "source_collection",
    "web_research",
}
_EVIDENCE_AGGREGATION_KINDS = {"evidence", "extract_evidence"}
_FINAL_WORK_KINDS = {
    "compose",
    "deliver",
    "finalize",
    "report",
    "synthesize",
}


def _source_constraint_bindings(
    work_items: list[WorkflowWorkItem],
    *,
    source_target: int,
) -> list[WorkflowConstraintProposal]:
    terminal_ids = _terminal_ids(work_items)
    final_candidates = [
        item.work_item_id
        for item in work_items
        if item.work_item_id in terminal_ids and item.kind in _FINAL_WORK_KINDS
    ]
    final_id = (final_candidates or terminal_ids)[-1]
    source_collection_ids = [
        item.work_item_id
        for item in work_items
        if item.kind in _SOURCE_COLLECTION_KINDS
    ]
    aggregate_item = next(
        (item for item in work_items if item.kind in _EVIDENCE_AGGREGATION_KINDS),
        None,
    )
    verify_item = next(
        (item for item in work_items if item.kind == "verify"),
        None,
    )
    final_item = next(
        item for item in work_items if item.work_item_id == final_id
    )
    if aggregate_item is not None:
        evidence_owner_ids = [aggregate_item.work_item_id]
    elif len(source_collection_ids) == 1:
        evidence_owner_ids = source_collection_ids
    elif len(source_collection_ids) > 1:
        evidence_owner_ids = [(verify_item or final_item).work_item_id]
    else:
        evidence_owner_ids = list(
            (verify_item or final_item).depends_on
        )
    if not evidence_owner_ids:
        evidence_owner_ids = [final_id]
    return [
        WorkflowConstraintProposal(
            constraint_id="evidence-source-count",
            statement=(
                f"已核验证据集合包含至少 {source_target} 个可信且多样的来源。"
            ),
            owner_work_item_ids=evidence_owner_ids,
            severity="required",
        ),
        WorkflowConstraintProposal(
            constraint_id="final-source-count",
            statement=f"最终报告引用至少 {source_target} 个可信且多样的来源。",
            owner_work_item_ids=[final_id],
            verifier_work_item_id=final_id,
            severity="required",
        ),
    ]


def _terminal_ids(work_items: list[WorkflowWorkItem]) -> list[str]:
    dependency_ids = {
        dependency
        for item in work_items
        for dependency in item.depends_on
    }
    return [
        item.work_item_id
        for item in work_items
        if item.work_item_id not in dependency_ids
    ]
