"""Phase-scoped model and Tool budgets for the shared planning agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware import hook_config
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from assistant_agent.native_agent.models import BudgetUsage
from assistant_agent.native_agent.state import AgentPhase


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


class PhaseBudgetMiddleware(AgentMiddleware):
    """End a phase cleanly when its model or Tool allowance is exhausted."""

    def __init__(self, policy: PlanningBudgetPolicy) -> None:
        super().__init__()
        self.policy = policy

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        del runtime
        phase = _agent_phase(state)
        current = int(state.get("phase_model_call_count", 0))
        if current + 1 > self.policy.phase_limits(phase).model_calls:
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
        pending_calls = _last_ai_tool_calls(state.get("messages", ()))
        if not pending_calls:
            return None
        phase = _agent_phase(state)
        current = int(state.get("phase_tool_call_count", 0))
        if current + len(pending_calls) > self.policy.phase_limits(phase).tool_calls:
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


def _last_ai_tool_calls(messages: object) -> list[dict[str, Any]]:
    if not isinstance(messages, (list, tuple)):
        return []
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return list(message.tool_calls)
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


__all__ = ["PhaseBudgetMiddleware", "PhaseLimits", "PlanningBudgetPolicy"]
