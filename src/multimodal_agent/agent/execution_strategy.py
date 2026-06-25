"""Execution strategy selection for agent runs."""

from enum import Enum
from typing import Literal

from multimodal_agent.schemas.requests import UserRequest


ExecutionStrategyName = Literal["react", "plan_and_solve"]


class ExecutionStrategy(str, Enum):
    """Supported assistant execution strategies."""

    REACT = "react"
    PLAN_AND_SOLVE = "plan_and_solve"


def resolve_execution_strategy(request: UserRequest) -> ExecutionStrategyName:
    """Resolve the per-request execution strategy with a safe default."""

    value = request.execution_strategy or request.metadata.get("execution_strategy")
    if value == ExecutionStrategy.PLAN_AND_SOLVE.value:
        return "plan_and_solve"
    return "react"
