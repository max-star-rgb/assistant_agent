"""Temporary RED/GREEN coverage for native plan revision and checkpoint resume."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
    WorkerResult,
)
from assistant_agent.native_agent.planning_graph import (
    NativePlanAdmissionError,
    build_planning_graph,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import PlanningState
from assistant_agent.skills.loading import SkillCatalog


class _RevisionFastAgent:
    name = "AssistantFastAgent"

    def __init__(self, *, always_invalid: bool = False) -> None:
        self.always_invalid = always_invalid
        self.planner_inputs: list[dict[str, Any]] = []

    async def ainvoke(self, input: dict[str, Any], *, context: Any):
        del context
        phase = input["agent_phase"]
        if phase == "planner":
            self.planner_inputs.append(input)
            attempt = len(self.planner_inputs)
            invalid = self.always_invalid or attempt == 1
            node_id = "invalid-worker" if invalid else "valid-worker"
            allowed_tools = ("unknown_probe",) if invalid else ()
            return {
                "messages": list(input["messages"]),
                "structured_response": NativePlanProposal(
                    schema_version="native_plan_v1",
                    nodes=(
                        NativePlanNode(
                            node_id=node_id,
                            objective="worker-sentinel",
                            allowed_tool_names=allowed_tools,
                            evidence_refs=("evidence-1",),
                        ),
                    ),
                    deliverables=(
                        PlanDeliverable(
                            deliverable_id="answer",
                            description="answer sentinel",
                            producer_node_ids=(node_id,),
                            evidence_refs=("evidence-1",),
                        ),
                    ),
                ),
                "active_skill_ids": (
                    ["travel-sentinel"] if attempt == 1 else []
                ),
                "skill_reference_grants": (
                    {"travel-sentinel": ["guide-sentinel"]}
                    if attempt == 1
                    else {}
                ),
            }
        if phase == "worker":
            return {
                "messages": [
                    *input["messages"],
                    AIMessage(content="worker-complete-sentinel"),
                ]
            }
        return {
            "messages": [
                *input["messages"],
                AIMessage(content="final-answer-sentinel"),
            ]
        }


def test_invalid_plan_reenters_planner_through_native_edge() -> None:
    """Catches admission failures escaping instead of revising through graph state."""

    evidence = PlannerEvidence(
        evidence_id="evidence-1",
        tool_name="weather_probe",
        status="succeeded",
        content="weather-content-sentinel",
        structured_content={"must_not": "enter-revision-context"},
        artifact_ref="artifact://weather-sentinel",
    )
    agent = _RevisionFastAgent()
    graph = build_planning_graph(
        object(),
        agent,
        tools=[_probe_tool("weather_probe")],
        skill_catalog=SkillCatalog(),
    )

    async def stream_run():
        updates: list[dict[str, Any]] = []
        final: dict[str, Any] | None = None
        async for mode, chunk in graph.astream(
            _planning_input(planner_evidence=[evidence]),
            context=AssistantRunContext(),
            stream_mode=["updates", "values"],
        ):
            if mode == "updates":
                updates.append(chunk)
            elif mode == "values":
                final = chunk
        assert final is not None
        return updates, final

    updates, result = asyncio.run(stream_run())

    assert [next(iter(update)) for update in updates].count("planner") == 2
    assert result["revision_count"] == 1
    assert result["admission_error"] is None
    assert result["plan"].nodes[0].node_id == "valid-worker"
    assert result["planner_active_skill_ids"] == ["travel-sentinel"]
    assert result["planner_skill_reference_grants"] == {
        "travel-sentinel": ["guide-sentinel"]
    }
    assert result["planner_evidence"] == [evidence]

    correction = agent.planner_inputs[1]["messages"][-1]
    assert isinstance(correction, HumanMessage)
    payload = _revision_payload(str(correction.content))
    assert payload == {
        "admission_error_code": "unknown_tool",
        "planner_evidence": [
            {
                "evidence_id": "evidence-1",
                "tool_name": "weather_probe",
                "status": "succeeded",
                "content": "weather-content-sentinel",
                "artifact_ref": "artifact://weather-sentinel",
            }
        ],
    }
    assert "worker references an unknown Tool" not in str(correction.content)


def test_third_invalid_candidate_raises_bounded_admission_error() -> None:
    """Catches unbounded plan-revision loops or raw admission failures."""

    evidence = PlannerEvidence(
        evidence_id="evidence-1",
        tool_name="weather_probe",
        status="succeeded",
        content="weather-content-sentinel",
    )
    agent = _RevisionFastAgent(always_invalid=True)
    graph = build_planning_graph(
        object(),
        agent,
        tools=[_probe_tool("weather_probe")],
        skill_catalog=SkillCatalog(),
    )

    with pytest.raises(NativePlanAdmissionError) as raised:
        asyncio.run(
            graph.ainvoke(
                _planning_input(planner_evidence=[evidence]),
                context=AssistantRunContext(),
            )
        )

    assert len(agent.planner_inputs) == 3
    assert raised.value.code == "unknown_tool"
    assert str(raised.value) == "plan admission failed after bounded revisions: unknown_tool"


class _CheckpointPlanningModel(MockAssistantChatModel):
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
                                    "node_id": "write-worker",
                                    "objective": "write-worker-sentinel",
                                    "allowed_tool_names": ["write_probe"],
                                },
                                {
                                    "node_id": "dependent-worker",
                                    "objective": "dependent-worker-sentinel",
                                    "depends_on": ["write-worker"],
                                },
                            ],
                            "deliverables": [
                                {
                                    "deliverable_id": "answer",
                                    "description": "return both results",
                                    "producer_node_ids": ["dependent-worker"],
                                }
                            ],
                        },
                        "id": "checkpoint-plan-proposal",
                        "type": "tool_call",
                    }
                ],
            )
        current = _last_human_text(messages)
        if current.startswith("write-worker-sentinel"):
            if any(
                isinstance(message, ToolMessage) and message.name == "write_probe"
                for message in messages
            ):
                return AIMessage(content="write-worker-complete")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_probe",
                        "args": {"value": "write-worker"},
                        "id": "checkpoint-write-call",
                        "type": "tool_call",
                    }
                ],
            )
        if current.startswith("dependent-worker-sentinel"):
            return AIMessage(content="dependent-worker-complete")
        return AIMessage(content="final-answer-sentinel")


def test_scheduler_recomputes_ready_nodes_after_checkpoint_resume() -> None:
    """Catches resume replaying completed workers or skipping a dependent wave."""

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
    model = _CheckpointPlanningModel()
    shared_agent = build_fast_agent(model, [tool], skill_catalog=SkillCatalog())
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
    checkpointed_graph = builder.compile(
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
    config = {"configurable": {"thread_id": "revision-resume-thread"}}

    async def run_and_resume():
        interrupted = await checkpointed_graph.ainvoke(
            _planning_input(),
            config=config,
            context=AssistantRunContext(),
        )
        resumed = await checkpointed_graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        return interrupted, resumed

    interrupted, resumed = asyncio.run(run_and_resume())

    assert interrupted["__interrupt__"]
    assert executed == ["write-worker"]
    assert [item.work_item_id for item in resumed["worker_results"]] == [
        "write-worker",
        "dependent-worker",
    ]


def _planning_input(**updates: Any) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="request-sentinel")],
        "memory_context": (),
        "memory_status": "empty",
        **updates,
    }


def _probe_tool(name: str) -> StructuredTool:
    def probe() -> str:
        """Return one deterministic offline sentinel."""

        return "probe-sentinel"

    return StructuredTool.from_function(probe, name=name, metadata={"effect": "read"})


def _revision_payload(content: str) -> dict[str, Any]:
    opening = '<plan_revision_context format="json" readonly="true">\n'
    closing = "\n</plan_revision_context>"
    assert content.startswith(opening)
    return json.loads(content.removeprefix(opening).split(closing, 1)[0])


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


def _last_human_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""
