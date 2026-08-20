from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
import pytest

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import (
    build_fast_agent,
)
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    WorkerResult,
)
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import PlanningState
from assistant_agent.skills.loading import SkillCatalog


class _HitlPlanningModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        if "NativePlanProposal" in _tool_names(kwargs.get("tools")):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "NativePlanProposal",
                        "args": {
                            "schema_version": "native_plan_v1",
                            "nodes": [
                                {
                                    "node_id": "worker-1",
                                    "objective": "write-sentinel",
                                    "allowed_tool_names": ["write_probe"],
                                }
                            ],
                            "deliverables": [
                                {
                                    "deliverable_id": "answer",
                                    "description": "write the sentinel",
                                    "producer_node_ids": ["worker-1"],
                                }
                            ],
                        },
                        "id": "hitl-plan-proposal",
                        "type": "tool_call",
                    }
                ],
            )
        if _last_human_text(messages) == "write-sentinel":
            if any(isinstance(message, ToolMessage) for message in messages):
                return AIMessage(content="completed:write-sentinel")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_probe",
                        "args": {"value": "write-sentinel"},
                        "id": "call-write-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        return super()._response_message(messages, **kwargs)


class _CaptureMessagesModel(MockAssistantChatModel):
    observed_messages: list[tuple[Any, ...]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_messages.append(tuple(messages))
        return super()._response_message(messages, **kwargs)


def _last_human_text(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    return {
        function["name"]
        for item in raw_tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
        and isinstance(function.get("name"), str)
    }


@pytest.mark.core_invariant("CTX-001")
def test_frozen_memory_is_a_transient_human_context_before_current_request() -> None:
    model = _CaptureMessagesModel()
    model.observed_messages = []
    graph = build_fast_agent(
        model,
        [],
        skill_catalog=SkillCatalog(),
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )

    model_human_messages = [
        message
        for message in model.observed_messages[-1]
        if isinstance(message, HumanMessage)
    ]
    state_human_messages = [
        message for message in result["messages"] if isinstance(message, HumanMessage)
    ]

    assert len(model_human_messages) == 2
    assert "memory-sentinel" in str(model_human_messages[-2].content)
    assert model_human_messages[-1].content == "request-sentinel"
    assert [message.content for message in state_human_messages] == ["request-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_create_agent_owns_limits_summary_and_hitl_middleware() -> None:
    def write_probe(value: str) -> str:
        """probe"""

        return value

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    graph = build_fast_agent(MockAssistantChatModel(), [tool])
    nodes = set(graph.get_graph().nodes)

    assert any("ModelCallLimitMiddleware" in node for node in nodes)
    assert any("ToolCallLimitMiddleware" in node for node in nodes)
    assert any("SummarizationMiddleware" in node for node in nodes)
    assert any("HumanInTheLoopMiddleware" in node for node in nodes)


@pytest.mark.core_invariant("CTX-001")
def test_planning_worker_write_tool_interrupts_and_resumes() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one approved write operation."""

        executed.append(value)
        return "write-complete"

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    model = _HitlPlanningModel()
    shared_agent = build_fast_agent(model, [tool])
    planning_graph = build_planning_graph(model, shared_agent)
    builder = StateGraph(PlanningState, context_schema=AssistantRunContext)
    builder.add_node("planning", planning_graph)
    builder.add_edge(START, "planning")
    builder.add_edge("planning", END)
    graph = builder.compile(
        checkpointer=InMemorySaver(
            serde=JsonPlusSerializer(
                allowed_msgpack_modules=[
                    NativePlanNode,
                    NativePlanProposal,
                    WorkerResult,
                ]
            )
        )
    )
    config = {"configurable": {"thread_id": "hitl-thread-sentinel"}}

    async def run_and_resume():
        interrupted = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
            },
            config=config,
            context=AssistantRunContext(),
        )
        executed_before_resume = tuple(executed)
        resumed = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        return interrupted, resumed, executed_before_resume

    interrupted, resumed, executed_before_resume = asyncio.run(run_and_resume())

    assert executed_before_resume == ()
    assert executed == ["write-sentinel"]
    assert interrupted["__interrupt__"][0].value["action_requests"][0]["name"] == (
        "write_probe"
    )
    assert resumed["worker_results"] == [
        WorkerResult(work_item_id="worker-1", content="completed:write-sentinel")
    ]
