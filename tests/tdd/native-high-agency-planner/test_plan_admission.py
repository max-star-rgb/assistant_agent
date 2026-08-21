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
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.tools.plugins.builtin.skill_loading import (
    plugin as skill_loading_plugin,
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
        schema_version="native_plan_v2",
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


def test_admission_rejects_worker_load_skill_even_when_it_is_in_inventory() -> None:
    """Catches a worker expanding its Planner-projected Skill snapshot."""

    proposal = _proposal(
        nodes=(
            NativePlanNode(
                node_id="worker-1",
                objective="attempt to expand worker Skill scope",
                allowed_tool_names=(LOAD_SKILL_TOOL_NAME,),
                evidence_refs=(EVIDENCE_ID,),
            ),
        )
    )
    policy = PlanningAdmissionPolicy.from_inventory(
        [_probe_tool(DEFAULT_TOOL_NAME), _probe_tool(LOAD_SKILL_TOOL_NAME)],
        _catalog(),
    )

    with pytest.raises(NativePlanAdmissionError, match="load_skill"):
        admit_native_plan(
            proposal,
            policy=policy,
            evidence=_evidence(),
            active_skill_ids=(),
        )


def test_admission_keeps_explicit_reference_tool_for_inherited_skill() -> None:
    proposal = _proposal(
        nodes=(
            NativePlanNode(
                node_id="worker-1",
                objective="read an inherited Skill reference",
                required_skill_ids=(SKILL_ID,),
                allowed_tool_names=(LOAD_SKILL_REFERENCE_TOOL_NAME,),
                evidence_refs=(EVIDENCE_ID,),
            ),
        )
    )
    policy = PlanningAdmissionPolicy.from_inventory(
        [_probe_tool(DEFAULT_TOOL_NAME), _probe_tool(LOAD_SKILL_REFERENCE_TOOL_NAME)],
        _catalog(),
    )

    admitted = admit_native_plan(
        proposal,
        policy=policy,
        evidence=_evidence(),
        active_skill_ids=(SKILL_ID,),
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


@pytest.mark.parametrize("required_skill_id", ["skill-a", "skill-b"])
def test_admission_accepts_any_one_of_multiple_governing_skills(
    required_skill_id: str,
) -> None:
    """Catches multi-governed Tools accidentally requiring every governing Skill."""

    catalog = SkillCatalog(
        descriptors=[
            SkillDescriptor(
                name=skill_id,
                description=f"Govern via {skill_id}.",
                body=f"Use {skill_id}.",
                governed_tools=[GOVERNED_TOOL_NAME],
            )
            for skill_id in ("skill-a", "skill-b")
        ]
    )
    policy = PlanningAdmissionPolicy.from_inventory(
        [_probe_tool(DEFAULT_TOOL_NAME), _probe_tool(GOVERNED_TOOL_NAME)],
        catalog,
    )
    proposal = _proposal(
        nodes=(
            NativePlanNode(
                node_id="worker-1",
                objective="authorized by one governing Skill",
                required_skill_ids=(required_skill_id,),
                allowed_tool_names=(GOVERNED_TOOL_NAME,),
            ),
        )
    )

    assert (
        admit_native_plan(
            proposal,
            policy=policy,
            evidence=_evidence(),
            active_skill_ids=(required_skill_id,),
        )
        is proposal
    )


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


@pytest.mark.parametrize(
    ("nodes", "max_depth", "expected_code"),
    [
        (
            (
                NativePlanNode(node_id="worker-1", objective="root"),
                NativePlanNode(
                    node_id="worker-2",
                    objective="middle",
                    depends_on=("worker-1",),
                ),
                NativePlanNode(
                    node_id="worker-3",
                    objective="leaf",
                    depends_on=("worker-2",),
                ),
            ),
            3,
            None,
        ),
        (
            (
                NativePlanNode(node_id="worker-1", objective="root"),
                NativePlanNode(
                    node_id="worker-2",
                    objective="middle-1",
                    depends_on=("worker-1",),
                ),
                NativePlanNode(
                    node_id="worker-3",
                    objective="middle-2",
                    depends_on=("worker-2",),
                ),
                NativePlanNode(
                    node_id="worker-4",
                    objective="leaf",
                    depends_on=("worker-3",),
                ),
            ),
            3,
            "dependency_depth_exceeded",
        ),
        (
            (
                NativePlanNode(node_id="worker-1", objective="root"),
                NativePlanNode(
                    node_id="worker-2",
                    objective="left",
                    depends_on=("worker-1",),
                ),
                NativePlanNode(
                    node_id="worker-3",
                    objective="right",
                    depends_on=("worker-1",),
                ),
                NativePlanNode(
                    node_id="worker-4",
                    objective="join",
                    depends_on=("worker-2", "worker-3"),
                ),
            ),
            3,
            None,
        ),
    ],
    ids=["exact-depth", "over-depth", "diamond-depth"],
)
def test_admission_dependency_depth_table(
    nodes: tuple[NativePlanNode, ...],
    max_depth: int,
    expected_code: str | None,
) -> None:
    proposal = _proposal(
        nodes=nodes,
        producer_node_ids=(nodes[-1].node_id,),
    )
    if expected_code is None:
        assert (
            admit_native_plan(
                proposal,
                policy=replace(_policy(), max_dependency_depth=max_depth),
                evidence=_evidence(),
                active_skill_ids=(),
            )
            is proposal
        )
        return
    with pytest.raises(NativePlanAdmissionError) as raised:
        admit_native_plan(
            proposal,
            policy=replace(_policy(), max_dependency_depth=max_depth),
            evidence=_evidence(),
            active_skill_ids=(),
        )
    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    ("proposal", "evidence", "expected_code"),
    [
        (
            _proposal(producer_node_ids=("worker-1", "worker-1")),
            _evidence(),
            "duplicate_deliverable_producer",
        ),
        (
            _proposal(
                deliverable_evidence_refs=(EVIDENCE_ID, EVIDENCE_ID),
            ),
            _evidence(),
            "duplicate_deliverable_evidence_ref",
        ),
        (
            _proposal(),
            (*_evidence(), *_evidence()),
            "duplicate_evidence_id",
        ),
    ],
    ids=[
        "duplicate-deliverable-producers",
        "duplicate-deliverable-evidence-refs",
        "duplicate-evidence-ids",
    ],
)
def test_admission_duplicate_reference_table(
    proposal: NativePlanProposal,
    evidence: tuple[PlannerEvidence, ...],
    expected_code: str,
) -> None:
    with pytest.raises(NativePlanAdmissionError) as raised:
        admit_native_plan(
            proposal,
            policy=_policy(),
            evidence=evidence,
            active_skill_ids=(),
        )
    assert raised.value.code == expected_code


def test_policy_inventory_is_immutable() -> None:
    policy = _policy()

    with pytest.raises(TypeError):
        policy.governed_tool_skills[GOVERNED_TOOL_NAME] = frozenset()  # type: ignore[index]


def test_production_composition_loads_and_shares_one_skill_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_loaded_catalogs: list[SkillCatalog] = []
    plugin_loaded_catalogs: list[SkillCatalog] = []
    inventory_catalogs: list[SkillCatalog | None] = []
    skill_loading_plugin_catalogs: list[SkillCatalog | None] = []
    fast_catalogs: list[SkillCatalog | None] = []
    planning_catalogs: list[SkillCatalog | None] = []
    real_skill_loading_plugin = skill_loading_plugin.SkillLoadingPlugin
    real_create_native_tool_inventory = services.create_native_tool_inventory
    real_build_fast_agent = services.build_fast_agent
    real_build_planning_graph = services.build_planning_graph

    def recording_service_load(root) -> SkillCatalog:
        catalog = load_repo_skill_descriptors(root)
        service_loaded_catalogs.append(catalog)
        return catalog

    def recording_plugin_load(root) -> SkillCatalog:
        catalog = load_repo_skill_descriptors(root)
        plugin_loaded_catalogs.append(catalog)
        return catalog

    async def recording_create_native_tool_inventory(*args: Any, **kwargs: Any):
        inventory_catalogs.append(kwargs.get("skill_catalog"))
        return await real_create_native_tool_inventory(*args, **kwargs)

    def recording_skill_loading_plugin(
        *,
        skill_catalog: SkillCatalog | None = None,
    ):
        skill_loading_plugin_catalogs.append(skill_catalog)
        return real_skill_loading_plugin(skill_catalog=skill_catalog)

    def recording_build_fast_agent(*args: Any, **kwargs: Any):
        fast_catalogs.append(kwargs.get("skill_catalog"))
        return real_build_fast_agent(*args, **kwargs)

    def recording_build_planning_graph(*args: Any, **kwargs: Any):
        planning_catalogs.append(kwargs.get("skill_catalog"))
        return real_build_planning_graph(*args, **kwargs)

    monkeypatch.setattr(
        services,
        "load_repo_skill_descriptors",
        recording_service_load,
    )
    monkeypatch.setattr(
        skill_loading_plugin,
        "load_repo_skill_descriptors",
        recording_plugin_load,
    )
    monkeypatch.setattr(
        skill_loading_plugin,
        "SkillLoadingPlugin",
        recording_skill_loading_plugin,
    )
    monkeypatch.setattr(
        services,
        "create_native_tool_inventory",
        recording_create_native_tool_inventory,
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

    assert len(service_loaded_catalogs) + len(plugin_loaded_catalogs) == 1
    assert len(service_loaded_catalogs) == 1
    assert plugin_loaded_catalogs == []
    assert len(inventory_catalogs) == 1
    assert len(skill_loading_plugin_catalogs) == 1
    assert len(fast_catalogs) == 1
    assert len(planning_catalogs) == 1
    assert inventory_catalogs[0] is service_loaded_catalogs[0]
    assert skill_loading_plugin_catalogs[0] is service_loaded_catalogs[0]
    assert fast_catalogs[0] is service_loaded_catalogs[0]
    assert planning_catalogs[0] is service_loaded_catalogs[0]


def test_native_tool_inventory_requires_catalog_before_skill_plugin_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents the Skill plugin fallback from becoming a second production load."""

    plugin_load_roots: list[Any] = []

    def recording_plugin_load(root) -> SkillCatalog:
        plugin_load_roots.append(root)
        return SkillCatalog(descriptors=[])

    monkeypatch.setattr(
        skill_loading_plugin,
        "load_repo_skill_descriptors",
        recording_plugin_load,
    )

    with pytest.raises(TypeError, match="skill_catalog"):
        asyncio.run(
            services.create_native_tool_inventory(
                services.ProviderConfig(provider_mode="mock"),
                resources=services.NativeToolResources(),
                mcp_server_configs=[],
            )
        )

    assert plugin_load_roots == []
