"""Temporary RED/GREEN coverage for generation-aware plan admission."""

from __future__ import annotations

import pytest

from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
)
from assistant_agent.native_agent.planning_graph import (
    NativePlanAdmissionError,
    PlanningAdmissionPolicy,
    admit_native_plan,
)


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
