"""Temporary RED/GREEN coverage for planning worker recovery."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    BudgetUsage,
    FailureFact,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    RecoveryDecision,
    WorkerCompletion,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.planning_graph import (
    build_planning_graph,
    route_scheduler,
    scheduler_node,
)
from assistant_agent.native_agent.planning_recovery import (
    assess_workers_node,
    prepare_replan_node,
)
from assistant_agent.skills.loading import SkillCatalog


def _planning_input() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="request-sentinel")],
        "memory_context": (),
        "memory_status": "empty",
    }


def _initial_parallel_plan() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(node_id="weather_g0", objective="weather_g0"),
            NativePlanNode(node_id="route_g0", objective="route_g0"),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="weather and route",
                producer_node_ids=("weather_g0", "route_g0"),
            ),
        ),
    )


def _replacement_plan(*, failed_id: str) -> NativePlanProposal:
    replacement_id = f"{failed_id.removesuffix('_g0')}_replacement_g1"
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id=replacement_id,
                objective=replacement_id,
                replaces_node_ids=(failed_id,),
                frozen_dependency_ids=("weather_g0",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="weather and recovered route",
                producer_node_ids=(replacement_id,),
                frozen_result_refs=("weather_g0",),
            ),
        ),
    )


class _ParallelRecoveryAgent:
    name = "AssistantFastAgent"

    def __init__(
        self,
        *,
        failure_mode: str = "timeout",
        direct_failure: Exception | None = None,
    ) -> None:
        self.failure_mode = failure_mode
        self.direct_failure = direct_failure
        self.planner_calls = 0
        self.calls_by_objective: Counter[str] = Counter()

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        del context
        phase = input["agent_phase"]
        if phase == "planner":
            self.planner_calls += 1
            if self.planner_calls == 2:
                rendered_context = str(input["messages"][-1].content)
                assert "weather_g0" in rendered_context
                assert "route_g0" in rendered_context
                assert "answer" in rendered_context
            plan = (
                _initial_parallel_plan()
                if self.planner_calls == 1
                else _replacement_plan(failed_id="route_g0")
            )
            return {
                "messages": list(input["messages"]),
                "structured_response": plan,
            }
        if phase == "worker":
            objective = str(input["messages"][0].content).split("\n", 1)[0]
            self.calls_by_objective[objective] += 1
            if objective == "weather_g0":
                return _worker_completion("weather-ok")
            if objective == "route_g0":
                if self.direct_failure is not None:
                    raise self.direct_failure
                if self.failure_mode == "timeout":
                    raise TimeoutError("raw-provider-secret-sentinel")
                if self.failure_mode == "insufficient":
                    return _worker_completion(
                        "not enough route data",
                        status="insufficient",
                    )
                if self.failure_mode == "budget":
                    return {
                        "messages": [AIMessage(content="worker phase exhausted")],
                        "phase_budget_status": "exhausted",
                        "phase_budget_usage": BudgetUsage(model_calls=1),
                    }
                if self.failure_mode == "malformed":
                    return {
                        "messages": [AIMessage(content="malformed")],
                        "structured_response": {
                            "status": "completed",
                            "content": {"credential": "schema-credential-marker"},
                        },
                    }
            return _worker_completion("replacement-ok")
        assert phase == "finalizer"
        return {"messages": [AIMessage(content="final-answer-sentinel")]}


class _WorkerHttp4xx(Exception):
    def __init__(self) -> None:
        self.response = type(
            "Response",
            (),
            {
                "status_code": 400,
                "body": "http-body-marker",
                "credential": "http-credential-marker",
            },
        )()
        super().__init__("http-message-marker")


class _WorkerPermission(PermissionError):
    def __init__(self) -> None:
        self.body = "permission-body-marker"
        self.credential = "permission-credential-marker"
        super().__init__("permission-message-marker")


class _WorkerContract(TypeError):
    def __init__(self) -> None:
        self.body = "contract-body-marker"
        self.credential = "contract-credential-marker"
        super().__init__("contract-message-marker")


def _worker_completion(
    content: str,
    *,
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content=content)],
        "structured_response": WorkerCompletion(status=status, content=content),
        "phase_budget_usage": BudgetUsage(model_calls=1),
    }


def _one_success_one_failure_graph(*, saver: InMemorySaver | None = None):
    agent = _ParallelRecoveryAgent()
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        checkpointer=saver,
    )
    return graph, agent


def test_failed_worker_replans_without_replaying_successful_sibling() -> None:
    """Catches terminalizing failure or replaying a frozen successful sibling."""

    saver = InMemorySaver()
    graph, agent = _one_success_one_failure_graph(saver=saver)
    result = asyncio.run(
        graph.ainvoke(
            _planning_input(),
            config={"configurable": {"thread_id": "worker-recovery"}},
            context=AssistantRunContext(),
        )
    )

    assert agent.calls_by_objective["weather_g0"] == 1
    assert agent.calls_by_objective["route_g0"] == 3
    assert agent.calls_by_objective["route_replacement_g1"] == 1
    assert result["frozen_worker_results"]["weather_g0"].content == "weather-ok"
    assert result["frozen_worker_results"]["route_replacement_g1"].content == (
        "replacement-ok"
    )
    assert "route_g0" in result["superseded_work_item_ids"]
    assert result["plan_generation"] == 1
    assert result["messages"][-1].type == "ai"
    assert "raw-provider-secret-sentinel" not in _saver_text(saver)


@pytest.mark.parametrize(
    ("failure_mode", "reason_code"),
    (
        ("insufficient", "worker_business_insufficient"),
        ("budget", "worker_phase_budget_exhausted"),
    ),
)
def test_worker_expected_failure_replans_without_same_input_retry(
    failure_mode: str,
    reason_code: str,
) -> None:
    """Catches business/budget failures being retried with identical input."""

    agent = _ParallelRecoveryAgent(failure_mode=failure_mode)
    graph = build_planning_graph(object(), agent, skill_catalog=SkillCatalog())

    result = asyncio.run(
        graph.ainvoke(_planning_input(), context=AssistantRunContext())
    )

    assert agent.calls_by_objective["route_g0"] == 1
    assert result["recovery_history"][0].reason_code == reason_code
    assert result["plan_generation"] == 1


@pytest.mark.parametrize(
    ("failure", "expected_code", "markers"),
    [
        (
            _WorkerPermission(),
            "worker_authorization_failure",
            (
                "permission-message-marker",
                "permission-body-marker",
                "permission-credential-marker",
            ),
        ),
        (
            _WorkerContract(),
            "worker_contract_failure",
            (
                "contract-message-marker",
                "contract-body-marker",
                "contract-credential-marker",
            ),
        ),
        (
            _WorkerHttp4xx(),
            "worker_nonretryable_provider_failure",
            (
                "http-message-marker",
                "http-body-marker",
                "http-credential-marker",
            ),
        ),
    ],
)
def test_worker_nonrecoverable_failure_is_sanitized_before_checkpoint_boundary(
    failure: Exception,
    expected_code: str,
    markers: tuple[str, ...],
) -> None:
    """Catches raw authorization/contract/provider data entering durable writes."""

    saver = InMemorySaver()
    agent = _ParallelRecoveryAgent(direct_failure=failure)
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": f"worker-sanitize-{expected_code}"}}

    with pytest.raises(Exception) as raised:
        asyncio.run(
            graph.ainvoke(
                _planning_input(),
                config=config,
                context=AssistantRunContext(),
            )
        )
    assert type(raised.value).__name__ == "WorkerPropagationError"
    assert getattr(raised.value, "code", None) == expected_code
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    saver_text = _saver_text(saver)
    assert all(marker not in saver_text for marker in markers)
    snapshots = list(graph.get_state_history(config))
    assert all(
        all(
            getattr(outcome, "work_item_id", None) != "route_g0"
            for outcome in snapshot.values.get("worker_outcomes", {}).values()
        )
        for snapshot in snapshots
    )
    assert all("recovery_history" not in snapshot.values for snapshot in snapshots)


def test_malformed_worker_completion_is_sanitized_before_checkpoint_boundary() -> None:
    """Catches structured-response validation escaping the worker safety boundary."""

    saver = InMemorySaver()
    agent = _ParallelRecoveryAgent(failure_mode="malformed")
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "worker-malformed-completion"}}

    with pytest.raises(Exception) as raised:
        asyncio.run(
            graph.ainvoke(
                _planning_input(),
                config=config,
                context=AssistantRunContext(),
            )
        )
    assert type(raised.value).__name__ == "WorkerPropagationError"
    assert getattr(raised.value, "code", None) == "worker_contract_failure"
    assert "schema-credential-marker" not in _saver_text(saver)


def test_generation_scheduler_reuses_frozen_dependency_idempotently() -> None:
    """Catches old failures affecting readiness or frozen results being replayed."""

    weather = WorkerResult(work_item_id="weather_g0", content="weather-ok")
    plan = _replacement_plan(failed_id="route_g0")
    state = {
        **_planning_input(),
        "plan": plan,
        "plan_generation": 1,
        "frozen_worker_results": {"weather_g0": weather},
        "worker_outcomes": {},
    }

    first_update = scheduler_node(state)
    replay_update = scheduler_node(state)
    scheduled = {**state, **first_update}
    first_send = route_scheduler(scheduled)[0]
    replay_send = route_scheduler(scheduled)[0]

    assert (
        first_update
        == replay_update
        == {
            "worker_attempts": {"route_replacement_g1": 1},
            "recovery_decision": None,
        }
    )
    assert first_send.arg["execution_id"] == replay_send.arg["execution_id"]
    assert first_send.arg["execution_id"] == "g1:route_replacement_g1:a1"
    assert first_send.arg["dependency_results"] == (weather,)


def test_chained_planner_replan_does_not_reclassify_stale_admitted_plan() -> None:
    """Catches a pre-admission planner recovery superseding frozen old work."""

    policy = PlanningBudgetPolicy.from_base(1)
    weather = WorkerResult(work_item_id="weather_g0", content="weather-ok")
    weather_outcome = WorkerOutcome(
        execution_id="g0:weather_g0:a1",
        plan_generation=0,
        work_item_id="weather_g0",
        attempt=1,
        status="succeeded",
        result=weather,
        usage=BudgetUsage(node_attempts=1),
    )
    route_outcome = WorkerOutcome(
        execution_id="g0:route_g0:a3",
        plan_generation=0,
        work_item_id="route_g0",
        attempt=3,
        status="operational_failed",
        failure=FailureFact(
            category="operational",
            code="worker_operational_failure",
            phase="worker",
            plan_generation=0,
            work_item_id="route_g0",
            attempt=3,
        ),
        usage=BudgetUsage(node_attempts=1),
    )
    initial = {
        **_planning_input(),
        "plan": _initial_parallel_plan(),
        "plan_generation": 0,
        "worker_outcomes": {
            weather_outcome.execution_id: weather_outcome,
            route_outcome.execution_id: route_outcome,
        },
    }
    assessment = assess_workers_node(initial, policy=policy)
    first_replan = prepare_replan_node(
        {**initial, **assessment},
        policy=policy,
    )
    chained_state = {
        **initial,
        **assessment,
        **first_replan,
        "frozen_worker_results": {"weather_g0": weather},
        "recovery_decision": RecoveryDecision(
            action="replan",
            reason_code="planner_tool_budget_exhausted",
        ),
    }

    second_replan = prepare_replan_node(chained_state, policy=policy)

    assert second_replan["plan_generation"] == 2
    recovery_context = second_replan["recovery_context"]
    assert recovery_context["replannable_work_item_ids"] == ["route_g0"]
    assert recovery_context["frozen_result_ids"] == ["weather_g0"]
    assert "weather_g0" not in second_replan.get("superseded_work_item_ids", ())


def _saver_text(saver: InMemorySaver) -> str:
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, bytes):
            values.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)

    collect(saver.storage)
    collect(saver.writes)
    collect(saver.blobs)
    return "\n".join(values)
