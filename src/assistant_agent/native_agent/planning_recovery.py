"""Deterministic, checkpoint-safe recovery decisions for planning nodes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from urllib.error import HTTPError, URLError

import httpx
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp, NodeCancelledError

from assistant_agent.native_agent.models import (
    BudgetUsage,
    NativePlanProposal,
    PlannerOutcome,
    RecoveryDecision,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.state import PlanningState


class PlannerPropagationError(RuntimeError):
    """A stable planner boundary error without provider exception references."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkerPropagationError(RuntimeError):
    """A stable worker boundary error without provider exception references."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
        self.__suppress_context__ = True


def classify_operational_failure(error: BaseException) -> bool:
    """Accept only transient transport failures; fail closed for every other error."""

    pending: list[BaseException] = [error]
    visited: set[int] = set()
    operational = False
    while pending:
        current = pending.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(
            current,
            (
                asyncio.CancelledError,
                GraphBubbleUp,
                PermissionError,
                NodeCancelledError,
                AssertionError,
                TypeError,
                ValueError,
                LookupError,
                ArithmeticError,
                ImportError,
                NameError,
                SyntaxError,
            ),
        ):
            return False
        recognized = False
        if isinstance(current, (TimeoutError, ConnectionError, httpx.TransportError)):
            recognized = True
            operational = True
        status_code = _exception_status_code(current)
        if status_code is not None:
            if status_code in {408, 409, 425, 429} or status_code >= 500:
                recognized = True
                operational = True
            else:
                return False
        if isinstance(current, URLError):
            recognized = True
            operational = True
            if isinstance(current.reason, BaseException):
                pending.append(current.reason)
        if not recognized:
            return False
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
        if isinstance(current.__context__, BaseException):
            pending.append(current.__context__)
    return operational


def sanitize_planner_propagation(error: Exception) -> PlannerPropagationError:
    """Erase unsafe exception chains before a nonrecoverable planner failure exits.

    This deliberately recognizes only authorization, contract/schema, nonretryable
    provider status, and an unclassified fail-closed remainder.  It never copies an
    exception message, response, cause, context, or arbitrary provider attribute.
    """

    if isinstance(error, PermissionError):
        return PlannerPropagationError("planner_authorization_failure")
    if isinstance(error, (TypeError, ValueError, AssertionError)):
        return PlannerPropagationError("planner_contract_failure")
    if _exception_status_code(error) is not None:
        return PlannerPropagationError("planner_nonretryable_provider_failure")
    return PlannerPropagationError("planner_unclassified_failure")


def find_worker_control_failure(error: BaseException) -> BaseException | None:
    """Find native control flow hidden in a provider wrapper without converting it."""

    for current in _exception_chain(error):
        if isinstance(
            current,
            (asyncio.CancelledError, GraphBubbleUp, NodeCancelledError),
        ):
            return current
    return None


def sanitize_worker_propagation(error: Exception) -> WorkerPropagationError:
    """Classify nonrecoverable worker failures using stable local codes only."""

    chain = tuple(_exception_chain(error))
    if any(isinstance(item, PermissionError) for item in chain):
        return WorkerPropagationError("worker_authorization_failure")
    if any(
        isinstance(
            item,
            (
                AssertionError,
                TypeError,
                ValueError,
                LookupError,
                ArithmeticError,
                ImportError,
                NameError,
                SyntaxError,
                AttributeError,
            ),
        )
        for item in chain
    ):
        return WorkerPropagationError("worker_contract_failure")
    if any(_exception_status_code(item) is not None for item in chain):
        return WorkerPropagationError("worker_nonretryable_provider_failure")
    return WorkerPropagationError("worker_unclassified_failure")


def _exception_chain(error: BaseException):
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        yield current
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
        if isinstance(current.__context__, BaseException):
            pending.append(current.__context__)


def assess_recovery_budget(
    usage: BudgetUsage | Mapping[str, object],
    policy: PlanningBudgetPolicy,
) -> RecoveryDecision | None:
    """Return the stable controlled-finalize decision for an exhausted cap."""

    consumed = BudgetUsage.model_validate(usage)
    checks = (
        (
            consumed.tool_calls >= policy.graph_tool_limit,
            "graph_tool_budget_exhausted",
        ),
        (
            consumed.model_calls >= policy.graph_model_limit,
            "graph_model_budget_exhausted",
        ),
        (
            consumed.node_attempts >= policy.graph_node_attempt_limit,
            "graph_node_attempt_budget_exhausted",
        ),
        (consumed.replans >= policy.max_replans, "replan_budget_exhausted"),
    )
    for exhausted, reason_code in checks:
        if exhausted:
            return RecoveryDecision(action="finalize", reason_code=reason_code)
    return None


def assess_planner_node(
    state: PlanningState,
    *,
    policy: PlanningBudgetPolicy,
) -> dict[str, object]:
    """Select the next deterministic planner recovery transition."""

    outcome = _planner_outcome(state)
    if outcome is None:
        raise ValueError("planner assessment requires planner_outcome")
    if outcome.status == "succeeded":
        return {"recovery_decision": None}
    failure = outcome.failure
    if failure is None:
        raise ValueError("failed planner outcome requires failure")
    exhausted = _assess_execution_budget(_budget_usage(state), policy)
    if exhausted is not None:
        decision = exhausted
    elif (
        outcome.status == "operational_failed"
        and _planner_attempt_count(state) < policy.planner_attempts
    ):
        decision = RecoveryDecision(action="retry", reason_code=failure.code)
    elif _budget_usage(state).replans >= policy.max_replans:
        decision = RecoveryDecision(
            action="finalize",
            reason_code="planner_recovery_budget_exhausted",
        )
    else:
        decision = RecoveryDecision(action="replan", reason_code=failure.code)
    return {"recovery_decision": decision}


def route_after_planner_assessment(state: PlanningState) -> str:
    """Route successful candidates to admission and failures through recovery."""

    outcome = _planner_outcome(state)
    if outcome is None:
        raise ValueError("planner assessment requires planner_outcome")
    if outcome.status == "succeeded":
        return "admit_plan"
    decision = _recovery_decision(state)
    if decision is None:
        raise ValueError("failed planner outcome requires recovery_decision")
    return {
        "retry": "planner",
        "replan": "prepare_replan",
        "finalize": "controlled_finalize",
        "propagate": "controlled_finalize",
    }[decision.action]


def freeze_successful_worker_results(
    state: Mapping[str, object],
) -> dict[str, WorkerResult]:
    """Return monotonic successful worker results not already frozen."""

    frozen = _frozen_worker_results(state)
    additions: dict[str, WorkerResult] = {}
    outcomes = sorted(
        _worker_outcomes(state).values(),
        key=lambda item: (
            item.plan_generation,
            item.work_item_id,
            item.attempt,
            item.execution_id,
        ),
    )
    for outcome in outcomes:
        if outcome.status != "succeeded" or outcome.result is None:
            continue
        existing = frozen.get(outcome.work_item_id) or additions.get(
            outcome.work_item_id
        )
        if existing is not None and existing != outcome.result:
            raise ValueError("conflicting frozen worker result")
        if existing is None:
            additions[outcome.work_item_id] = outcome.result
    return additions


def assess_workers_node(
    state: PlanningState,
    *,
    policy: PlanningBudgetPolicy,
) -> dict[str, object]:
    """Freeze successes before selecting retry, replan, or completion."""

    plan = _plan(state)
    latest = _latest_current_worker_outcomes(state, plan=plan)
    worker_attempt_limit = min(max(policy.worker_attempts, 1), 3)
    frozen_additions = freeze_successful_worker_results(state)
    combined_frozen = {
        **_frozen_worker_results(state),
        **frozen_additions,
    }
    failures = [
        latest[node.node_id]
        for node in plan.nodes
        if node.node_id in latest and latest[node.node_id].status != "succeeded"
    ]
    terminal_failures = [
        outcome
        for outcome in failures
        if outcome.status in {"budget_exhausted", "business_failed"}
        or (
            outcome.status == "operational_failed"
            and outcome.attempt >= worker_attempt_limit
        )
    ]
    retryable = [
        outcome
        for outcome in failures
        if outcome.status == "operational_failed"
        and outcome.attempt < worker_attempt_limit
    ]
    decision: RecoveryDecision | None = None
    exhausted = _assess_execution_budget(_budget_usage(state), policy)
    if exhausted is not None:
        decision = exhausted
    elif terminal_failures:
        source_ids = tuple(item.execution_id for item in terminal_failures)
        if _budget_usage(state).replans >= policy.max_replans:
            decision = RecoveryDecision(
                action="finalize",
                reason_code="worker_recovery_budget_exhausted",
                source_execution_ids=source_ids,
            )
        else:
            first = terminal_failures[0]
            reason_code = (
                "worker_operational_exhausted"
                if first.status == "operational_failed"
                else first.failure.code
                if first.failure is not None
                else "worker_recovery_required"
            )
            decision = RecoveryDecision(
                action="replan",
                reason_code=reason_code,
                source_execution_ids=source_ids,
            )
    elif retryable:
        decision = RecoveryDecision(
            action="retry",
            reason_code="worker_operational_retry",
            source_execution_ids=tuple(item.execution_id for item in retryable),
        )
    update: dict[str, object] = {
        "frozen_worker_results": frozen_additions,
        "worker_results": _ordered_compatibility_results(
            combined_frozen,
            plan=plan,
        ),
        "recovery_decision": decision,
    }
    if decision is not None and decision.action in {"retry", "finalize"}:
        update["recovery_history"] = _bounded_history(
            state.get("recovery_history", ()),
            decision,
            limit=policy.recovery_history_limit,
        )
    return update


def route_after_worker_assessment(state: PlanningState) -> str:
    """Route the deterministic worker assessment without model judgment."""

    decision = _recovery_decision(state)
    if decision is not None:
        return {
            "retry": "scheduler",
            "replan": "prepare_replan",
            "finalize": "controlled_finalize",
            "propagate": "controlled_finalize",
        }[decision.action]
    plan = _plan(state)
    latest = _latest_current_worker_outcomes(state, plan=plan)
    if all(
        (outcome := latest.get(node.node_id)) is not None
        and outcome.status == "succeeded"
        for node in plan.nodes
    ):
        return "finalize"
    return "scheduler"


def prepare_replan_node(
    state: PlanningState,
    *,
    policy: PlanningBudgetPolicy,
) -> dict[str, object]:
    """Preserve safe planner facts and create the next generation's context."""

    decision = _recovery_decision(state)
    if decision is None or decision.action != "replan":
        raise ValueError("replan preparation requires a replan decision")
    if _budget_usage(state).replans >= policy.max_replans:
        raise ValueError("replan preparation exceeds configured budget")
    generation = _plan_generation(state) + 1
    plan = _optional_plan(state)
    worker_origin = _is_current_worker_replan(
        state,
        decision=decision,
        plan=plan,
    )
    historical_node_ids = (
        tuple(node.node_id for node in plan.nodes)
        if worker_origin and plan is not None
        else ()
    )
    frozen_additions = freeze_successful_worker_results(state) if worker_origin else {}
    frozen_results = {
        **_frozen_worker_results(state),
        **frozen_additions,
    }
    if worker_origin:
        superseded_ids = _superseded_work_item_ids(state, plan=plan)
        unfinished_deliverable_ids = _unfinished_deliverable_ids(
            plan,
            frozen_result_ids=frozen_results,
        )
    else:
        prior_context = state.get("recovery_context")
        prior_context = prior_context if isinstance(prior_context, Mapping) else {}
        superseded_ids = _preserved_ids(
            prior_context.get(
                "replannable_work_item_ids",
                state.get("superseded_work_item_ids", ()),
            ),
            excluded_ids=frozen_results,
        )
        unfinished_deliverable_ids = _preserved_ids(
            prior_context.get("unfinished_deliverable_ids", ()),
        )
    update: dict[str, object] = {
        "plan_generation": generation,
        "planner_attempt_count": 0,
        "revision_count": 0,
        "plan_candidate": None,
        "planner_outcome": None,
        "admission_error": None,
        "recovery_decision": None,
        "recovery_context": {
            "failure_code": decision.reason_code,
            "planner_evidence_ids": list(_evidence_ids(state)),
            "plan_generation": generation,
            "remaining_replans": policy.max_replans - _budget_usage(state).replans - 1,
            "frozen_result_ids": sorted(frozen_results),
            "replannable_work_item_ids": list(superseded_ids),
            "unfinished_deliverable_ids": list(unfinished_deliverable_ids),
        },
        "recovery_history": _bounded_history(
            state.get("recovery_history", ()),
            decision,
            limit=policy.recovery_history_limit,
        ),
        "budget_usage": _add_usage(
            _budget_usage(state),
            BudgetUsage(replans=1),
        ),
        "historical_node_ids": list(historical_node_ids),
    }
    if worker_origin:
        update["superseded_work_item_ids"] = list(superseded_ids)
        update["frozen_worker_results"] = frozen_additions
        update["worker_results"] = _ordered_compatibility_results(
            frozen_results,
            plan=plan,
        )
    return update


def controlled_finalize_node(
    state: PlanningState,
    *,
    policy: PlanningBudgetPolicy | None = None,
) -> dict[str, object]:
    """Produce a local terminal message without exposing provider error payloads."""

    decision = _recovery_decision(state)
    reason_code = (
        decision.reason_code if decision is not None else "planner_recovery_unavailable"
    )
    usage = _budget_usage(state)
    node_attempts = usage.node_attempts + 1
    if policy is not None:
        node_attempts = min(node_attempts, policy.graph_node_attempt_limit)
    return {
        "messages": [AIMessage(content=f"Planning stopped: {reason_code}.")],
        "admission_error": None,
        "budget_usage": usage.model_copy(update={"node_attempts": node_attempts}),
    }


def _bounded_history(
    existing: Sequence[RecoveryDecision] | Sequence[Mapping[str, object]],
    decision: RecoveryDecision,
    *,
    limit: int,
) -> list[RecoveryDecision]:
    if limit <= 0:
        raise ValueError("recovery history limit must be positive")
    history = [RecoveryDecision.model_validate(item) for item in existing]
    if history and history[-1] == decision:
        return history[-limit:]
    return [*history, decision][-limit:]


def _add_usage(left: BudgetUsage, right: BudgetUsage) -> BudgetUsage:
    return BudgetUsage(
        model_calls=left.model_calls + right.model_calls,
        tool_calls=left.tool_calls + right.tool_calls,
        node_attempts=left.node_attempts + right.node_attempts,
        replans=left.replans + right.replans,
    )


def _assess_execution_budget(
    usage: BudgetUsage,
    policy: PlanningBudgetPolicy,
) -> RecoveryDecision | None:
    if usage.node_attempts >= policy.graph_node_attempt_limit - 1:
        return RecoveryDecision(
            action="finalize",
            reason_code="graph_node_attempt_budget_exhausted",
        )
    decision = assess_recovery_budget(usage, policy)
    if decision is not None and decision.reason_code != "replan_budget_exhausted":
        return decision
    return None


def _planner_outcome(state: Mapping[str, object]) -> PlannerOutcome | None:
    value = state.get("planner_outcome")
    return PlannerOutcome.model_validate(value) if value is not None else None


def _recovery_decision(state: Mapping[str, object]) -> RecoveryDecision | None:
    value = state.get("recovery_decision")
    return RecoveryDecision.model_validate(value) if value is not None else None


def _budget_usage(state: Mapping[str, object]) -> BudgetUsage:
    return BudgetUsage.model_validate(state.get("budget_usage") or {})


def _plan_generation(state: Mapping[str, object]) -> int:
    return int(state.get("plan_generation", 0))


def _planner_attempt_count(state: Mapping[str, object]) -> int:
    return int(state.get("planner_attempt_count", 0))


def _plan(state: Mapping[str, object]) -> NativePlanProposal:
    plan = _optional_plan(state)
    if plan is None:
        raise ValueError("worker recovery requires an admitted plan")
    return plan


def _optional_plan(state: Mapping[str, object]) -> NativePlanProposal | None:
    value = state.get("plan")
    return NativePlanProposal.model_validate(value) if value is not None else None


def _worker_outcomes(state: Mapping[str, object]) -> dict[str, WorkerOutcome]:
    raw = state.get("worker_outcomes") or {}
    if not isinstance(raw, Mapping):
        raise TypeError("worker_outcomes must be a mapping")
    return {str(key): WorkerOutcome.model_validate(value) for key, value in raw.items()}


def _frozen_worker_results(
    state: Mapping[str, object],
) -> dict[str, WorkerResult]:
    raw = state.get("frozen_worker_results") or {}
    if not isinstance(raw, Mapping):
        raise TypeError("frozen_worker_results must be a mapping")
    return {str(key): WorkerResult.model_validate(value) for key, value in raw.items()}


def _latest_current_worker_outcomes(
    state: Mapping[str, object],
    *,
    plan: NativePlanProposal,
) -> dict[str, WorkerOutcome]:
    generation = _plan_generation(state)
    current_ids = {node.node_id for node in plan.nodes}
    latest: dict[str, WorkerOutcome] = {}
    for outcome in _worker_outcomes(state).values():
        if (
            outcome.plan_generation != generation
            or outcome.work_item_id not in current_ids
        ):
            continue
        existing = latest.get(outcome.work_item_id)
        if existing is not None and existing.attempt == outcome.attempt:
            if existing != outcome:
                raise ValueError("conflicting worker attempts")
            continue
        if existing is None or outcome.attempt > existing.attempt:
            latest[outcome.work_item_id] = outcome
    return latest


def _is_current_worker_replan(
    state: Mapping[str, object],
    *,
    decision: RecoveryDecision,
    plan: NativePlanProposal | None,
) -> bool:
    if plan is None or not decision.source_execution_ids:
        return False
    historical_ids = frozenset(
        item for item in state.get("historical_node_ids", ()) if isinstance(item, str)
    )
    plan_ids = frozenset(node.node_id for node in plan.nodes)
    if plan_ids & historical_ids:
        return False
    generation = _plan_generation(state)
    outcomes = _worker_outcomes(state)
    sources = [
        outcomes.get(execution_id) for execution_id in decision.source_execution_ids
    ]
    return all(
        outcome is not None
        and outcome.plan_generation == generation
        and outcome.work_item_id in plan_ids
        and outcome.status != "succeeded"
        for outcome in sources
    )


def _preserved_ids(
    values: object,
    *,
    excluded_ids: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    excluded = frozenset(excluded_ids or {})
    return tuple(
        item
        for item in dict.fromkeys(values)
        if isinstance(item, str) and item not in excluded
    )


def _superseded_work_item_ids(
    state: Mapping[str, object],
    *,
    plan: NativePlanProposal | None,
) -> tuple[str, ...]:
    if plan is None:
        return ()
    latest = _latest_current_worker_outcomes(state, plan=plan)
    superseded = {
        node.node_id
        for node in plan.nodes
        if (outcome := latest.get(node.node_id)) is None
        or outcome.status != "succeeded"
    }
    superseded.difference_update(_frozen_worker_results(state))
    changed = True
    while changed:
        changed = False
        for node in plan.nodes:
            if node.node_id in superseded:
                continue
            if any(dependency in superseded for dependency in node.depends_on):
                superseded.add(node.node_id)
                changed = True
    return tuple(node.node_id for node in plan.nodes if node.node_id in superseded)


def _unfinished_deliverable_ids(
    plan: NativePlanProposal | None,
    *,
    frozen_result_ids: Mapping[str, WorkerResult],
) -> tuple[str, ...]:
    if plan is None:
        return ()
    frozen_ids = frozenset(frozen_result_ids)
    return tuple(
        deliverable.deliverable_id
        for deliverable in plan.deliverables
        if not set(deliverable.producer_node_ids).issubset(frozen_ids)
        or not set(deliverable.frozen_result_refs).issubset(frozen_ids)
    )


def _ordered_compatibility_results(
    frozen: Mapping[str, WorkerResult],
    *,
    plan: NativePlanProposal | None,
) -> list[WorkerResult]:
    plan_order = tuple(node.node_id for node in plan.nodes) if plan else ()
    ordered_ids = tuple(dict.fromkeys([*plan_order, *frozen.keys()]))
    return [
        frozen[work_item_id] for work_item_id in ordered_ids if work_item_id in frozen
    ]


def _evidence_ids(state: Mapping[str, object]) -> tuple[str, ...]:
    evidence = state.get("planner_evidence", ())
    return tuple(
        item.evidence_id
        for item in evidence
        if hasattr(item, "evidence_id") and isinstance(item.evidence_id, str)
    )


def _exception_status_code(error: BaseException) -> int | None:
    if isinstance(error, HTTPError):
        return error.code
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


__all__ = [
    "assess_planner_node",
    "assess_workers_node",
    "classify_operational_failure",
    "controlled_finalize_node",
    "freeze_successful_worker_results",
    "find_worker_control_failure",
    "prepare_replan_node",
    "route_after_planner_assessment",
    "route_after_worker_assessment",
    "PlannerPropagationError",
    "sanitize_planner_propagation",
    "sanitize_worker_propagation",
    "WorkerPropagationError",
]
