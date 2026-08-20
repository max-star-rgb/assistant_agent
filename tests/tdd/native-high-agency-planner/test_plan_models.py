"""Temporary RED/GREEN coverage for the high-agency planning contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
)
from assistant_agent.native_agent.state import _merge_planner_evidence


def test_plan_allows_zero_nodes_when_evidence_produces_deliverable() -> None:
    evidence = PlannerEvidence(
        evidence_id="tool-call-1",
        tool_name="weather_probe",
        status="succeeded",
        content="sunny",
    )
    proposal = NativePlanProposal(
        schema_version="native_plan_v1",
        nodes=(),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="回答天气",
                evidence_refs=(evidence.evidence_id,),
            ),
        ),
    )
    assert proposal.nodes == ()


def test_deliverable_requires_a_producer_or_evidence() -> None:
    with pytest.raises(ValidationError):
        PlanDeliverable(deliverable_id="answer", description="形成回答")


def test_plan_node_rejects_duplicate_evidence_references() -> None:
    with pytest.raises(ValidationError, match="node evidence refs must be unique"):
        NativePlanNode(
            node_id="research",
            objective="研究天气",
            evidence_refs=("tool-call-1", "tool-call-1"),
        )


def test_planner_evidence_reducer_preserves_first_item_for_each_id() -> None:
    existing = PlannerEvidence(
        evidence_id="tool-call-1",
        tool_name="weather_probe",
        status="succeeded",
        content="sunny",
    )
    duplicate = PlannerEvidence(
        evidence_id="tool-call-1",
        tool_name="weather_probe",
        status="failed",
        content="should-not-replace",
    )
    new = PlannerEvidence(
        evidence_id="tool-call-2",
        tool_name="forecast_probe",
        status="succeeded",
        content="warm",
    )

    assert _merge_planner_evidence([existing], [duplicate, new]) == [existing, new]
