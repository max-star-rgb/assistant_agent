"""Deterministic, checkpoint-safe recovery decisions for planning nodes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError

import httpx
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp, NodeCancelledError

from assistant_agent.native_agent.models import (
    BudgetUsage,
    PlannerOutcome,
    RecoveryDecision,
)
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.state import PlanningState


class PlannerPropagationError(RuntimeError):
    """A stable planner boundary error without provider exception references."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
    if (
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
    historical_node_ids = tuple(
        node.node_id for node in getattr(state.get("plan"), "nodes", ())
    )
    return {
        "plan_generation": generation,
        "planner_attempt_count": 0,
        "plan_candidate": None,
        "planner_outcome": None,
        "admission_error": None,
        "recovery_decision": None,
        "recovery_context": {
            "failure_code": decision.reason_code,
            "planner_evidence_ids": list(_evidence_ids(state)),
            "plan_generation": generation,
            "remaining_replans": policy.max_replans - _budget_usage(state).replans - 1,
        },
        "recovery_history": _bounded_history(
            state.get("recovery_history", ()),
            decision,
            limit=policy.recovery_history_limit,
        ),
        "budget_usage": BudgetUsage(replans=1),
        "historical_node_ids": list(historical_node_ids),
    }


def controlled_finalize_node(state: PlanningState) -> dict[str, object]:
    """Produce a local terminal message without exposing provider error payloads."""

    decision = _recovery_decision(state)
    reason_code = (
        decision.reason_code if decision is not None else "planner_recovery_unavailable"
    )
    return {"messages": [AIMessage(content=f"Planning stopped: {reason_code}.")]}


def _bounded_history(
    existing: Sequence[RecoveryDecision] | Sequence[Mapping[str, object]],
    decision: RecoveryDecision,
    *,
    limit: int,
) -> list[RecoveryDecision]:
    if limit <= 0:
        raise ValueError("recovery history limit must be positive")
    history = [RecoveryDecision.model_validate(item) for item in existing]
    return [*history, decision][-limit:]


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
    "classify_operational_failure",
    "controlled_finalize_node",
    "prepare_replan_node",
    "route_after_planner_assessment",
    "PlannerPropagationError",
    "sanitize_planner_propagation",
]
