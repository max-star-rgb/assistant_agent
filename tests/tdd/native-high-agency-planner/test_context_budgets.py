"""Temporary RED/GREEN coverage for planning context and artifact budgets."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from html import unescape
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from assistant_agent.native_agent import planning_graph
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    EvidenceLink,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
    WorkerResult,
)
from assistant_agent.skills.loading import SkillCatalog


def test_worker_context_budget_preserves_metadata_and_shares_content_fairly() -> None:
    """Catches item-count bounds still rendering one giant worker message."""

    sources = (
        EvidenceLink(
            index=1,
            title="source-title-sentinel",
            url="https://example.test/source",
            domain="example.test",
        ),
    )
    dependencies = tuple(
        WorkerResult(
            work_item_id=f"dependency-{position}",
            content="<&>" * 30_000,
            verification_status="verified",
            sources=sources,
        )
        for position in range(3)
    )
    evidence = tuple(
        PlannerEvidence(
            evidence_id=f"evidence-{position}",
            tool_name="evidence_probe",
            status="succeeded",
            content="<&>" * 6_666,
            artifact_ref=f"artifact://evidence-{position}",
        )
        for position in range(3)
    )

    prompt = planning_graph._worker_prompt(
        {
            "objective": "worker-objective-sentinel",
            "dependency_results": dependencies,
            "planner_evidence": evidence,
        }
    )
    dependency_payload = _tagged_payload(prompt, "dependency_results")
    evidence_payload = _tagged_payload(prompt, "planner_evidence")

    assert len(prompt) <= 48_000
    assert [item["work_item_id"] for item in dependency_payload] == [
        "dependency-0",
        "dependency-1",
        "dependency-2",
    ]
    assert {item["verification_status"] for item in dependency_payload} == {"verified"}
    assert all(
        item["sources"] == [sources[0].model_dump(mode="json")]
        for item in dependency_payload
    )
    assert [item["evidence_id"] for item in evidence_payload] == [
        "evidence-0",
        "evidence-1",
        "evidence-2",
    ]
    assert [item["artifact_ref"] for item in evidence_payload] == [
        "artifact://evidence-0",
        "artifact://evidence-1",
        "artifact://evidence-2",
    ]
    projected_content = [
        *(item["content"] for item in dependency_payload),
        *(item["content"] for item in evidence_payload),
    ]
    assert all(projected_content)
    assert max(map(len, projected_content)) - min(map(len, projected_content)) <= 1
    assert all(item["content_truncated"] is True for item in dependency_payload)
    assert all(item["content_truncated"] is True for item in evidence_payload)


def test_finalizer_message_budget_includes_request_and_all_ordered_results() -> None:
    """Catches a giant latest user message bypassing middleware compaction."""

    evidence = [
        PlannerEvidence(
            evidence_id=f"evidence-{position}",
            tool_name="evidence_probe",
            status="succeeded",
            content="<&>" * 6_666,
            artifact_ref=f"artifact://evidence-{position}",
        )
        for position in range(4)
    ]
    agent = _BudgetFastAgent()
    graph = planning_graph.build_planning_graph(
        object(),
        agent,
        tools=[_ProbeTool("evidence_probe")],
        skill_catalog=SkillCatalog(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="<&>" * 60_000)],
                "memory_context": (),
                "memory_status": "empty",
                "planner_evidence": evidence,
            },
            context=AssistantRunContext(),
        )
    )

    assert agent.finalizer_message is not None
    assert len(agent.finalizer_message) <= 96_000
    payload = json.loads(agent.finalizer_message)
    assert payload["request_truncated"] is True
    assert [item["deliverable_id"] for item in payload["deliverables"]] == ["answer"]
    assert [item["evidence_id"] for item in payload["planner_evidence"]] == [
        f"evidence-{position}" for position in range(4)
    ]
    assert [item["artifact_ref"] for item in payload["planner_evidence"]] == [
        f"artifact://evidence-{position}" for position in range(4)
    ]
    assert [item["work_item_id"] for item in payload["worker_results"]] == [
        f"worker-{position}" for position in range(4)
    ]
    assert {item["verification_status"] for item in payload["worker_results"]} == {
        "verified"
    }
    assert all(
        item["sources"][0]["url"] == "https://example.test/source"
        for item in payload["worker_results"]
    )
    evidence_lengths = [len(item["content"]) for item in payload["planner_evidence"]]
    worker_lengths = [len(item["content"]) for item in payload["worker_results"]]
    assert (
        max(evidence_lengths + worker_lengths) - min(evidence_lengths + worker_lengths)
        <= 1
    )
    assert all(
        item["content_truncated"] is True for item in payload["planner_evidence"]
    )
    assert all(item["content_truncated"] is True for item in payload["worker_results"])
    assert sum(isinstance(message, AIMessage) for message in result["messages"]) == 1


def test_artifact_projection_traverses_huge_deep_cyclic_values_incrementally() -> None:
    """Catches full artifact materialization, unsafe recursion, and raw stringify."""

    artifact = _HugeArtifact()

    projected = planning_graph._bounded_artifact(artifact)

    assert artifact.visited_items <= 512
    assert projected["artifact_ref"] == "artifact://bounded-traversal-sentinel"
    structured = projected["structured_content"]
    encoded = json.dumps(
        structured,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert len(encoded) <= 50_000
    assert _has_truncation_marker(structured)
    assert "provider_raw_response" not in encoded.decode("utf-8")
    assert "unknown-object-sentinel" not in encoded.decode("utf-8")


def test_artifact_projection_reserves_space_for_truncation_marker() -> None:
    projected = planning_graph._bounded_artifact(
        {"payload": "x" * 49_960, "overflow": "y" * 1_000}
    )

    structured = projected["structured_content"]
    encoded = json.dumps(
        structured,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert len(encoded) <= 50_000
    assert _has_truncation_marker(structured)


class _BudgetFastAgent:
    name = "AssistantFastAgent"

    def __init__(self) -> None:
        self.finalizer_message: str | None = None

    async def ainvoke(self, input: dict[str, Any], *, context: Any):
        del context
        phase = input["agent_phase"]
        if phase == "planner":
            nodes = tuple(
                NativePlanNode(
                    node_id=f"worker-{position}",
                    objective=f"worker-{position}-objective",
                    evidence_refs=(f"evidence-{position}",),
                )
                for position in range(4)
            )
            return {
                "messages": list(input["messages"]),
                "structured_response": NativePlanProposal(
                    schema_version="native_plan_v2",
                    nodes=nodes,
                    deliverables=(
                        PlanDeliverable(
                            deliverable_id="answer",
                            description="final deliverable sentinel",
                            producer_node_ids=tuple(node.node_id for node in nodes),
                            evidence_refs=tuple(
                                f"evidence-{position}" for position in range(4)
                            ),
                        ),
                    ),
                ),
            }
        if phase == "worker":
            return {
                "messages": [
                    *input["messages"],
                    AIMessage(
                        content="<&>" * 33_333,
                        response_metadata={
                            "provider_search_sources": [
                                {
                                    "index": 1,
                                    "title": "source-title-sentinel",
                                    "url": "https://example.test/source",
                                }
                            ]
                        },
                    ),
                ]
            }
        self.finalizer_message = str(input["messages"][0].content)
        return {"messages": [AIMessage(content="finalizer-budget-sentinel")]}


class _ProbeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _ArtifactModel(BaseModel):
    output_ref: str
    nested: object


class _HugeArtifact(Mapping[str, Any]):
    def __init__(self) -> None:
        self.visited_items = 0
        cycle: list[Any] = []
        cycle.append(cycle)
        deep: dict[str, Any] = {"leaf": "deep-sentinel"}
        for position in range(32):
            deep = {f"level-{position}": deep}
        self._special = {
            "provider_raw_response": {"secret": "must-not-be-retained"},
            "model": _ArtifactModel(
                output_ref="artifact://bounded-traversal-sentinel",
                nested={"cycle": cycle, "deep": deep, "opaque": _UnknownArtifact()},
            ),
        }

    def __getitem__(self, key: str) -> Any:
        if key in self._special:
            return self._special[key]
        return "x" * 4_000

    def __iter__(self) -> Iterator[str]:
        for key in self._special:
            self.visited_items += 1
            yield key
        for position in range(1_000_000):
            self.visited_items += 1
            if self.visited_items > 512:
                raise AssertionError("artifact traversal exceeded the item budget")
            yield f"payload-{position}"

    def __len__(self) -> int:
        return 1_000_002


class _UnknownArtifact:
    def __str__(self) -> str:
        return "unknown-object-sentinel"


def _tagged_payload(prompt: str, tag: str) -> list[dict[str, Any]]:
    opening = f'<{tag} format="json"'
    start = prompt.index(opening)
    encoded = prompt[prompt.index("\n", start) + 1 : prompt.index(f"\n</{tag}>", start)]
    return json.loads(unescape(encoded))


def _has_truncation_marker(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("_truncated") is True:
            return True
        return any(_has_truncation_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_truncation_marker(item) for item in value)
    return False
