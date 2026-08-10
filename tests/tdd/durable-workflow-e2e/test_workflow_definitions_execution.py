from __future__ import annotations

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.agent_runtime import AgentWorkItemResult
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.definitions import WorkflowDefinitionCatalog
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.models import WorkflowSeedWorkItem, WorkflowSubmission
from assistant_agent.workflows.research.definition import DeepResearchWorkflowDefinition
from assistant_agent.workflows.runtime import WorkItemAssignment


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


def test_deep_research_is_a_definition_not_a_special_runtime() -> None:
    definition = DeepResearchWorkflowDefinition()
    submission = WorkflowSubmission(
        workflow_type="deep_research",
        objective="research-objective-sentinel",
        deliverables=["report-sentinel"],
        inputs={"research_questions": ["question-sentinel"], "source_target": 12},
        durability_reasons=["multi_stage", "many_sources"],
        idempotency_key="submission-sentinel",
    )

    plan = definition.build_initial_plan(
        workflow_id="workflow-sentinel",
        submission=submission,
    )

    assert WorkflowDefinitionCatalog([definition]).list_types() == ("deep_research",)
    assert [item.kind for item in plan.work_items] == [
        "scope",
        "collect_sources",
        "extract_evidence",
        "outline",
        "draft",
        "verify",
        "synthesize",
    ]
    assert plan.work_items[-1].depends_on == ["verify"]


def test_deep_research_uses_agent_planned_workstreams_and_display_titles() -> None:
    definition = DeepResearchWorkflowDefinition()
    submission = WorkflowSubmission(
        workflow_type="deep_research",
        objective="research-objective-sentinel",
        deliverables=["report-sentinel"],
        inputs={"research_questions": ["question-sentinel"], "source_target": 15},
        initial_workstreams=[
            WorkflowSeedWorkItem(
                seed_id="research-hermes",
                kind="collect_sources",
                display_title="正在检索并核实 Hermes 工程资料",
                objective="收集 Hermes 官方文档和一手工程资料。",
            ),
            WorkflowSeedWorkItem(
                seed_id="synthesize",
                kind="synthesize",
                display_title="正在撰写跨框架对比报告",
                objective="综合证据并形成最终报告。",
                depends_on=["research-hermes"],
            ),
        ],
        durability_reasons=["multi_stage", "many_sources"],
        idempotency_key="submission-agent-plan-sentinel",
    )

    plan = definition.build_initial_plan(
        workflow_id="workflow-sentinel",
        submission=submission,
    )

    assert [item.work_item_id for item in plan.work_items] == [
        "research-hermes",
        "synthesize",
    ]
    assert plan.work_items[0].display_title == "正在检索并核实 Hermes 工程资料"
    assert plan.work_items[1].depends_on == ["research-hermes"]


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
            "work_item": {
                "work_item_id": "draft",
                "kind": "draft",
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
    assert request.allowed_tool_names == []
    assert artifacts.read_text(
        identity=identity,
        artifact_ref=result.artifact_refs[0],
    ) == "work-item-output-sentinel"
    artifacts.close()
