"""Temporary RED/GREEN coverage for deterministic native-plan admission."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from langgraph.store.memory import InMemoryStore

from assistant_agent.agent_server import services
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
)
from assistant_agent.native_agent.planning_graph import (
    NativePlanAdmissionError,
    PlanningAdmissionPolicy,
    admit_native_plan,
)
from assistant_agent.skills.loading import (
    SkillCatalog,
    SkillDescriptor,
    load_repo_skill_descriptors,
)


DEFAULT_TOOL_NAME = "default_probe"
GOVERNED_TOOL_NAME = "governed_probe"
SKILL_ID = "workflow-sentinel"
EVIDENCE_ID = "evidence-call-1"


def _probe_tool(name: str) -> StructuredTool:
    def probe() -> str:
        """Return one deterministic offline sentinel."""

        return "probe-sentinel"

    return StructuredTool.from_function(probe, name=name)


def _catalog() -> SkillCatalog:
    return SkillCatalog(
        descriptors=[
            SkillDescriptor(
                name=SKILL_ID,
                description="Govern the probe workflow.",
                body="Use the governed probe.",
                governed_tools=[GOVERNED_TOOL_NAME],
            )
        ]
    )


def _policy() -> PlanningAdmissionPolicy:
    return PlanningAdmissionPolicy.from_inventory(
        [_probe_tool(DEFAULT_TOOL_NAME), _probe_tool(GOVERNED_TOOL_NAME)],
        _catalog(),
    )


def _evidence() -> tuple[PlannerEvidence, ...]:
    return (
        PlannerEvidence(
            evidence_id=EVIDENCE_ID,
            tool_name=DEFAULT_TOOL_NAME,
            status="succeeded",
            content="evidence-sentinel",
        ),
    )


def _proposal(
    *,
    nodes: tuple[NativePlanNode, ...] | None = None,
    producer_node_ids: tuple[str, ...] = ("worker-1",),
    deliverable_evidence_refs: tuple[str, ...] = (EVIDENCE_ID,),
) -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v1",
        nodes=nodes
        or (
            NativePlanNode(
                node_id="worker-1",
                objective="produce the sentinel",
                allowed_tool_names=(DEFAULT_TOOL_NAME,),
                evidence_refs=(EVIDENCE_ID,),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="return the sentinel",
                producer_node_ids=producer_node_ids,
                evidence_refs=deliverable_evidence_refs,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("proposal", "active_skill_ids"),
    [
        (
            _proposal(
                nodes=(
                    NativePlanNode(
                        node_id="worker-1",
                        objective="unknown Tool",
                        allowed_tool_names=("unknown_probe",),
                        evidence_refs=(EVIDENCE_ID,),
                    ),
                )
            ),
            (),
        ),
        (
            _proposal(
                nodes=(
                    NativePlanNode(
                        node_id="worker-1",
                        objective="fake evidence",
                        allowed_tool_names=(DEFAULT_TOOL_NAME,),
                        evidence_refs=("fake-evidence",),
                    ),
                )
            ),
            (),
        ),
        (
            _proposal(
                nodes=(
                    NativePlanNode(
                        node_id="worker-1",
                        objective="ungranted governed Tool",
                        required_skill_ids=(SKILL_ID,),
                        allowed_tool_names=(GOVERNED_TOOL_NAME,),
                        evidence_refs=(EVIDENCE_ID,),
                    ),
                )
            ),
            (),
        ),
        (
            _proposal(
                nodes=(
                    NativePlanNode(
                        node_id="worker-1",
                        objective="cycle one",
                        depends_on=("worker-2",),
                    ),
                    NativePlanNode(
                        node_id="worker-2",
                        objective="cycle two",
                        depends_on=("worker-1",),
                    ),
                )
            ),
            (),
        ),
        (_proposal(producer_node_ids=("unknown-worker",)), ()),
        (_proposal(deliverable_evidence_refs=("fake-evidence",)), ()),
    ],
    ids=[
        "unknown-tool",
        "fake-node-evidence",
        "ungranted-governed-tool",
        "cycle",
        "unknown-deliverable-producer",
        "fake-deliverable-evidence",
    ],
)
def test_admission_rejects_untrusted_plan_edges(
    proposal: NativePlanProposal,
    active_skill_ids: tuple[str, ...],
) -> None:
    with pytest.raises(NativePlanAdmissionError):
        admit_native_plan(
            proposal,
            policy=_policy(),
            evidence=_evidence(),
            active_skill_ids=active_skill_ids,
        )


def test_admission_accepts_default_inventory_tool() -> None:
    proposal = _proposal()

    admitted = admit_native_plan(
        proposal,
        policy=_policy(),
        evidence=_evidence(),
        active_skill_ids=(),
    )

    assert admitted is proposal


def test_admission_accepts_governed_tool_with_matching_active_and_required_skill() -> (
    None
):
    proposal = _proposal(
        nodes=(
            NativePlanNode(
                node_id="worker-1",
                objective="authorized governed Tool",
                required_skill_ids=(SKILL_ID,),
                allowed_tool_names=(GOVERNED_TOOL_NAME,),
                evidence_refs=(EVIDENCE_ID,),
            ),
        )
    )

    admitted = admit_native_plan(
        proposal,
        policy=_policy(),
        evidence=_evidence(),
        active_skill_ids=(SKILL_ID,),
    )

    assert admitted is proposal


def test_admission_rejects_active_skill_omitted_from_governed_node_requirements() -> (
    None
):
    proposal = _proposal(
        nodes=(
            NativePlanNode(
                node_id="worker-1",
                objective="missing node grant",
                allowed_tool_names=(GOVERNED_TOOL_NAME,),
                evidence_refs=(EVIDENCE_ID,),
            ),
        )
    )

    with pytest.raises(NativePlanAdmissionError):
        admit_native_plan(
            proposal,
            policy=_policy(),
            evidence=_evidence(),
            active_skill_ids=(SKILL_ID,),
        )


def test_admission_enforces_node_count_and_dependency_depth_limits() -> None:
    two_node_proposal = _proposal(
        nodes=(
            NativePlanNode(node_id="worker-1", objective="root"),
            NativePlanNode(
                node_id="worker-2",
                objective="child",
                depends_on=("worker-1",),
            ),
        )
    )

    with pytest.raises(NativePlanAdmissionError):
        admit_native_plan(
            two_node_proposal,
            policy=replace(_policy(), max_nodes=1),
            evidence=_evidence(),
            active_skill_ids=(),
        )
    with pytest.raises(NativePlanAdmissionError):
        admit_native_plan(
            two_node_proposal,
            policy=replace(_policy(), max_dependency_depth=1),
            evidence=_evidence(),
            active_skill_ids=(),
        )


def test_policy_inventory_is_immutable() -> None:
    policy = _policy()

    with pytest.raises(TypeError):
        policy.governed_tool_skills[GOVERNED_TOOL_NAME] = frozenset()  # type: ignore[index]


def test_production_composition_loads_and_shares_one_skill_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_catalogs: list[SkillCatalog] = []
    fast_catalogs: list[SkillCatalog | None] = []
    planning_catalogs: list[SkillCatalog | None] = []
    real_build_fast_agent = services.build_fast_agent
    real_build_planning_graph = services.build_planning_graph

    def recording_load(root) -> SkillCatalog:
        catalog = load_repo_skill_descriptors(root)
        loaded_catalogs.append(catalog)
        return catalog

    def recording_build_fast_agent(*args: Any, **kwargs: Any):
        fast_catalogs.append(kwargs.get("skill_catalog"))
        return real_build_fast_agent(*args, **kwargs)

    def recording_build_planning_graph(*args: Any, **kwargs: Any):
        planning_catalogs.append(kwargs.get("skill_catalog"))
        return real_build_planning_graph(*args, **kwargs)

    monkeypatch.setattr(
        services,
        "load_repo_skill_descriptors",
        recording_load,
        raising=False,
    )
    monkeypatch.setattr(services, "build_fast_agent", recording_build_fast_agent)
    monkeypatch.setattr(
        services,
        "build_planning_graph",
        recording_build_planning_graph,
    )

    async def compose_and_close() -> None:
        owner = await AgentServerExecutionOwner.compose(store=InMemoryStore())
        await owner.aclose()

    asyncio.run(compose_and_close())

    assert len(loaded_catalogs) == 1
    assert len(fast_catalogs) == 1
    assert len(planning_catalogs) == 1
    assert fast_catalogs[0] is loaded_catalogs[0]
    assert planning_catalogs[0] is loaded_catalogs[0]
