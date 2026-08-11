"""Deterministic workflow constraint ownership helpers."""

from __future__ import annotations

from collections.abc import Iterable

from assistant_agent.workflows.models import (
    WorkflowConstraintBinding,
    WorkflowConstraintProposal,
    WorkflowWorkItem,
)


def resolve_constraint_bindings(
    *,
    constraints: Iterable[str],
    work_items: list[WorkflowWorkItem],
    proposal_bindings: Iterable[WorkflowConstraintProposal] = (),
    definition_bindings: Iterable[WorkflowConstraintProposal] = (),
) -> list[WorkflowConstraintBinding]:
    """Admit planner bindings and bind any remaining prose constraints."""

    proposals = _deduplicate_constraint_proposals(
        [*definition_bindings, *proposal_bindings]
    )
    bindings = [
        _with_inferred_verifier(binding, work_items)
        for binding in proposals
    ]
    bound_statements = {item.statement for item in bindings}
    terminal_ids = _terminal_work_item_ids(work_items)
    verifier_id = terminal_ids[-1]
    existing_ids = {item.constraint_id for item in bindings}
    for index, statement in enumerate(constraints, start=1):
        if statement in bound_statements:
            continue
        constraint_id = _available_constraint_id(index, existing_ids)
        existing_ids.add(constraint_id)
        bindings.append(WorkflowConstraintBinding(
            constraint_id=constraint_id,
            statement=statement,
            owner_work_item_ids=list(terminal_ids),
            verifier_work_item_id=verifier_id,
            severity="required",
        ))
    return bindings


def _deduplicate_constraint_proposals(
    proposals: Iterable[WorkflowConstraintProposal],
) -> list[WorkflowConstraintProposal]:
    admitted: list[WorkflowConstraintProposal] = []
    by_id: dict[str, WorkflowConstraintProposal] = {}
    statements: set[str] = set()
    for proposal in proposals:
        candidate = proposal.model_copy(deep=True)
        existing = by_id.get(candidate.constraint_id)
        if existing is not None:
            if existing.statement != candidate.statement:
                raise ValueError(
                    f"conflicting workflow constraint id: {candidate.constraint_id}"
                )
            continue
        if candidate.statement in statements:
            by_id[candidate.constraint_id] = candidate
            continue
        admitted.append(candidate)
        by_id[candidate.constraint_id] = candidate
        statements.add(candidate.statement)
    return admitted


def assigned_constraints(
    bindings: Iterable[WorkflowConstraintBinding],
    *,
    work_item_id: str,
) -> list[WorkflowConstraintBinding]:
    return [
        binding.model_copy(deep=True)
        for binding in bindings
        if work_item_id in binding.owner_work_item_ids
        or work_item_id == binding.verifier_work_item_id
    ]


def _terminal_work_item_ids(work_items: list[WorkflowWorkItem]) -> list[str]:
    dependency_ids = {
        dependency
        for item in work_items
        for dependency in item.depends_on
    }
    terminals = [
        item.work_item_id
        for item in work_items
        if item.work_item_id not in dependency_ids
    ]
    return terminals or [work_items[-1].work_item_id]


def _with_inferred_verifier(
    binding: WorkflowConstraintProposal,
    work_items: list[WorkflowWorkItem],
) -> WorkflowConstraintBinding:
    verifier_id = binding.verifier_work_item_id
    if binding.severity == "required" and verifier_id is None:
        verifier_id = _infer_verifier_id(
            work_items,
            owner_work_item_ids=binding.owner_work_item_ids,
        )
    return WorkflowConstraintBinding.model_validate(
        {
            **binding.model_dump(mode="python"),
            "verifier_work_item_id": verifier_id,
        }
    )


def _infer_verifier_id(
    work_items: list[WorkflowWorkItem],
    *,
    owner_work_item_ids: list[str],
) -> str | None:
    """Select a deterministic common descendant after the concrete DAG exists."""

    item_by_id = {item.work_item_id: item for item in work_items}
    if not set(owner_work_item_ids).issubset(item_by_id):
        return None
    outgoing: dict[str, list[str]] = {item_id: [] for item_id in item_by_id}
    for item in work_items:
        for dependency in item.depends_on:
            if dependency in outgoing:
                outgoing[dependency].append(item.work_item_id)

    candidates = [
        item
        for item in work_items
        if all(
            _is_reachable(outgoing, owner_id, item.work_item_id)
            for owner_id in owner_work_item_ids
        )
    ]
    if not candidates:
        return None
    verify_candidates = [item for item in candidates if item.kind == "verify"]
    if verify_candidates:
        return verify_candidates[0].work_item_id
    terminal_ids = set(_terminal_work_item_ids(work_items))
    terminal_candidates = [
        item for item in candidates if item.work_item_id in terminal_ids
    ]
    return (terminal_candidates or candidates)[-1].work_item_id


def _is_reachable(
    outgoing: dict[str, list[str]],
    start_id: str,
    target_id: str,
) -> bool:
    if start_id == target_id:
        return True
    visited: set[str] = set()
    pending = list(outgoing.get(start_id, []))
    while pending:
        candidate = pending.pop()
        if candidate == target_id:
            return True
        if candidate in visited:
            continue
        visited.add(candidate)
        pending.extend(outgoing.get(candidate, []))
    return False


def _available_constraint_id(index: int, existing_ids: set[str]) -> str:
    candidate = f"constraint-{index}"
    while candidate in existing_ids:
        index += 1
        candidate = f"constraint-{index}"
    return candidate
