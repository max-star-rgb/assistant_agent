from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from evals.agent.contracts import TaskSpec
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import InMemoryWorkflowStore


class ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="deep-research-call",
                            name="workflow_submit",
                            arguments={
                                "workflow_type": "deep_research",
                                "objective": "研究 AI Agent 评测体系的行业实践",
                                "deliverables": ["带引用的研究报告", "执行摘要"],
                                "constraints": ["区分事实、推断和未决问题"],
                                "inputs": {
                                    "research_questions": [
                                        "业界如何评测长流程 Agent？",
                                        "线上监控与离线实验如何统一？",
                                    ],
                                    "source_target": 12,
                                },
                                "requested_budget": {},
                                "durability_reasons": [
                                    "multi_stage",
                                    "source_collection",
                                ],
                                "idempotency_key": "deep-research-eval",
                            },
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text="深度研究任务已创建，我会按阶段推进。",
                ),
            ]
        )

    def chat(self, request):
        return next(self.results)


def _probe_environment_type(workflow_service=None):
    support = importlib.import_module("evals.agent.deep_research_support")

    class ProbeEnvironment(support.DeepResearchMissionEnvironment):
        expected_objective_terms = ("AI Agent", "评测")
        expected_deliverable_terms = ("研究报告", "执行摘要")
        expected_constraint_terms = ("事实", "推断")
        minimum_research_questions = 2
        minimum_source_target = 10

        def runtime_overrides(self, request):
            del request
            return {"workflow_service": workflow_service}

    return ProbeEnvironment


def _task() -> TaskSpec:
    return TaskSpec(
        id="deep_research_probe",
        description="probe",
        capability="deep research workflow admission",
        request=UserRequest(
            user_id="eval-user",
            session_id="eval-session",
            text="请做一项需要多来源核验的深度研究。",
        ),
        environment="tests:probe",
        grader="tests:probe",
    )


def test_deep_research_environment_static_validation_is_offline() -> None:
    environment = _probe_environment_type()(
        config=ProviderConfig(provider_mode="mock")
    )

    validation = environment.validate()
    expectations = environment.tool_outcome_expectations()

    assert validation.passed is True
    assert environment.runtime_assembly is None
    assert len(expectations) == 1
    workflow_expectation = expectations[0]
    assert workflow_expectation.tool_name == "workflow_submit"
    assert workflow_expectation.required is True
    assert workflow_expectation.expected_result == "success"


def test_deep_research_environment_projects_created_workflow_state(tmp_path) -> None:
    workflow_service = WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=default_workflow_definitions(),
    )
    environment = _probe_environment_type(workflow_service)(
        config=ProviderConfig(
            provider_mode="mock",
            durable_workflows_enabled=True,
            durable_workflow_path=str(tmp_path / "workflows.sqlite3"),
            durable_workflow_artifact_path=str(tmp_path / "artifacts"),
        ),
        chat_adapter=ScriptedChatAdapter(),
    )

    execution = environment.execute(
        task=_task(),
        request=_task().request,
        trace_id="4" * 32,
        parent_span_id="5" * 16,
    )
    assertions = environment.objective_state_assertions(execution.evidence)

    assert execution.evidence.terminal_status == "completed"
    assert execution.evidence.tool_executions[0].dependency_mode == "live"
    assert execution.evidence.final_state["workflow"]["workflow_type"] == (
        "deep_research"
    )
    assert execution.evidence.final_state["workflow"]["plan_stage_ids"] == [
        "scope",
        "collect_sources",
        "extract_evidence",
        "outline",
        "draft",
        "verify",
        "synthesize",
    ]
    assert assertions
    assert all(item.passed for item in assertions.values())
import importlib
