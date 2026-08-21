"""Temporary RED/GREEN coverage for generation-aware plan admission."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from assistant_agent.native_agent.models import (
    BudgetUsage,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlanningAuthorizationEnvelope,
    PlannerEvidence,
    ReplacementClaim,
    SkillReferenceGrant,
)
from assistant_agent.native_agent.planning_budget import (
    PlanningBudgetPolicy,
    WaveReservation,
)
from assistant_agent.native_agent.planning_graph import (
    NativePlanAdmissionError,
    PlanningAdmissionPolicy,
    admit_native_plan,
    build_planning_graph,
)
from assistant_agent.skills.loading import SkillCatalog, SkillDescriptor


def _policy() -> PlanningAdmissionPolicy:
    return PlanningAdmissionPolicy(
        inventory_tool_names=frozenset(),
        governed_tool_skills={},
    )


def _replacement_plan(replacement: str) -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="route_replacement_g1",
                objective="replace failed route",
                replaces_node_ids=(replacement,),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_replacement_g1",),
            ),
        ),
    )


def _admit_recovery(proposal: NativePlanProposal) -> NativePlanProposal:
    return admit_native_plan(
        proposal,
        policy=_policy(),
        evidence=(),
        active_skill_ids=(),
        plan_generation=1,
        historical_node_ids={"route_g0", "weather_g0"},
        replannable_node_ids={"route_g0"},
        frozen_result_ids={"weather_g0"},
    )


def test_recovery_plan_accepts_unique_replacement_and_frozen_dependency() -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="route_replacement_g1",
                objective="replace failed route",
                replaces_node_ids=("route_g0",),
                frozen_dependency_ids=("weather_g0",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_replacement_g1",),
                frozen_result_refs=("weather_g0",),
            ),
        ),
    )

    assert _admit_recovery(proposal) is proposal


def test_first_successful_admission_freezes_only_plan_scope() -> None:
    """Catches freezing the full accumulated Skill/reference/Tool inventory."""

    graph = build_planning_graph(
        object(),
        _UnusedFastAgent(),
        tools=(
            _probe_tool("tool-a"),
            _probe_tool("tool-b"),
            _probe_tool("evidence-tool"),
            _probe_tool("load_skill_reference"),
        ),
        skill_catalog=SkillCatalog(
            descriptors=[
                SkillDescriptor(
                    name="skill-a",
                    description="skill a",
                    body="skill a body",
                    governed_tools=["tool-a"],
                    references={"guide-a": "references/guide-a.md"},
                ),
                SkillDescriptor(
                    name="skill-b",
                    description="skill b",
                    body="skill b body",
                    governed_tools=["tool-b"],
                    references={"guide-b": "references/guide-b.md"},
                ),
            ]
        ),
    )
    admission_node = graph.get_graph().nodes["admit_plan"].data
    evidence = PlannerEvidence(
        evidence_id="evidence-a",
        tool_name="evidence-tool",
        status="succeeded",
        content="evidence sentinel",
    )
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="worker-a",
                objective="worker a",
                required_skill_ids=("skill-a",),
                allowed_tool_names=("tool-a", "load_skill_reference"),
                evidence_refs=(evidence.evidence_id,),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("worker-a",),
                evidence_refs=(evidence.evidence_id,),
            ),
        ),
    )
    state = {
        "plan_candidate": proposal,
        "planner_evidence": [evidence],
        "planner_active_skill_ids": ["skill-a", "skill-b"],
        "planner_skill_reference_grants": {
            "skill-a": ["guide-a"],
            "skill-b": ["guide-b"],
        },
        "budget_usage": BudgetUsage(model_calls=1, node_attempts=1),
    }

    update = admission_node.invoke(state)

    assert update["plan"] is proposal
    assert update["authorization_envelope"] == PlanningAuthorizationEnvelope(
        skill_ids=("skill-a",),
        reference_grants=(
            SkillReferenceGrant(skill_id="skill-a", reference_ids=("guide-a",)),
        ),
        tool_names=("evidence-tool", "load_skill_reference", "tool-a"),
    )
    assert "tool-b" not in update["authorization_envelope"].tool_names
    assert "skill-b" not in update["authorization_envelope"].skill_ids


def test_generation_zero_revision_does_not_freeze_failed_candidate_scope() -> None:
    """Catches a rejected initial candidate preventing a bounded correction."""

    graph = build_planning_graph(
        object(),
        _UnusedFastAgent(),
        tools=(_probe_tool("tool-a"),),
        skill_catalog=SkillCatalog(),
    )
    admission_node = graph.get_graph().nodes["admit_plan"].data
    invalid = _many_worker_proposal(1).model_copy(
        update={
            "nodes": (
                NativePlanNode(
                    node_id="worker-0",
                    objective="invalid tool",
                    allowed_tool_names=("unknown-tool",),
                ),
            )
        }
    )

    rejected = admission_node.invoke({"plan_candidate": invalid})

    assert rejected == {"admission_error": "unknown_tool", "revision_count": 1}
    assert "authorization_envelope" not in rejected


def test_initial_plan_rejects_minimum_worker_and_finalizer_demand_over_cap() -> None:
    """Catches admission starting 31 workers after the planner spent one node slot."""

    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            _many_worker_proposal(31),
            policy=_policy(),
            evidence=(),
            active_skill_ids=(),
            budget_policy=_node_bound_policy(),
            budget_usage=BudgetUsage(model_calls=1, node_attempts=1),
        )

    assert caught.value.code == "insufficient_graph_budget"


def test_initial_plan_accepts_exact_remaining_minimum_demand() -> None:
    """Catches an off-by-one that withholds the finalizer slot twice."""

    proposal = _many_worker_proposal(30)
    assert (
        admit_native_plan(
            proposal,
            policy=_policy(),
            evidence=(),
            active_skill_ids=(),
            budget_policy=_node_bound_policy(),
            budget_usage=BudgetUsage(model_calls=1, node_attempts=1),
        )
        is proposal
    )


def test_recovery_plan_uses_nondefault_policy_and_existing_usage() -> None:
    """Catches recovery admission recomputing limits from default B or zero usage."""

    policy = PlanningBudgetPolicy(
        base=2,
        graph_tool_limit=16,
        graph_model_limit=9,
        graph_node_attempt_limit=12,
    )
    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            _replacement_plan("route_g0"),
            policy=_policy(),
            evidence=(),
            active_skill_ids=(),
            plan_generation=1,
            historical_node_ids={"route_g0"},
            replannable_node_ids={"route_g0"},
            budget_policy=policy,
            budget_usage=BudgetUsage(model_calls=8, node_attempts=8, replans=1),
        )

    assert caught.value.code == "insufficient_graph_budget"


def test_admission_counts_active_reservations_independent_of_mapping_order() -> None:
    """Catches replay feasibility depending on parallel reservation arrival order."""

    first = _reservation("reserved-a")
    second = _reservation("reserved-b")
    for reservations in (
        {first.execution_id: first, second.execution_id: second},
        {second.execution_id: second, first.execution_id: first},
    ):
        with pytest.raises(NativePlanAdmissionError) as caught:
            admit_native_plan(
                _many_worker_proposal(1),
                policy=_policy(),
                evidence=(),
                active_skill_ids=(),
                budget_policy=PlanningBudgetPolicy.from_base(1),
                budget_usage=BudgetUsage(model_calls=7, node_attempts=28),
                wave_reservations=reservations,
            )
        assert caught.value.code == "insufficient_graph_budget"


def test_zero_node_plan_reserves_one_model_and_node_for_finalizer() -> None:
    """Catches evidence-only plans bypassing the finalizer minimum demand."""

    evidence = PlannerEvidence(
        evidence_id="evidence-final",
        tool_name="weather_probe",
        status="succeeded",
        content="weather-sentinel",
    )
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer from evidence",
                evidence_refs=(evidence.evidence_id,),
            ),
        ),
    )
    admission_policy = PlanningAdmissionPolicy(
        inventory_tool_names=frozenset({"weather_probe"}),
        governed_tool_skills={},
    )
    budget_policy = PlanningBudgetPolicy.from_base(1)

    assert (
        admit_native_plan(
            proposal,
            policy=admission_policy,
            evidence=(evidence,),
            active_skill_ids=(),
            budget_policy=budget_policy,
            budget_usage=BudgetUsage(model_calls=9, node_attempts=31),
        )
        is proposal
    )
    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            proposal,
            policy=admission_policy,
            evidence=(evidence,),
            active_skill_ids=(),
            budget_policy=budget_policy,
            budget_usage=BudgetUsage(model_calls=10, node_attempts=31),
        )
    assert caught.value.code == "insufficient_graph_budget"


@pytest.mark.parametrize(
    ("replacement", "code"),
    (("weather_g0", "replace_frozen_result"), ("unknown_g0", "unknown_replacement")),
)
def test_recovery_plan_rejects_illegal_replacement(
    replacement: str, code: str
) -> None:
    with pytest.raises(NativePlanAdmissionError) as caught:
        _admit_recovery(_replacement_plan(replacement))

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("proposal_factory", "code"),
    (
        (
            lambda: NativePlanProposal(
                schema_version="native_plan_v2",
                nodes=(NativePlanNode(node_id="route_g0", objective="reused id"),),
                deliverables=(
                    PlanDeliverable(
                        deliverable_id="answer",
                        description="answer",
                        producer_node_ids=("route_g0",),
                    ),
                ),
            ),
            "reused_node_id",
        ),
        (
            lambda: NativePlanProposal(
                schema_version="native_plan_v2",
                nodes=(
                    NativePlanNode(
                        node_id="route_g1",
                        objective="depends on frozen result as a current node",
                        depends_on=("weather_g0",),
                    ),
                ),
                deliverables=(
                    PlanDeliverable(
                        deliverable_id="answer",
                        description="answer",
                        producer_node_ids=("route_g1",),
                    ),
                ),
            ),
            "unknown_dependency",
        ),
        (
            lambda: NativePlanProposal(
                schema_version="native_plan_v2",
                nodes=(
                    NativePlanNode(
                        node_id="route_g1",
                        objective="unknown frozen dependency",
                        frozen_dependency_ids=("unknown_g0",),
                    ),
                ),
                deliverables=(
                    PlanDeliverable(
                        deliverable_id="answer",
                        description="answer",
                        producer_node_ids=("route_g1",),
                    ),
                ),
            ),
            "unknown_frozen_dependency",
        ),
        (
            lambda: NativePlanProposal(
                schema_version="native_plan_v2",
                nodes=(NativePlanNode(node_id="route_g1", objective="route"),),
                deliverables=(
                    PlanDeliverable(
                        deliverable_id="answer",
                        description="answer",
                        frozen_result_refs=("unknown_g0",),
                    ),
                ),
            ),
            "unknown_frozen_deliverable_ref",
        ),
    ),
    ids=(
        "reused-node-id",
        "depends-on-frozen",
        "unknown-frozen-dependency",
        "unknown-deliverable-frozen-ref",
    ),
)
def test_recovery_plan_rejects_invalid_generation_references(
    proposal_factory, code: str
) -> None:
    with pytest.raises(NativePlanAdmissionError) as caught:
        _admit_recovery(proposal_factory())

    assert caught.value.code == code


def test_recovery_plan_rejects_duplicate_replacement() -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="route_replacement_a_g1",
                objective="first replacement",
                replaces_node_ids=("route_g0",),
            ),
            NativePlanNode(
                node_id="route_replacement_b_g1",
                objective="second replacement",
                replaces_node_ids=("route_g0",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_replacement_a_g1",),
            ),
        ),
    )

    with pytest.raises(NativePlanAdmissionError) as caught:
        _admit_recovery(proposal)

    assert caught.value.code == "duplicate_replacement"


def test_later_generation_cannot_reclaim_a_historical_node() -> None:
    """Catches claim uniqueness resetting when a new generation is admitted."""

    first_claim = ReplacementClaim(
        replaced_node_id="route_g0",
        replacement_node_id="route_replacement_g1",
        plan_generation=1,
    )
    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            NativePlanProposal(
                schema_version="native_plan_v2",
                nodes=(
                    NativePlanNode(
                        node_id="route_replacement_g2",
                        objective="claim route again",
                        replaces_node_ids=("route_g0",),
                    ),
                ),
                deliverables=(
                    PlanDeliverable(
                        deliverable_id="answer",
                        description="answer",
                        producer_node_ids=("route_replacement_g2",),
                    ),
                ),
            ),
            policy=_policy(),
            evidence=(),
            active_skill_ids=(),
            plan_generation=2,
            historical_node_ids={"route_g0", "route_replacement_g1"},
            replannable_node_ids={"route_g0"},
            replacement_claims={"route_g0": first_claim},
        )

    assert caught.value.code == "duplicate_replacement"


def test_same_historical_replacement_claim_is_replay_idempotent() -> None:
    """Catches checkpoint replay rejecting the exact already-admitted claim."""

    proposal = _replacement_plan("route_g0")
    claim = ReplacementClaim(
        replaced_node_id="route_g0",
        replacement_node_id="route_replacement_g1",
        plan_generation=1,
    )

    admitted = admit_native_plan(
        proposal,
        policy=_policy(),
        evidence=(),
        active_skill_ids=(),
        plan_generation=1,
        historical_node_ids={"route_g0"},
        replannable_node_ids={"route_g0"},
        replacement_claims={"route_g0": claim},
    )

    assert admitted is proposal


@pytest.mark.parametrize("expansion", ("skill", "tool", "reference"))
def test_later_plan_cannot_expand_first_admitted_authorization(
    expansion: str,
) -> None:
    """Catches replan adding a new Skill, Tool, or reference grant."""

    envelope = PlanningAuthorizationEnvelope(
        skill_ids=("skill-a",),
        reference_grants=(
            SkillReferenceGrant(skill_id="skill-a", reference_ids=("guide-a",)),
        ),
        tool_names=("load_skill_reference", "tool-a"),
    )
    required_skills = ("skill-a", "skill-b") if expansion == "skill" else ("skill-a",)
    allowed_tools = (
        ("load_skill_reference", "tool-a", "tool-b")
        if expansion == "tool"
        else ("load_skill_reference", "tool-a")
    )
    reference_grants = {
        "skill-a": ["guide-a", "guide-b"]
        if expansion == "reference"
        else ["guide-a"],
        "skill-b": ["guide-b"],
    }
    policy = PlanningAdmissionPolicy(
        inventory_tool_names=frozenset(
            {"load_skill_reference", "tool-a", "tool-b"}
        ),
        governed_tool_skills={
            "tool-a": frozenset({"skill-a"}),
        },
    )
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="route-g1",
                objective="route",
                required_skill_ids=required_skills,
                allowed_tool_names=allowed_tools,
                replaces_node_ids=("route-g0",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route-g1",),
            ),
        ),
    )

    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            proposal,
            policy=policy,
            evidence=(),
            active_skill_ids=("skill-a", "skill-b"),
            active_reference_grants=reference_grants,
            plan_generation=1,
            historical_node_ids={"route-g0"},
            replannable_node_ids={"route-g0"},
            authorization_envelope=envelope,
        )

    assert caught.value.code == "authorization_expansion"


def test_later_plan_may_narrow_first_admitted_authorization() -> None:
    """Catches subset replans being rejected as if the envelope must stay equal."""

    envelope = PlanningAuthorizationEnvelope(
        skill_ids=("skill-a",),
        reference_grants=(
            SkillReferenceGrant(skill_id="skill-a", reference_ids=("guide-a",)),
        ),
        tool_names=("load_skill_reference", "tool-a"),
    )
    proposal = _replacement_plan("route_g0")

    assert (
        admit_native_plan(
            proposal,
            policy=PlanningAdmissionPolicy(
                inventory_tool_names=frozenset(
                    {"load_skill_reference", "tool-a"}
                ),
                governed_tool_skills={"tool-a": frozenset({"skill-a"})},
            ),
            evidence=(),
            active_skill_ids=("skill-a",),
            active_reference_grants={"skill-a": ["guide-a"]},
            plan_generation=1,
            historical_node_ids={"route_g0"},
            replannable_node_ids={"route_g0"},
            authorization_envelope=envelope,
        )
        is proposal
    )


def test_unknown_frozen_ref_precedes_worker_tool_validation() -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="route_g1",
                objective="invalid frozen ref must win over a later invalid Tool",
                allowed_tool_names=("unknown_probe",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_g1",),
                frozen_result_refs=("unknown_g0",),
            ),
        ),
    )

    with pytest.raises(NativePlanAdmissionError) as caught:
        _admit_recovery(proposal)

    assert caught.value.code == "unknown_frozen_deliverable_ref"


def test_recovery_plan_rejects_duplicate_frozen_deliverable_refs() -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(NativePlanNode(node_id="route_g1", objective="route"),),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_g1",),
                frozen_result_refs=("weather_g0", "weather_g0"),
            ),
        ),
    )

    with pytest.raises(NativePlanAdmissionError) as caught:
        _admit_recovery(proposal)

    assert caught.value.code == "duplicate_deliverable_frozen_result_ref"


def test_initial_plan_rejects_node_id_from_history() -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(NativePlanNode(node_id="route_g0", objective="reused id"),),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_g0",),
            ),
        ),
    )

    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            proposal,
            policy=_policy(),
            evidence=(),
            active_skill_ids=(),
            historical_node_ids={"route_g0"},
        )

    assert caught.value.code == "reused_node_id"


@pytest.mark.parametrize(
    ("node_factory", "deliverable_factory", "code"),
    (
        (
            lambda: NativePlanNode(
                node_id="route_g0",
                objective="initial cannot replace",
                replaces_node_ids=("old_g0",),
            ),
            lambda: PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_g0",),
            ),
            "initial_replacement_forbidden",
        ),
        (
            lambda: NativePlanNode(
                node_id="route_g0",
                objective="initial cannot use frozen results",
                frozen_dependency_ids=("weather_g0",),
            ),
            lambda: PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route_g0",),
            ),
            "initial_replacement_forbidden",
        ),
        (
            lambda: NativePlanNode(node_id="route_g0", objective="initial route"),
            lambda: PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                frozen_result_refs=("weather_g0",),
            ),
            "initial_replacement_forbidden",
        ),
    ),
    ids=("replacement", "frozen-dependency", "frozen-deliverable-ref"),
)
def test_initial_plan_forbids_recovery_references(
    node_factory, deliverable_factory, code: str
) -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(node_factory(),),
        deliverables=(deliverable_factory(),),
    )

    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            proposal,
            policy=_policy(),
            evidence=(),
            active_skill_ids=(),
        )

    assert caught.value.code == code


def _many_worker_proposal(count: int) -> NativePlanProposal:
    node_ids = tuple(f"worker-{index}" for index in range(count))
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=tuple(
            NativePlanNode(node_id=node_id, objective=f"objective-{index}")
            for index, node_id in enumerate(node_ids)
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer sentinel",
                producer_node_ids=node_ids,
            ),
        ),
    )


def _reservation(work_item_id: str) -> WaveReservation:
    return WaveReservation(
        execution_id=f"g0:{work_item_id}:a1",
        plan_generation=0,
        work_item_id=work_item_id,
        attempt=1,
        allowance=BudgetUsage(model_calls=1, node_attempts=1),
    )


def _node_bound_policy() -> PlanningBudgetPolicy:
    return PlanningBudgetPolicy(
        base=1,
        graph_tool_limit=8,
        graph_model_limit=32,
        graph_node_attempt_limit=32,
    )


def _probe_tool(name: str) -> StructuredTool:
    def probe() -> str:
        """Return one offline sentinel."""

        return "probe-sentinel"

    return StructuredTool.from_function(probe, name=name)


class _UnusedFastAgent:
    name = "AssistantFastAgent"

    async def ainvoke(self, input: dict[str, Any], *, context: Any):
        raise AssertionError("admission test must not invoke the fast agent")
