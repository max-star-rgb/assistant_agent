from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)
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
    BudgetUsage,
    FailureFact,
    NativePlanNode,
    NativePlanProposal,
    PlannerEvidence,
    PlannerOutcome,
    RecoveryDecision,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import (
    PhaseBudgetMiddleware,
    WaveReservation,
)
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import PlanningState
from assistant_agent.native_agent.tool_call_limits import (
    PerToolCallLimitMiddleware,
)
from assistant_agent.skills.loading import SkillCatalog


class _HitlPlanningModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        if "NativePlanProposal" in _tool_names(kwargs.get("tools")):
            if not any(
                isinstance(message, ToolMessage)
                and message.tool_call_id == "call-planner-write-sentinel"
                for message in messages
            ):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_probe",
                            "args": {"value": "planner-write-sentinel"},
                            "id": "call-planner-write-sentinel",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "NativePlanProposal",
                        "args": {
                            "schema_version": "native_plan_v2",
                            "nodes": [
                                {
                                    "node_id": "worker-1",
                                    "objective": "worker-write-sentinel",
                                    "allowed_tool_names": ["write_probe"],
                                },
                                {
                                    "node_id": "worker-2",
                                    "objective": "dependent-sentinel",
                                    "depends_on": ["worker-1"],
                                },
                            ],
                            "deliverables": [
                                {
                                    "deliverable_id": "answer",
                                    "description": "write the sentinel",
                                    "producer_node_ids": ["worker-2"],
                                }
                            ],
                        },
                        "id": "hitl-plan-proposal",
                        "type": "tool_call",
                    }
                ],
            )
        if _last_human_text(messages).startswith("worker-write-sentinel"):
            if any(isinstance(message, ToolMessage) for message in messages):
                return _worker_completion_message(
                    "completed:worker-write-sentinel",
                    "worker-write-completion",
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_probe",
                        "args": {"value": "worker-write-sentinel"},
                        "id": "call-worker-write-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        if _last_human_text(messages).startswith("dependent-sentinel"):
            return _worker_completion_message(
                "completed:dependent-sentinel",
                "dependent-completion",
            )
        return super()._response_message(messages, **kwargs)


class _CaptureMessagesModel(MockAssistantChatModel):
    observed_messages: list[tuple[Any, ...]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_messages.append(tuple(messages))
        return super()._response_message(messages, **kwargs)


class _CompletedWorkerResumeModel(MockAssistantChatModel):
    first_worker_runs: int = 0

    def _response_message(self, messages, **kwargs):
        if "NativePlanProposal" in _tool_names(kwargs.get("tools")):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "NativePlanProposal",
                        "args": {
                            "schema_version": "native_plan_v2",
                            "nodes": [
                                {
                                    "node_id": "completed-worker",
                                    "objective": "completed-worker-sentinel",
                                },
                                {
                                    "node_id": "write-worker",
                                    "objective": "dependent-write-sentinel",
                                    "depends_on": ["completed-worker"],
                                    "allowed_tool_names": ["write_probe"],
                                },
                                {
                                    "node_id": "after-resume-worker",
                                    "objective": "after-resume-sentinel",
                                    "depends_on": ["write-worker"],
                                },
                            ],
                            "deliverables": [
                                {
                                    "deliverable_id": "answer",
                                    "description": "return the final worker",
                                    "producer_node_ids": ["after-resume-worker"],
                                }
                            ],
                        },
                        "id": "completed-worker-plan",
                        "type": "tool_call",
                    }
                ],
            )
        current = _last_human_text(messages)
        if current.startswith("completed-worker-sentinel"):
            self.first_worker_runs += 1
            return _worker_completion_message(
                "completed-worker-result",
                "completed-worker-completion",
            )
        if current.startswith("dependent-write-sentinel"):
            if any(isinstance(message, ToolMessage) for message in messages):
                return _worker_completion_message(
                    "write-worker-result",
                    "write-worker-completion",
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_probe",
                        "args": {"value": "dependent-write"},
                        "id": "dependent-write-call",
                        "type": "tool_call",
                    }
                ],
            )
        if current.startswith("after-resume-sentinel"):
            return _worker_completion_message(
                "after-resume-result",
                "after-resume-completion",
            )
        return AIMessage(content="final-answer-sentinel")


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


def _worker_completion_message(content: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "WorkerCompletion",
                "args": {"status": "completed", "content": content},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


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
def test_create_agent_owns_phase_budget_summary_retry_and_hitl_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_agent.native_agent import fast_agent as fast_agent_module

    def write_probe(value: str) -> str:
        """probe"""

        return value

    write_tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    read_tool = StructuredTool.from_function(
        write_probe,
        name="read_probe",
        metadata={"effect": "read"},
    )
    limited_tool = StructuredTool.from_function(
        write_probe,
        name="limited_probe",
        metadata={"effect": "read", "run_call_limit": 1},
    )
    captured_middleware: list[object] = []
    real_create_agent = fast_agent_module.create_agent

    def recording_create_agent(*args: Any, **kwargs: Any):
        captured_middleware.extend(kwargs["middleware"])
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(fast_agent_module, "create_agent", recording_create_agent)
    graph = build_fast_agent(
        MockAssistantChatModel(),
        [write_tool, read_tool, limited_tool],
    )
    nodes = set(graph.get_graph().nodes)
    tool_limiters = [
        item
        for item in captured_middleware
        if isinstance(item, PerToolCallLimitMiddleware)
    ]

    assert any("PhaseBudgetMiddleware" in node for node in nodes)
    assert any(isinstance(item, PhaseBudgetMiddleware) for item in captured_middleware)
    assert [item.run_limits for item in tool_limiters] == [{"limited_probe": 1}]
    assert any("SummarizationMiddleware" in node for node in nodes)
    assert any(isinstance(item, SummarizationMiddleware) for item in captured_middleware)
    assert any("HumanInTheLoopMiddleware" in node for node in nodes)
    assert any(isinstance(item, HumanInTheLoopMiddleware) for item in captured_middleware)
    assert any(isinstance(item, ToolRetryMiddleware) for item in captured_middleware)


@pytest.mark.core_invariant("CTX-001")
def test_planning_write_tools_interrupt_and_resume_without_replaying_completed_work() -> (
    None
):
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
    planning_graph = build_planning_graph(
        model,
        shared_agent,
        tools=[tool],
        skill_catalog=SkillCatalog(),
    )
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
                    PlannerEvidence,
                    BudgetUsage,
                    FailureFact,
                    PlannerOutcome,
                    RecoveryDecision,
                    WorkerOutcome,
                    WorkerResult,
                    WaveReservation,
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
        executed_before_planner_resume = tuple(executed)
        worker_interrupted = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        executed_before_worker_resume = tuple(executed)
        resumed = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        return (
            interrupted,
            worker_interrupted,
            resumed,
            executed_before_planner_resume,
            executed_before_worker_resume,
        )

    (
        planner_interrupted,
        worker_interrupted,
        resumed,
        executed_before_planner_resume,
        executed_before_worker_resume,
    ) = asyncio.run(run_and_resume())

    assert executed_before_planner_resume == ()
    assert executed_before_worker_resume == ("planner-write-sentinel",)
    assert executed == ["planner-write-sentinel", "worker-write-sentinel"]
    assert (
        planner_interrupted["__interrupt__"][0].value["action_requests"][0]["name"]
        == "write_probe"
    )
    assert (
        worker_interrupted["__interrupt__"][0].value["action_requests"][0]["name"]
        == "write_probe"
    )
    assert resumed["worker_results"] == [
        WorkerResult(
            work_item_id="worker-1",
            content="completed:worker-write-sentinel",
        ),
        WorkerResult(
            work_item_id="worker-2",
            content="completed:dependent-sentinel",
        ),
    ]


@pytest.mark.core_invariant("CTX-001")
def test_checkpoint_resume_does_not_replay_a_completed_worker() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record the dependent approved write."""

        executed.append(value)
        return "write-complete"

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    model = _CompletedWorkerResumeModel()
    shared_agent = build_fast_agent(model, [tool])
    planning_graph = build_planning_graph(
        model,
        shared_agent,
        tools=[tool],
        skill_catalog=SkillCatalog(),
    )
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
                    BudgetUsage,
                    FailureFact,
                    PlannerOutcome,
                    RecoveryDecision,
                    WorkerOutcome,
                    WorkerResult,
                    WaveReservation,
                ]
            )
        )
    )
    config = {"configurable": {"thread_id": "completed-worker-resume-thread"}}

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
        first_runs_before_resume = model.first_worker_runs
        resumed = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        return interrupted, resumed, first_runs_before_resume

    interrupted, resumed, first_runs_before_resume = asyncio.run(run_and_resume())

    assert interrupted["__interrupt__"][0].value["action_requests"][0]["name"] == (
        "write_probe"
    )
    assert first_runs_before_resume == 1
    assert model.first_worker_runs == 1
    assert executed == ["dependent-write"]
    assert [item.work_item_id for item in resumed["worker_results"]] == [
        "completed-worker",
        "write-worker",
        "after-resume-worker",
    ]


@pytest.mark.core_invariant("CTX-001")
def test_fast_mode_write_tool_does_not_interrupt() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one fast-mode write operation."""

        executed.append(value)
        return "write-complete"

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
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
