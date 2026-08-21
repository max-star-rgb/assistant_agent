"""Temporary RED/GREEN coverage for the explicit native planning scheduler."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphInterrupt

from assistant_agent.native_agent import planning_graph
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
    WorkerResult,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.skills.loading import SkillCatalog, load_repo_skill_descriptors
from assistant_agent.tools.ids import LOAD_SKILL_REFERENCE_TOOL_NAME
from assistant_agent.tools.plugins.builtin.skill_loading import (
    create_load_skill_reference_tool,
)


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
                    "frozen_result_refs": [],
                }
        ],
        "planner_evidence": [evidence.model_dump(mode="json")],
        "worker_results": [],
    }


def test_compiled_worker_exhaustion_blocks_dependents_before_finalizing() -> None:
    """Catches a root worker failure aborting the graph before DAG propagation."""

    model = _FailingRootWorkerModel()
    shared_agent = build_fast_agent(model, [], skill_catalog=SkillCatalog())
    graph = planning_graph.build_planning_graph(
        model,
        shared_agent,
        skill_catalog=SkillCatalog(),
    )

    async def run_with_updates():
        updates: list[dict[str, Any]] = []
        final: dict[str, Any] | None = None
        async for mode, chunk in graph.astream(
            {
                "messages": [HumanMessage(content="failure-request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
            },
            context=AssistantRunContext(),
            stream_mode=["updates", "values"],
        ):
            if mode == "updates":
                updates.append(chunk)
            else:
                final = chunk
        assert final is not None
        return updates, final

    updates, result = asyncio.run(run_with_updates())

    assert model.root_attempts == 3
    assert model.dependent_attempts == 0
    assert model.finalizer_payload is not None
    worker_results = model.finalizer_payload["worker_results"]
    assert [item["work_item_id"] for item in worker_results] == ["root", "child"]
    assert [item["verification_status"] for item in worker_results] == [
        "failed",
        "failed",
    ]
    assert "provider-secret-sentinel" not in json.dumps(worker_results)
    assert worker_results[0]["content"] == (
        "worker execution failed after bounded operational retries"
    )
    assert worker_results[1]["content"] == (
        "work item blocked because a direct dependency failed: root"
    )
    result_updates = [
        item for update in updates for item in _worker_result_updates(update)
    ]
    assert [item["work_item_id"] for item in result_updates] == ["root", "child"]
    assert len(result["worker_results"]) == 2
    assert sum(isinstance(message, AIMessage) for message in result["messages"]) == 1
    assert result["messages"][-1].content == "finalized-failure-sentinel"


def _operational_wrapper_with_cause(cause: Exception) -> TimeoutError:
    failure = TimeoutError("operational-wrapper")
    failure.__cause__ = cause
    return failure


def _deep_operational_chain_with_cause(cause: Exception) -> TimeoutError:
    current = cause
    for _ in range(8):
        current = _operational_wrapper_with_cause(current)
    assert isinstance(current, TimeoutError)
    return current


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("denied"),
        TypeError("bug"),
        _operational_wrapper_with_cause(PermissionError("wrapped-denied")),
        _operational_wrapper_with_cause(TypeError("wrapped-bug")),
        _operational_wrapper_with_cause(AttributeError("wrapped-attribute-bug")),
        _operational_wrapper_with_cause(RuntimeError("wrapped-runtime-bug")),
        _operational_wrapper_with_cause(GraphInterrupt()),
        _deep_operational_chain_with_cause(PermissionError("deep-wrapped-denied")),
    ],
    ids=[
        "permission",
        "type",
        "wrapped-permission",
        "wrapped-type",
        "wrapped-attribute",
        "wrapped-runtime",
        "wrapped-graph-interrupt",
        "deep-wrapped-permission",
    ],
)
def test_worker_failure_boundary_does_not_convert_non_operational_errors(
    failure: Exception,
) -> None:
    """Catches permission or programmer errors becoming advisory worker output."""

    agent = _DirectFailureFastAgent(failure)
    graph = planning_graph.build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
    )

    with pytest.raises(type(failure), match=str(failure)):
        asyncio.run(
            graph.ainvoke(
                {
                    "messages": [HumanMessage(content="failure-request-sentinel")],
                    "memory_context": (),
                    "memory_status": "empty",
                },
                context=AssistantRunContext(),
            )
        )


def test_worker_failure_classifier_never_converts_cancellation() -> None:
    failure = asyncio.CancelledError("cancelled")
    failure.__cause__ = TimeoutError("transport-timeout")

    assert planning_graph._is_worker_operational_failure(failure) is False


def test_worker_reference_tool_uses_only_scheduler_projected_grants(
    tmp_path: Path,
) -> None:
    """Catches inherited reference access widening worker Skill grants."""

    _write_reference_skill(tmp_path)
    catalog = load_repo_skill_descriptors(tmp_path)
    reference_tool = create_load_skill_reference_tool(root=tmp_path)

    allowed = _invoke_worker_reference(
        reference_id="allowed-guide",
        reference_tool=reference_tool,
        catalog=catalog,
    )
    denied = _invoke_worker_reference(
        reference_id="blocked-guide",
        reference_tool=reference_tool,
        catalog=catalog,
    )

    allowed_message = _only_tool_message(allowed)
    denied_message = _only_tool_message(denied)
    assert allowed_message.status == "success"
    assert allowed_message.artifact["content"] == "allowed-reference-sentinel\n"
    assert denied_message.status == "error"
    assert "skill_reference_not_loaded" in str(denied_message.content)
    assert denied["active_skill_ids"] == ["travel-sentinel"]
    assert denied["skill_reference_grants"] == {"travel-sentinel": ["allowed-guide"]}


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
                    schema_version="native_plan_v2",
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


class _FailingRootWorkerModel(MockAssistantChatModel):
    root_attempts: int = 0
    dependent_attempts: int = 0
    finalizer_payload: dict[str, Any] | None = None

    def _response_message(self, messages, **kwargs):
        if "NativePlanProposal" in _model_tool_names(kwargs.get("tools")):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "NativePlanProposal",
                        "args": {
                            "schema_version": "native_plan_v2",
                            "nodes": [
                                {
                                    "node_id": "root",
                                    "objective": "root-failure-sentinel",
                                },
                                {
                                    "node_id": "child",
                                    "objective": "dependent-must-not-run-sentinel",
                                    "depends_on": ["root"],
                                },
                            ],
                            "deliverables": [
                                {
                                    "deliverable_id": "answer",
                                    "description": "include failures and limitations",
                                    "producer_node_ids": ["root", "child"],
                                }
                            ],
                        },
                        "id": "failure-plan-proposal",
                        "type": "tool_call",
                    }
                ],
            )
        current = _last_human_message_text(messages)
        if current.startswith("root-failure-sentinel"):
            self.root_attempts += 1
            raise TimeoutError("provider-secret-sentinel")
        if current.startswith("dependent-must-not-run-sentinel"):
            self.dependent_attempts += 1
            return AIMessage(content="dependent-ran-unexpectedly")
        self.finalizer_payload = json.loads(current)
        return AIMessage(content="finalized-failure-sentinel")


class _DirectFailureFastAgent:
    name = "AssistantFastAgent"

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def ainvoke(self, input: dict[str, Any], *, context: Any):
        del context
        if input["agent_phase"] == "planner":
            return {
                "messages": list(input["messages"]),
                "structured_response": NativePlanProposal(
                    schema_version="native_plan_v2",
                    nodes=(
                        NativePlanNode(
                            node_id="root",
                            objective="root-failure-sentinel",
                        ),
                    ),
                    deliverables=(
                        PlanDeliverable(
                            deliverable_id="answer",
                            description="answer sentinel",
                            producer_node_ids=("root",),
                        ),
                    ),
                ),
            }
        if input["agent_phase"] == "worker":
            raise self.failure
        return {"messages": [AIMessage(content="must-not-finalize")]}


class _ReferenceCallModel(MockAssistantChatModel):
    reference_id: str

    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="reference-call-complete")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": LOAD_SKILL_REFERENCE_TOOL_NAME,
                    "args": {
                        "skill_id": "travel-sentinel",
                        "reference_id": self.reference_id,
                    },
                    "id": f"reference-{self.reference_id}",
                    "type": "tool_call",
                }
            ],
        )


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
            schema_version="native_plan_v2",
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


def _invoke_worker_reference(
    *,
    reference_id: str,
    reference_tool: BaseTool,
    catalog: SkillCatalog,
) -> dict[str, Any]:
    graph = build_fast_agent(
        _ReferenceCallModel(reference_id=reference_id),
        [reference_tool],
        skill_catalog=catalog,
    )
    return graph.invoke(
        {
            "messages": [HumanMessage(content="reference-request-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "planning",
            "agent_phase": "worker",
            "worker_tool_allowlist": (LOAD_SKILL_REFERENCE_TOOL_NAME,),
            "active_skill_ids": ["travel-sentinel"],
            "skill_reference_grants": {"travel-sentinel": ["allowed-guide"]},
        },
        context=AssistantRunContext(),
        config={
            "configurable": {
                "langgraph_auth_user": _AuthenticatedUser(),
            }
        },
    )


def _only_tool_message(result: dict[str, Any]) -> ToolMessage:
    messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert len(messages) == 1
    return messages[0]


def _model_tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    return {
        function["name"]
        for item in raw_tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
        and isinstance(function.get("name"), str)
    }


def _last_human_message_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _worker_result_updates(update: dict[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for value in update.values():
        if not isinstance(value, dict):
            continue
        for result in value.get("worker_results", ()):
            if isinstance(result, WorkerResult):
                projected.append(result.model_dump(mode="json"))
            elif isinstance(result, dict):
                projected.append(result)
    return projected


class _AuthenticatedUser(dict):
    identity = "worker-user-sentinel"
    permissions = ()


def _write_reference_skill(root: Path) -> None:
    skill_dir = root / "skills" / "travel-sentinel"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "Use the inherited travel workflow.\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.toml").write_text(
        "schema_version = 1\n"
        'skill_id = "travel-sentinel"\n'
        "version = 1\n"
        'description = "Travel workflow"\n'
        'governed_tools = ["route_probe"]\n'
        "[references]\n"
        'allowed-guide = "references/allowed-guide.md"\n'
        'blocked-guide = "references/blocked-guide.md"\n',
        encoding="utf-8",
    )
    (references_dir / "allowed-guide.md").write_text(
        "allowed-reference-sentinel\n",
        encoding="utf-8",
    )
    (references_dir / "blocked-guide.md").write_text(
        "blocked-reference-sentinel\n",
        encoding="utf-8",
    )
