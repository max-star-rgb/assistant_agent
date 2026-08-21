"""Temporary RED/GREEN coverage for phase-scoped agent budgets."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import (
    BudgetUsage,
    WorkerCompletion,
    WorkerOutcome,
)
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.planning_budget import (
    PhaseLimits,
    PlanningBudgetPolicy,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import add_budget_usage
from assistant_agent.skills.loading import SkillCatalog


@pytest.mark.parametrize("legacy_name", ("model_call_limit", "tool_call_limit"))
def test_fast_agent_rejects_removed_legacy_limit_keywords(
    legacy_name: str,
) -> None:
    with pytest.raises(TypeError):
        build_fast_agent(
            MockAssistantChatModel(),
            [],
            skill_catalog=SkillCatalog(),
            **{legacy_name: 1},
        )


def test_budget_policy_derives_approved_limits() -> None:
    policy = PlanningBudgetPolicy.from_base(8)

    assert policy.phase_limits("fast") == PhaseLimits(8, 9)
    assert policy.phase_limits("planner") == PhaseLimits(16, 17)
    assert policy.phase_limits("worker") == PhaseLimits(8, 9)
    assert policy.phase_limits("finalizer") == PhaseLimits(0, 1)
    assert policy.graph_tool_limit == 64
    assert policy.graph_model_limit == 80
    assert policy.graph_node_attempt_limit == 32
    assert policy.max_replans == 2


def test_planner_tool_budget_returns_closed_messages_instead_of_raising() -> None:
    result = asyncio.run(_run_budget_loop(phase="planner", base=1))

    assert result["phase_budget_status"] == "exhausted"
    assert result["phase_budget_usage"].tool_calls == 2
    assert _successful_tool_message_ids(result["messages"]) == ["call-1", "call-2"]
    blocked = _tool_message(result["messages"], "call-3")
    assert blocked.status == "error"
    assert result["messages"][-1].type == "ai"


def test_budget_usage_reducer_accepts_checkpoint_safe_mapping() -> None:
    assert add_budget_usage(
        {"model_calls": 1, "tool_calls": 0, "node_attempts": 0, "replans": 0},
        BudgetUsage(tool_calls=1),
    ) == BudgetUsage(model_calls=1, tool_calls=1)


def test_worker_consumes_mock_structured_completion() -> None:
    result = asyncio.run(_run_mock_worker(MockAssistantChatModel()))

    worker = result["worker_results"][0]
    assert worker.content == "mock worker completion"
    assert worker.verification_status == "advisory"


def test_worker_preserves_insufficient_completion_as_business_outcome() -> None:
    result = asyncio.run(_run_mock_worker(_InsufficientWorkerModel(), max_replans=0))

    worker = next(iter(result["worker_outcomes"].values()))
    assert isinstance(worker, WorkerOutcome)
    assert worker.status == "business_failed"
    assert worker.failure is not None
    assert worker.failure.code == "worker_business_insufficient"
    assert result["worker_results"] == []


def test_structured_completion_does_not_consume_tool_budget() -> None:
    result = asyncio.run(_run_budget_completion_loop())

    assert result["phase_tool_call_count"] == 2
    assert result["phase_budget_usage"].tool_calls == 2
    assert "phase_budget_status" not in result
    assert result["structured_response"] == WorkerCompletion(
        status="completed",
        content="budget completion",
    )
    tool_call_ids = [
        call["id"]
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    closed_ids = [
        message.tool_call_id
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert sorted(closed_ids) == sorted(tool_call_ids)
    assert len(closed_ids) == len(set(closed_ids))


async def _run_budget_loop(*, phase: str, base: int) -> dict[str, Any]:
    @tool
    def probe_tool() -> str:
        """Return deterministic probe evidence."""

        return "probe-ok"

    agent = build_fast_agent(
        _ThreeProbeCallsModel(),
        [probe_tool],
        budget_policy=PlanningBudgetPolicy.from_base(base),
        skill_catalog=SkillCatalog(),
    )
    return await agent.ainvoke(
        {
            "messages": [HumanMessage(content="budget-loop")],
            "agent_phase": phase,
        },
        context=AssistantRunContext(),
    )


async def _run_mock_worker(
    model: MockAssistantChatModel,
    *,
    max_replans: int | None = None,
) -> dict[str, Any]:
    agent = build_fast_agent(model, [], skill_catalog=SkillCatalog())
    budget_policy = PlanningBudgetPolicy.from_base(8)
    if max_replans is not None:
        budget_policy = replace(budget_policy, max_replans=max_replans)
    graph = build_planning_graph(
        model,
        agent,
        tools=[],
        skill_catalog=SkillCatalog(),
        budget_policy=budget_policy,
    )
    return await graph.ainvoke(
        {
            "messages": [HumanMessage(content="mock worker request")],
            "memory_context": (),
            "memory_status": "empty",
        },
        context=AssistantRunContext(),
    )


async def _run_budget_completion_loop() -> dict[str, Any]:
    @tool
    def probe_tool() -> str:
        """Return deterministic probe evidence."""

        return "probe-ok"

    agent = build_fast_agent(
        _TwoProbeThenCompletionModel(),
        [probe_tool],
        budget_policy=PlanningBudgetPolicy.from_base(2),
        skill_catalog=SkillCatalog(),
    )
    return await agent.ainvoke(
        {
            "messages": [HumanMessage(content="budget-completion-loop")],
            "agent_phase": "worker",
            "worker_tool_allowlist": ("probe_tool",),
        },
        context=AssistantRunContext(),
    )


class _ThreeProbeCallsModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        completed = sum(
            isinstance(message, ToolMessage) and message.name == "probe_tool"
            for message in messages
        )
        if completed < 3:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "probe_tool",
                        "args": {},
                        "id": f"call-{completed + 1}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="unreachable")


class _InsufficientWorkerModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        message = super()._response_message(messages, **kwargs)
        if any(call["name"] == "WorkerCompletion" for call in message.tool_calls):
            return message.model_copy(
                update={
                    "tool_calls": [
                        {
                            "name": "WorkerCompletion",
                            "args": {
                                "status": "insufficient",
                                "content": "mock worker insufficient",
                            },
                            "id": "mock-insufficient-worker-completion",
                            "type": "tool_call",
                        }
                    ]
                }
            )
        return message


class _TwoProbeThenCompletionModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        completed = sum(
            isinstance(message, ToolMessage) and message.name == "probe_tool"
            for message in messages
        )
        if completed < 2:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "probe_tool",
                        "args": {},
                        "id": f"budget-probe-{completed + 1}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "WorkerCompletion",
                    "args": {
                        "status": "completed",
                        "content": "budget completion",
                    },
                    "id": "budget-worker-completion",
                    "type": "tool_call",
                }
            ],
        )


def _successful_tool_message_ids(messages: list[Any]) -> list[str]:
    return [
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage) and message.status != "error"
    ]


def _tool_message(messages: list[Any], tool_call_id: str) -> ToolMessage:
    return next(
        message
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id == tool_call_id
    )
