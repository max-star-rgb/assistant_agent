from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import PrivateAttr

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.planning_agent import build_planning_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.runtime_facts import (
    capture_trusted_runtime_facts_node,
)
from assistant_agent.native_agent.state import PlanningAgentState
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware
from assistant_agent.skills.loading import SkillCatalog


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


class _CaptureMessagesModel(MockAssistantChatModel):
    observed_messages: list[tuple[Any, ...]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_messages.append(tuple(messages))
        return super()._response_message(messages, **kwargs)


class _CapturePlanningMessagesModel(MockAssistantChatModel):
    observed_calls: list[tuple[set[str], tuple[Any, ...]]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_calls.append(
            (_tool_names(kwargs.get("tools")), tuple(messages))
        )
        return super()._response_message(messages, **kwargs)


class _PlanningWriteModel(MockAssistantChatModel):
    _planning_calls: int = PrivateAttr(default=0)
    _subagent_runs: int = PrivateAttr(default=0)

    @property
    def subagent_runs(self) -> int:
        return self._subagent_runs

    def _response_message(self, messages, **kwargs):
        visible = _tool_names(kwargs.get("tools"))
        if {"task", "write_todos"} <= visible:
            self._planning_calls += 1
            if self._planning_calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "write one sentinel",
                                "subagent_type": "general-purpose",
                            },
                            "id": "task-write-sentinel",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="final-answer-sentinel")
        self._subagent_runs += 1
        if not any(
            isinstance(item, ToolMessage) and item.name == "write_probe"
            for item in messages
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_probe",
                        "args": {"value": "worker-write-sentinel"},
                        "id": "worker-write-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="worker-write-complete")


class _FastWriteModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="completed:fast-write-sentinel")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_probe",
                    "args": {"value": "fast-write-sentinel"},
                    "id": "call-fast-write-sentinel",
                    "type": "tool_call",
                }
            ],
        )


@pytest.mark.core_invariant("CTX-001")
def test_frozen_memory_is_transient_context_before_the_current_request() -> None:
    model = _CaptureMessagesModel()
    model.observed_messages = []
    graph = build_fast_agent(model, [], skill_catalog=SkillCatalog())
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )

    model_humans = [
        item for item in model.observed_messages[-1] if isinstance(item, HumanMessage)
    ]
    state_humans = [item for item in result["messages"] if isinstance(item, HumanMessage)]
    assert len(model_humans) == 2
    assert "memory-sentinel" in str(model_humans[-2].content)
    assert model_humans[-1].content == "request-sentinel"
    assert [item.content for item in state_humans] == ["request-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_planning_and_task_receive_transient_frozen_context() -> None:
    model = _CapturePlanningMessagesModel()
    model.observed_calls = []
    fast = build_fast_agent(model, [], skill_catalog=SkillCatalog())
    graph = build_planning_agent(model, fast)
    facts = capture_trusted_runtime_facts_node(
        {},
        clock=lambda: datetime(
            2026,
            8,
            24,
            12,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )["trusted_runtime_facts"]

    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "trusted_runtime_facts": facts,
            "execution_mode": "planning",
        },
        context=AssistantRunContext(),
    )

    parent_calls = [
        messages
        for tools, messages in model.observed_calls
        if {"task", "write_todos"} <= tools
    ]
    child_call = next(
        messages
        for tools, messages in model.observed_calls
        if not {"task", "write_todos"} <= tools
    )
    for messages in (parent_calls[0], child_call):
        humans = [item for item in messages if isinstance(item, HumanMessage)]
        assert len(humans) == 3
        assert "memory-sentinel" in str(humans[-3].content)
        assert "2026-08-24" in str(humans[-2].content)
        assert humans[-1].content == "request-sentinel"
    assert [
        item.content for item in result["messages"] if isinstance(item, HumanMessage)
    ] == ["request-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_create_agent_owns_native_limits_summary_retry_hitl_and_per_tool_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_agent.native_agent import fast_agent as fast_agent_module

    def probe(value: str) -> str:
        """Return one generic sentinel."""
        return value

    write_tool = StructuredTool.from_function(
        probe, name="write_probe", metadata={"effect": "write"}
    )
    read_tool = StructuredTool.from_function(
        probe, name="read_probe", metadata={"effect": "read"}
    )
    limited_tool = StructuredTool.from_function(
        probe,
        name="limited_probe",
        metadata={"effect": "read", "run_call_limit": 1},
    )
    captured: list[object] = []
    real_create_agent = fast_agent_module.create_agent

    def recording_create_agent(*args: Any, **kwargs: Any):
        captured.extend(kwargs["middleware"])
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(fast_agent_module, "create_agent", recording_create_agent)
    graph = build_fast_agent(
        MockAssistantChatModel(),
        [write_tool, read_tool, limited_tool],
        model_call_limit=3,
        tool_call_limit=4,
    )
    nodes = set(graph.get_graph().nodes)
    per_tool = [item for item in captured if isinstance(item, PerToolCallLimitMiddleware)]

    assert any(isinstance(item, ModelCallLimitMiddleware) for item in captured)
    assert any(isinstance(item, ToolCallLimitMiddleware) for item in captured)
    assert [item.run_limits for item in per_tool] == [{"limited_probe": 1}]
    assert any("SummarizationMiddleware" in node for node in nodes)
    assert any(isinstance(item, SummarizationMiddleware) for item in captured)
    assert any("HumanInTheLoopMiddleware" in node for node in nodes)
    assert any(isinstance(item, HumanInTheLoopMiddleware) for item in captured)
    assert any(isinstance(item, ToolRetryMiddleware) for item in captured)


@pytest.mark.core_invariant("CTX-001")
def test_planning_task_write_interrupts_and_resume_does_not_replay() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one approved write operation."""
        executed.append(value)
        return "write-complete"

    write_tool = StructuredTool.from_function(
        write_probe, name="write_probe", metadata={"effect": "write"}
    )
    model = _PlanningWriteModel()
    catalog = SkillCatalog()
    fast = build_fast_agent(model, [write_tool], skill_catalog=catalog)
    planning = build_planning_agent(model, fast)
    builder = StateGraph(PlanningAgentState, context_schema=AssistantRunContext)
    builder.add_node("planning", planning)
    builder.add_edge(START, "planning")
    builder.add_edge("planning", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "native-task-hitl-thread"}}

    async def run_and_resume():
        interrupted = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
                "execution_mode": "planning",
            },
            config=config,
            context=AssistantRunContext(),
        )
        runs_before_resume = model.subagent_runs
        resumed = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        return interrupted, resumed, runs_before_resume

    interrupted, resumed, runs_before_resume = asyncio.run(run_and_resume())
    assert interrupted["__interrupt__"][0].value["action_requests"][0]["name"] == "write_probe"
    assert executed == ["worker-write-sentinel"]
    assert runs_before_resume == 1
    assert model.subagent_runs == 2
    assert any(
        isinstance(item, ToolMessage)
        and item.tool_call_id == "task-write-sentinel"
        and item.content == "worker-write-complete"
        for item in resumed["messages"]
    )
    assert not any(
        isinstance(item, ToolMessage) and item.name == "write_probe"
        for item in resumed["messages"]
    )


@pytest.mark.core_invariant("CTX-001")
def test_fast_mode_write_tool_does_not_interrupt() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one fast-mode write operation."""
        executed.append(value)
        return "write-complete"

    tool = StructuredTool.from_function(
        write_probe, name="write_probe", metadata={"effect": "write"}
    )
    graph = build_fast_agent(_FastWriteModel(), [tool])
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="fast-write-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )
    assert "__interrupt__" not in result
    assert executed == ["fast-write-sentinel"]
