"""Controlled durable-workflow Environment shared by Deep Research Missions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.ids import WORKFLOW_SUBMIT_TOOL_NAME
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import InMemoryWorkflowStore
from evals.agent.contracts import AssertionResult, RunEvidence
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.grading import rule_assertion
from evals.agent.task_support import build_controlled_registry


DEEP_RESEARCH_PLAN_STAGE_IDS = (
    "scope",
    "collect_sources",
    "extract_evidence",
    "outline",
    "draft",
    "verify",
    "synthesize",
)


class DeepResearchMissionEnvironment(ControlledTaskEnvironment):
    """Run actual workflow admission against isolated in-memory persistence."""

    dependency_label = "controlled:deep_research_workflow"
    writes = True
    state_reset = "in_memory_per_mission_run"
    expected_objective_terms: ClassVar[tuple[str, ...]] = ()
    expected_deliverable_terms: ClassVar[tuple[str, ...]] = ()
    expected_constraint_terms: ClassVar[tuple[str, ...]] = ()
    minimum_research_questions: ClassVar[int] = 1
    minimum_source_target: ClassVar[int] = 3

    def setup(self) -> None:
        self._tempdir = TemporaryDirectory(prefix="agent-eval-deep-research-")
        self._root = Path(self._tempdir.name)
        base = self.config or ProviderConfig(provider_mode="mock")
        self.config = replace(
            base,
            durable_workflows_enabled=True,
            durable_workflow_worker_enabled=False,
            durable_workflow_path=str(self._root / "workflows.sqlite3"),
            durable_workflow_artifact_path=str(self._root / "artifacts"),
        )
        self.workflow_store = InMemoryWorkflowStore()
        self.workflow_service = WorkflowService(
            store=self.workflow_store,
            definitions=default_workflow_definitions(),
        )

    def build_registry(self) -> ToolRegistry:
        return build_controlled_registry(
            config=self.config,
            workflow_service=self.workflow_service,
        )

    def required_successes(self) -> tuple[str, ...]:
        return (WORKFLOW_SUBMIT_TOOL_NAME,)

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> dict[str, AssertionResult]:
        workflow_types = self.workflow_service.definitions.list_types()
        return {
            "deep_research_definition_registered": rule_assertion(
                "deep_research" in workflow_types,
                f"workflow_types={workflow_types}",
                label="Deep Research Workflow 已注册",
            ),
            "workflow_store_isolated": rule_assertion(
                isinstance(self.workflow_store, InMemoryWorkflowStore),
                "store=in_memory_per_mission_run",
                label="Workflow 状态按 Mission 隔离",
            ),
            "workflow_tool_governed": rule_assertion(
                WORKFLOW_SUBMIT_TOOL_NAME in registry.list(),
                f"registered={WORKFLOW_SUBMIT_TOOL_NAME in registry.list()}",
                label="Workflow Tool 经过受控目录注册",
            ),
        }

    def initial_state(self, request: UserRequest) -> dict[str, Any]:
        del request
        return {"workflow": None}

    def runtime_overrides(self, request: UserRequest) -> dict[str, Any]:
        del request
        return {"workflow_service": self.workflow_service}

    def final_state_reader(self, request: UserRequest):
        del request

        def read(runtime: AgentGraphRuntime, state: Any) -> dict[str, Any]:
            del runtime
            workflow_id = None
            for result in state.tool_results:
                if result.tool_name != WORKFLOW_SUBMIT_TOOL_NAME or not result.success:
                    continue
                workflow = result.data.get("workflow")
                if isinstance(workflow, dict):
                    workflow_id = workflow.get("workflow_id")
            if not isinstance(workflow_id, str) or not workflow_id:
                return {"workflow": None}
            bundle = self.workflow_store.load(workflow_id)
            if bundle is None:
                return {"workflow": None}
            workflow = bundle.workflow
            plan = bundle.current_plan
            events = self.workflow_store.list_events(
                workflow_id,
                after=0,
                limit=500,
            )
            return {
                "workflow": {
                    "workflow_type": workflow.workflow_type,
                    "status": workflow.status,
                    "phase": workflow.phase,
                    "objective": workflow.objective,
                    "deliverables": list(workflow.deliverables),
                    "constraints": list(workflow.constraints),
                    "inputs": dict(workflow.inputs),
                    "plan_stage_ids": [item.work_item_id for item in plan.work_items],
                    "acceptance_contracts": {
                        item.work_item_id: dict(item.acceptance_contract)
                        for item in plan.work_items
                        if item.acceptance_contract
                    },
                    "event_types": [event.event_type for event in events],
                }
            }

        return read

    def objective_state_assertions(
        self,
        evidence: RunEvidence,
    ) -> dict[str, AssertionResult]:
        workflow = evidence.final_state.get("workflow")
        payload = workflow if isinstance(workflow, dict) else {}
        objective = str(payload.get("objective") or "")
        deliverables = [str(item) for item in payload.get("deliverables") or []]
        constraints = [str(item) for item in payload.get("constraints") or []]
        inputs = payload.get("inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        questions = inputs.get("research_questions")
        questions = questions if isinstance(questions, list) else []
        source_target = inputs.get("source_target")
        objective_terms_present = _contains_all(objective, self.expected_objective_terms)
        deliverable_terms_present = _contains_all(
            "\n".join(deliverables),
            self.expected_deliverable_terms,
        )
        constraint_terms_present = _contains_all(
            "\n".join([objective, *constraints]),
            self.expected_constraint_terms,
        )
        plan_stage_ids = payload.get("plan_stage_ids")
        return {
            "workflow_created": rule_assertion(
                payload.get("workflow_type") == "deep_research"
                and payload.get("status") == "queued",
                (
                    f"workflow_type={payload.get('workflow_type')!r}, "
                    f"status={payload.get('status')!r}"
                ),
                label="已创建 Deep Research Workflow",
            ),
            "objective_grounded": rule_assertion(
                objective_terms_present,
                f"required_terms={list(self.expected_objective_terms)}",
                label="研究目标忠于用户请求",
            ),
            "deliverables_grounded": rule_assertion(
                deliverable_terms_present,
                f"required_terms={list(self.expected_deliverable_terms)}",
                label="研究交付物完整映射",
            ),
            "constraints_grounded": rule_assertion(
                constraint_terms_present,
                f"required_terms={list(self.expected_constraint_terms)}",
                label="研究约束完整映射",
            ),
            "research_questions_structured": rule_assertion(
                len(questions) >= self.minimum_research_questions
                and all(isinstance(item, str) and item.strip() for item in questions),
                (
                    f"question_count={len(questions)}, "
                    f"minimum={self.minimum_research_questions}"
                ),
                label="研究问题已结构化拆分",
            ),
            "source_target_sufficient": rule_assertion(
                isinstance(source_target, int)
                and source_target >= self.minimum_source_target,
                (
                    f"source_target={source_target!r}, "
                    f"minimum={self.minimum_source_target}"
                ),
                label="多来源目标满足研究要求",
            ),
            "deep_research_plan_initialized": rule_assertion(
                plan_stage_ids == list(DEEP_RESEARCH_PLAN_STAGE_IDS),
                f"plan_stage_ids={plan_stage_ids!r}",
                label="七阶段研究计划已初始化",
            ),
        }


def _contains_all(value: str, terms: tuple[str, ...]) -> bool:
    normalized = value.casefold()
    return all(term.casefold() in normalized for term in terms)
