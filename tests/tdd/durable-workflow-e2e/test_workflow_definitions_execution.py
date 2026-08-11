from __future__ import annotations

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.agent_runtime import AgentWorkItemResult
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.constraints import assigned_constraints
from assistant_agent.workflows.definitions import WorkflowDefinitionCatalog
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.models import WorkflowPlanProposal, WorkflowSubmission
from assistant_agent.workflows.research.definition import DeepResearchWorkflowDefinition
from assistant_agent.workflows.runtime import WorkItemAssignment
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import InMemoryWorkflowStore
from assistant_agent.workflows.transitions import validate_plan_dag


class CapturingAgentRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run_work_item(self, request) -> AgentWorkItemResult:
        self.requests.append(request)
        return AgentWorkItemResult(
            status="succeeded",
            run_id="run-work-item-sentinel",
            summary="work-item-output-sentinel",
        )


class LongResultAgentRuntime:
    def __init__(self, content: str) -> None:
        self.content = content

    def run_work_item(self, request) -> AgentWorkItemResult:
        return AgentWorkItemResult(
            status="succeeded",
            run_id="run-work-item-sentinel",
            summary=self.content[:4_000],
            content=self.content,
        )


def _submitted_research_workflow():
    definition = DeepResearchWorkflowDefinition()
    submission = WorkflowSubmission(
        workflow_type="deep_research",
        objective="research-objective-sentinel",
        deliverables=["report-sentinel"],
        inputs={"research_questions": ["question-sentinel"], "source_target": 12},
        durability_reasons=["multi_stage", "many_sources"],
        idempotency_key="submission-sentinel",
    )
    service = WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=WorkflowDefinitionCatalog([definition]),
    )
    bundle = service.submit(
        identity=RequestIdentity.for_user(
            user_id="user-sentinel",
            agent_id="agent-sentinel",
            session_id="session-sentinel",
        ),
        ingress_run_id="run-sentinel",
        submission=submission,
    )
    return definition, bundle


def test_deep_research_is_a_definition_not_a_special_runtime() -> None:
    definition, bundle = _submitted_research_workflow()

    assert WorkflowDefinitionCatalog([definition]).list_types() == ("deep_research",)
    assert bundle.workflow.phase == "planning"
    assert [item.kind for item in bundle.current_plan.work_items] == ["plan"]


def test_deep_research_uses_agent_planned_workstreams_and_display_titles() -> None:
    definition, bundle = _submitted_research_workflow()
    proposal = WorkflowPlanProposal(
        workstreams=[
            {
                "seed_id": "research-hermes",
                "kind": "collect_sources",
                "display_title": "正在检索并核实 Hermes 工程资料",
                "objective": "收集 Hermes 官方文档和一手工程资料。",
            },
            {
                "seed_id": "synthesize",
                "kind": "synthesize",
                "display_title": "正在撰写跨框架对比报告",
                "objective": "综合证据并形成最终报告。",
                "depends_on": ["research-hermes"],
            },
        ],
        constraint_bindings=[
            {
                "constraint_id": "source-count",
                "statement": "最终报告至少引用 15 个来源",
                "owner_work_item_ids": ["research-hermes", "synthesize"],
                "verifier_work_item_id": "synthesize",
                "severity": "required",
            }
        ],
    )

    plan = definition.materialize_plan(
        workflow=bundle.workflow,
        proposal=proposal,
    )

    assert [item.work_item_id for item in plan.work_items] == [
        "research-hermes",
        "synthesize",
    ]
    assert plan.work_items[0].display_title == "正在检索并核实 Hermes 工程资料"
    assert plan.work_items[1].depends_on == ["research-hermes"]
    planner_binding = next(
        item for item in plan.constraint_bindings if item.constraint_id == "source-count"
    )
    assert planner_binding.owner_work_item_ids == [
        "research-hermes",
        "synthesize",
    ]


def test_deep_research_infers_missing_required_constraint_verifier_from_dag() -> None:
    definition, bundle = _submitted_research_workflow()
    proposal = WorkflowPlanProposal(
        workstreams=[
            {
                "seed_id": "collect",
                "kind": "collect_sources",
                "display_title": "正在收集资料",
                "objective": "collect-sentinel",
            },
            {
                "seed_id": "verify",
                "kind": "verify",
                "display_title": "正在核验资料",
                "objective": "verify-sentinel",
                "depends_on": ["collect"],
            },
            {
                "seed_id": "synthesize",
                "kind": "synthesize",
                "display_title": "正在综合结果",
                "objective": "synthesize-sentinel",
                "depends_on": ["verify"],
            },
        ],
        constraint_bindings=[
            {
                "constraint_id": "source-count",
                "statement": "final-source-count-sentinel",
                "owner_work_item_ids": ["collect"],
                "severity": "required",
            }
        ],
    )

    plan = definition.materialize_plan(
        workflow=bundle.workflow,
        proposal=proposal,
    )

    planner_binding = next(
        item for item in plan.constraint_bindings if item.constraint_id == "source-count"
    )
    assert planner_binding.verifier_work_item_id == "verify"


def test_deep_research_materializes_source_target_as_scoped_bindings() -> None:
    definition, bundle = _submitted_research_workflow()
    proposal = WorkflowPlanProposal(workstreams=[
        {
            "seed_id": "scope",
            "kind": "scope",
            "display_title": "正在界定范围",
            "objective": "scope-sentinel",
        },
        {
            "seed_id": "collect-hermes",
            "kind": "collect_sources",
            "display_title": "正在研究 Hermes",
            "objective": "collect-hermes-sentinel",
            "depends_on": ["scope"],
        },
        {
            "seed_id": "collect-openclaw",
            "kind": "research",
            "display_title": "正在研究 OpenClaw",
            "objective": "collect-openclaw-sentinel",
            "depends_on": ["scope"],
        },
        {
            "seed_id": "verify",
            "kind": "verify",
            "display_title": "正在核验证据",
            "objective": "verify-sentinel",
            "depends_on": ["collect-hermes", "collect-openclaw"],
        },
        {
            "seed_id": "synthesize",
            "kind": "synthesize",
            "display_title": "正在生成报告",
            "objective": "synthesize-sentinel",
            "depends_on": ["verify"],
        },
    ])

    plan = definition.materialize_plan(
        workflow=bundle.workflow,
        proposal=proposal,
    )
    validate_plan_dag(plan, max_work_items=20)

    evidence = next(
        item
        for item in plan.constraint_bindings
        if item.constraint_id == "evidence-source-count"
    )
    final = next(
        item
        for item in plan.constraint_bindings
        if item.constraint_id == "final-source-count"
    )
    assert evidence.statement == "已核验证据集合包含至少 12 个可信且多样的来源。"
    assert evidence.owner_work_item_ids == ["verify"]
    assert evidence.verifier_work_item_id == "verify"
    assert final.owner_work_item_ids == ["synthesize"]
    assert final.verifier_work_item_id == "synthesize"
    assert assigned_constraints(
        plan.constraint_bindings,
        work_item_id="scope",
    ) == []
    assert assigned_constraints(
        plan.constraint_bindings,
        work_item_id="collect-hermes",
    ) == []


def test_deep_research_rejects_conflicting_reserved_source_binding() -> None:
    definition, bundle = _submitted_research_workflow()
    proposal = WorkflowPlanProposal(
        workstreams=[
            {
                "seed_id": "collect",
                "kind": "collect_sources",
                "display_title": "正在收集来源",
                "objective": "collect-sentinel",
            },
            {
                "seed_id": "synthesize",
                "kind": "synthesize",
                "display_title": "正在生成报告",
                "objective": "synthesize-sentinel",
                "depends_on": ["collect"],
            },
        ],
        constraint_bindings=[{
            "constraint_id": "evidence-source-count",
            "statement": "conflicting-source-rule-sentinel",
            "owner_work_item_ids": ["synthesize"],
            "verifier_work_item_id": "synthesize",
            "severity": "required",
        }],
    )

    with pytest.raises(ValueError, match="conflicting workflow constraint id"):
        definition.materialize_plan(
            workflow=bundle.workflow,
            proposal=proposal,
        )


def test_agent_executor_compiles_dependency_artifacts_and_persists_output(tmp_path) -> None:
    identity = RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )
    artifacts = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    source = artifacts.write_text(
        identity=identity,
        workflow_id="workflow-sentinel",
        kind="source",
        text="source-evidence-sentinel",
        producer_work_item_id="collect",
    )
    agent_runtime = CapturingAgentRuntime()
    executor = AgentRuntimeWorkItemExecutor(
        agent_runtime=agent_runtime,
        artifact_store=artifacts,
        context_compiler=WorkflowContextCompiler(artifact_store=artifacts),
    )
    assignment = WorkItemAssignment.model_validate(
        {
            "workflow_id": "workflow-sentinel",
            "workflow_type": "deep_research",
            "definition_version": "1",
            "user_id": identity.user_id,
            "agent_id": identity.agent_id,
            "session_id": identity.session_id,
            "attempt_id": "attempt-sentinel",
            "objective": "research-objective-sentinel",
            "inputs": {},
            "model_calls_remaining": 5,
            "tool_calls_remaining": 5,
            "constraint_bindings": [
                {
                    "constraint_id": "assigned-sentinel",
                    "statement": "assigned-constraint-sentinel",
                    "owner_work_item_ids": ["draft"],
                    "verifier_work_item_id": "verify",
                    "severity": "required",
                },
                {
                    "constraint_id": "other-sentinel",
                    "statement": "other-constraint-sentinel",
                    "owner_work_item_ids": ["other"],
                    "verifier_work_item_id": "verify",
                    "severity": "required",
                },
            ],
            "work_item": {
                "work_item_id": "draft",
                "kind": "draft",
                "display_title": "正在撰写草稿",
                "objective": "draft-objective-sentinel",
                "input_artifact_refs": [source.uri],
            },
        }
    )

    result = executor.execute(assignment)

    assert result.status == "succeeded"
    assert len(result.artifact_refs) == 1
    request = agent_runtime.requests[0]
    assert request.context_manifest.artifacts[0].excerpt == "source-evidence-sentinel"
    assert [item.constraint_id for item in request.assigned_constraints] == [
        "assigned-sentinel"
    ]
    assert request.allowed_tool_names == []
    assert artifacts.read_text(
        identity=identity,
        artifact_ref=result.artifact_refs[0],
    ) == "work-item-output-sentinel"
    artifacts.close()


def test_agent_executor_persists_full_content_without_overflowing_result_summary(
    tmp_path,
) -> None:
    identity = RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )
    artifacts = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    full_content = "研究正文" * 2_000
    executor = AgentRuntimeWorkItemExecutor(
        agent_runtime=LongResultAgentRuntime(full_content),
        artifact_store=artifacts,
        context_compiler=WorkflowContextCompiler(artifact_store=artifacts),
    )
    assignment = WorkItemAssignment.model_validate({
        "workflow_id": "workflow-sentinel",
        "workflow_type": "deep_research",
        "definition_version": "2",
        "user_id": identity.user_id,
        "agent_id": identity.agent_id,
        "session_id": identity.session_id,
        "attempt_id": "attempt-sentinel",
        "objective": "research-objective-sentinel",
        "inputs": {},
        "model_calls_remaining": 5,
        "tool_calls_remaining": 5,
            "work_item": {
                "work_item_id": "research",
                "kind": "research",
                "display_title": "正在执行研究",
                "objective": "research-step-sentinel",
            "acceptance_contract": {"min_sources": 4},
        },
    })

    result = executor.execute(assignment)

    assert result.status == "succeeded"
    assert len(result.summary) <= 4_000
    assert artifacts.read_text(
        identity=identity,
        artifact_ref=result.artifact_refs[0],
    ) == full_content
    artifacts.close()
