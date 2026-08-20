"""Temporary RED/GREEN coverage for the explicit native planning scheduler."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
    WorkerResult,
)
from assistant_agent.native_agent import planning_graph


def test_failed_dependency_becomes_terminal_without_dispatch_or_deadlock() -> None:
    """Catches failed dependencies being dispatched, repeated, or left pending."""

    failed = _result("root", verification_status="failed")
    state = _state(
        nodes=(
            NativePlanNode(node_id="root", objective="root objective"),
            NativePlanNode(
                node_id="child",
                objective="child objective",
                depends_on=("root",),
            ),
            NativePlanNode(
                node_id="grandchild",
                objective="grandchild objective",
                depends_on=("child",),
            ),
        ),
        results=(failed,),
    )

    update = planning_graph.scheduler_node(state)

    blocked = update["worker_results"]
    assert [item.work_item_id for item in blocked] == ["child", "grandchild"]
    assert [item.verification_status for item in blocked] == ["failed", "failed"]
    terminal_state = {**state, "worker_results": [failed, *blocked]}
    assert planning_graph.route_scheduler(terminal_state) == "finalize"
    assert planning_graph.scheduler_node(terminal_state) == {}


def test_scheduler_dispatches_only_ready_wave() -> None:
    """Catches roots or dependent nodes being dispatched in the wrong wave."""

    nodes = (
        NativePlanNode(node_id="weather", objective="weather objective"),
        NativePlanNode(node_id="food", objective="food objective"),
        NativePlanNode(
            node_id="itinerary",
            objective="itinerary objective",
            depends_on=("weather", "food"),
        ),
    )

    first = planning_graph.route_scheduler(_state(nodes=nodes))
    second = planning_graph.route_scheduler(
        _state(nodes=nodes, results=(_result("weather"), _result("food")))
    )

    assert [send.arg["work_item_id"] for send in first] == ["weather", "food"]
    assert [send.arg["work_item_id"] for send in second] == ["itinerary"]


def test_worker_receives_only_scoped_inputs() -> None:
    """Catches planner history, unrelated evidence/results, or Skill grants leaking."""

    nodes = (
        NativePlanNode(node_id="weather", objective="weather objective"),
        NativePlanNode(node_id="food", objective="food objective"),
        NativePlanNode(
            node_id="itinerary",
            objective="itinerary objective",
            depends_on=("weather",),
            required_skill_ids=("travel-sentinel", "inactive-sentinel"),
            allowed_tool_names=("route_probe",),
            evidence_refs=("route-call",),
        ),
    )
    state = _state(
        nodes=nodes,
        results=(_result("weather"), _result("food")),
        evidence=(
            _evidence("weather-call", tool_name="weather_probe"),
            _evidence("route-call", tool_name="route_probe"),
        ),
    )
    state.update(
        {
            "messages": [HumanMessage(content="planner-history-must-not-leak")],
            "planner_active_skill_ids": ["travel-sentinel", "unrelated-sentinel"],
            "planner_skill_reference_grants": {
                "travel-sentinel": ["route-guide"],
                "unrelated-sentinel": ["unrelated-guide"],
            },
        }
    )

    send = planning_graph.route_scheduler(state)[0]

    assert [item.work_item_id for item in send.arg["dependency_results"]] == ["weather"]
    assert [item.evidence_id for item in send.arg["planner_evidence"]] == ["route-call"]
    assert send.arg["active_skill_ids"] == ["travel-sentinel"]
    assert send.arg["skill_reference_grants"] == {"travel-sentinel": ["route-guide"]}
    assert send.arg["worker_tool_allowlist"] == ("route_probe",)
    assert send.arg["messages"] == []


def test_zero_node_plan_routes_directly_to_shared_agent_finalizer() -> None:
    """Catches empty admitted plans failing or bypassing the shared fast agent."""

    evidence = _evidence("planner-call", tool_name="planner_probe")
    agent = _RecordingFastAgent(evidence_id=evidence.evidence_id)
    graph = planning_graph.build_planning_graph(
        object(),
        agent,
        tools=[_probe_tool("planner_probe")],
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
                "planner_evidence": [evidence],
            },
            context=AssistantRunContext(),
        )
    )

    assert [call["agent_phase"] for call in agent.calls] == ["planner", "finalizer"]
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "final-answer-sentinel"
    final_payload = json.loads(str(agent.calls[-1]["messages"][0].content))
    assert final_payload == {
        "request": "request-sentinel",
        "deliverables": [
            {
                "deliverable_id": "answer",
                "description": "answer from planner evidence",
                "producer_node_ids": [],
                "evidence_refs": ["planner-call"],
            }
        ],
        "planner_evidence": [evidence.model_dump(mode="json")],
        "worker_results": [],
    }


class _RecordingFastAgent:
    name = "AssistantFastAgent"

    def __init__(self, *, evidence_id: str) -> None:
        self._evidence_id = evidence_id
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, input: dict[str, Any], *, context: Any):
        del context
        self.calls.append(input)
        if input["agent_phase"] == "planner":
            return {
                "messages": list(input["messages"]),
                "structured_response": NativePlanProposal(
                    schema_version="native_plan_v1",
                    nodes=(),
                    deliverables=(
                        PlanDeliverable(
                            deliverable_id="answer",
                            description="answer from planner evidence",
                            evidence_refs=(self._evidence_id,),
                        ),
                    ),
                ),
                "active_skill_ids": [],
                "skill_reference_grants": {},
            }
        return {
            "messages": [
                *input["messages"],
                AIMessage(content="final-answer-sentinel"),
            ]
        }


def _state(
    *,
    nodes: tuple[NativePlanNode, ...],
    results: tuple[WorkerResult, ...] = (),
    evidence: tuple[PlannerEvidence, ...] = (),
) -> dict[str, Any]:
    producer_ids = (nodes[-1].node_id,) if nodes else ()
    evidence_refs = () if nodes else (evidence[0].evidence_id,)
    return {
        "messages": [],
        "memory_context": (),
        "memory_status": "empty",
        "plan": NativePlanProposal(
            schema_version="native_plan_v1",
            nodes=nodes,
            deliverables=(
                PlanDeliverable(
                    deliverable_id="answer",
                    description="answer sentinel",
                    producer_node_ids=producer_ids,
                    evidence_refs=evidence_refs,
                ),
            ),
        ),
        "planner_evidence": list(evidence),
        "worker_results": list(results),
    }


def _result(
    work_item_id: str,
    *,
    verification_status: str = "verified",
) -> WorkerResult:
    return WorkerResult(
        work_item_id=work_item_id,
        content=f"{work_item_id}-result",
        verification_status=verification_status,
    )


def _evidence(evidence_id: str, *, tool_name: str) -> PlannerEvidence:
    return PlannerEvidence(
        evidence_id=evidence_id,
        tool_name=tool_name,
        status="succeeded",
        content=f"{evidence_id}-content",
    )


def _probe_tool(name: str) -> StructuredTool:
    def probe() -> str:
        """Return one offline scheduler sentinel."""

        return "probe-sentinel"

    return StructuredTool.from_function(probe, name=name)
