"""Temporary RED/GREEN coverage for phase-scoped agent budgets."""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import BudgetUsage
from assistant_agent.native_agent.planning_budget import PhaseLimits, PlanningBudgetPolicy
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import add_budget_usage
from assistant_agent.skills.loading import SkillCatalog


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
