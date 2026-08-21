"""Phase-scoped model and Tool budgets for the shared planning agent."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from langchain.agents.middleware import hook_config
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.native_agent.models import BudgetUsage


AgentPhase = Literal["fast", "planner", "worker", "finalizer"]


@dataclass(frozen=True)
class PhaseLimits:
    """The maximum Tool and model calls allowed in one agent phase attempt."""

    tool_calls: int
    model_calls: int


@dataclass(frozen=True)
class PlanningBudgetPolicy:
    """Trusted bounds shared by the planning graph and its reused fast agent."""

    base: int
    graph_tool_limit: int
    graph_model_limit: int
    graph_node_attempt_limit: int
    planner_attempts: int = 2
    worker_attempts: int = 3
    max_replans: int = 2
    recovery_history_limit: int = 32

    def __post_init__(self) -> None:
        if self.base <= 0:
            raise ValueError("planning budget base must be positive")
        if (
            min(
                self.graph_tool_limit,
                self.graph_model_limit,
                self.graph_node_attempt_limit,
                self.planner_attempts,
                self.worker_attempts,
            )
            <= 0
        ):
            raise ValueError("planning execution budgets must be positive")
        if self.max_replans < 0:
            raise ValueError("planning replan budget must be nonnegative")
        if not 1 <= self.recovery_history_limit <= 32:
            raise ValueError("planning recovery history limit must be within 1..32")

    @classmethod
    def from_base(cls, base: int) -> "PlanningBudgetPolicy":
        if base <= 0:
            raise ValueError("planning budget base must be positive")
        return cls(base, 8 * base, 10 * base, 32)

    def phase_limits(self, phase: AgentPhase) -> PhaseLimits:
        return {
            "fast": PhaseLimits(self.base, self.base + 1),
            "planner": PhaseLimits(2 * self.base, 2 * self.base + 1),
            "worker": PhaseLimits(self.base, self.base + 1),
            "finalizer": PhaseLimits(0, 1),
        }[phase]


class WaveReservation(BaseModel):
    """Immutable allowance for one deterministic worker execution identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: str
    plan_generation: int = Field(ge=0)
    work_item_id: str
    attempt: int = Field(ge=1)
    allowance: BudgetUsage

    @model_validator(mode="after")
    def _identity_matches_fields(self) -> "WaveReservation":
        expected = f"g{self.plan_generation}:{self.work_item_id}:a{self.attempt}"
        if self.execution_id != expected:
            raise ValueError("wave reservation execution_id does not match fields")
        if self.allowance.node_attempts != 1 or self.allowance.replans != 0:
            raise ValueError("wave reservation requires exactly one worker attempt")
        return self

    @property
    def model_calls(self) -> int:
        return self.allowance.model_calls

    @property
    def tool_calls(self) -> int:
        return self.allowance.tool_calls

    @property
    def node_attempts(self) -> int:
        return self.allowance.node_attempts


def remaining_budget(
    usage: BudgetUsage | Mapping[str, object] | None,
    policy: PlanningBudgetPolicy,
    *,
    reservations: Mapping[str, WaveReservation | Mapping[str, object]] | None = None,
    reconciled_execution_ids: Collection[str] = (),
) -> BudgetUsage:
    """Return graph capacity after actual usage and active reservations."""

    consumed = BudgetUsage.model_validate(usage or {})
    reconciled = frozenset(reconciled_execution_ids)
    reserved_model = 0
    reserved_tool = 0
    reserved_attempts = 0
    for key, raw in (reservations or {}).items():
        reservation = WaveReservation.model_validate(raw)
        if key != reservation.execution_id:
            raise ValueError("wave reservation key does not match execution_id")
        if key in reconciled:
            continue
        reserved_model += reservation.allowance.model_calls
        reserved_tool += reservation.allowance.tool_calls
        reserved_attempts += reservation.allowance.node_attempts
    return BudgetUsage(
        model_calls=max(
            policy.graph_model_limit - consumed.model_calls - reserved_model,
            0,
        ),
        tool_calls=max(
            policy.graph_tool_limit - consumed.tool_calls - reserved_tool,
            0,
        ),
        node_attempts=max(
            policy.graph_node_attempt_limit
            - consumed.node_attempts
            - reserved_attempts,
            0,
        ),
        replans=max(policy.max_replans - consumed.replans, 0),
    )


class PhaseBudgetMiddleware(AgentMiddleware):
    """End a phase cleanly when its model or Tool allowance is exhausted."""

    def __init__(
        self,
        policy: PlanningBudgetPolicy,
        *,
        business_tool_names: frozenset[str],
    ) -> None:
        super().__init__()
        self.policy = policy
        self.business_tool_names = business_tool_names

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        del runtime
        phase = _agent_phase(state)
        current = int(state.get("phase_model_call_count", 0))
        limits = _phase_limits(state, policy=self.policy, phase=phase)
        if current + 1 > limits.model_calls:
            return _model_budget_end_update(phase=phase, current=current)
        return {
            "phase_model_call_count": current + 1,
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        del runtime
        pending_calls = _pending_business_tool_calls(
            state.get("messages", ()),
            business_tool_names=self.business_tool_names,
        )
        if not pending_calls:
            return None
        phase = _agent_phase(state)
        current = int(state.get("phase_tool_call_count", 0))
        limits = _phase_limits(state, policy=self.policy, phase=phase)
        if current + len(pending_calls) > limits.tool_calls:
            return _tool_budget_end_update(
                phase=phase,
                current=current,
                pending_calls=pending_calls,
            )
        return {
            "phase_tool_call_count": current + len(pending_calls),
            "phase_budget_usage": BudgetUsage(tool_calls=len(pending_calls)),
        }

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


def _agent_phase(state: dict[str, Any]) -> AgentPhase:
    phase = state.get("agent_phase", "fast")
    if phase in {"fast", "planner", "worker", "finalizer"}:
        return cast(AgentPhase, phase)
    return "fast"


def _phase_limits(
    state: Mapping[str, object],
    *,
    policy: PlanningBudgetPolicy,
    phase: AgentPhase,
) -> PhaseLimits:
    configured = policy.phase_limits(phase)
    raw_allowance = state.get("phase_budget_allowance")
    if raw_allowance is None:
        return configured
    allowance = BudgetUsage.model_validate(raw_allowance)
    return PhaseLimits(
        tool_calls=min(configured.tool_calls, allowance.tool_calls),
        model_calls=min(configured.model_calls, allowance.model_calls),
    )


def _pending_business_tool_calls(
    messages: object,
    *,
    business_tool_names: frozenset[str],
) -> list[dict[str, Any]]:
    if not isinstance(messages, (list, tuple)):
        return []
    closed_call_ids = {
        message.tool_call_id for message in messages if isinstance(message, ToolMessage)
    }
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return [
                call
                for call in message.tool_calls
                if call.get("name") in business_tool_names
                and call.get("id") not in closed_call_ids
            ]
    return []


def _model_budget_end_update(*, phase: AgentPhase, current: int) -> dict[str, Any]:
    return {
        "jump_to": "end",
        "phase_model_call_count": current,
        "phase_budget_status": "exhausted",
        "messages": [AIMessage(content=f"{phase} phase budget exhausted.")],
    }


def _tool_budget_end_update(
    *,
    phase: AgentPhase,
    current: int,
    pending_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    messages: list[BaseMessage] = [
        ToolMessage(
            content="Tool execution was not performed because this phase budget is exhausted.",
            tool_call_id=str(call["id"]),
            name=str(call.get("name", "tool")),
            status="error",
        )
        for call in pending_calls
    ]
    messages.append(AIMessage(content=f"{phase} phase budget exhausted."))
    return {
        "jump_to": "end",
        "phase_tool_call_count": current,
        "phase_budget_status": "exhausted",
        "messages": messages,
    }


__all__ = [
    "PhaseBudgetMiddleware",
    "PhaseLimits",
    "PlanningBudgetPolicy",
    "WaveReservation",
    "remaining_budget",
]
