"""Async application facade for native DurableWorkflowGraph execution."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any, Literal, cast

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.workflows.graph_context import WorkflowGraphRuntimeContext
from assistant_agent.workflows.graph_state import (
    DurableWorkflowState,
    WorkflowBranchInterruptInput,
    validate_durable_workflow_state,
)


class WorkflowGraphExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class WorkflowGraphExecutionIdentity:
    workflow_id: str
    thread_id: str
    run_id: str
    user_id: str
    session_id: str
    agent_id: str

    @classmethod
    def for_workflow(
        cls,
        *,
        workflow_id: str,
        workflow_thread_id: str,
        run_id: str,
        user_id: str,
        session_id: str,
        agent_id: str,
    ) -> "WorkflowGraphExecutionIdentity":
        return cls(
            workflow_id=workflow_id,
            thread_id=workflow_thread_id,
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
        )

    def runnable_config(self) -> dict[str, dict[str, str]]:
        return {
            "configurable": {"thread_id": self.thread_id, "run_id": self.run_id}
        }


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkflowResume(_StrictModel):
    values_by_action_ref: dict[str, dict[str, str]] = Field(
        min_length=1, max_length=32
    )


class WorkflowGraphInterrupt(_StrictModel):
    action_ref: str
    node_id: str
    execution_generation: int
    required_fields: tuple[str, ...]
    prompt_code: str
    safe_prompt: str


@dataclass(frozen=True)
class WorkflowGraphStreamPart:
    type: str
    namespace: tuple[str, ...]
    data: Any


@dataclass(frozen=True)
class WorkflowGraphStreamResult:
    final_state: DurableWorkflowState
    parts: tuple[WorkflowGraphStreamPart, ...]
    status: Literal[
        "completed", "failed", "cancelled", "interrupted", "infrastructure_error"
    ]
    interrupts: tuple[WorkflowGraphInterrupt, ...] = ()


class DurableWorkflowGraphApp:
    def __init__(self, graph: Any) -> None:
        self.graph = graph

    async def astream(
        self,
        input_or_command: DurableWorkflowState | Command[Any],
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
    ) -> AsyncIterator[WorkflowGraphStreamPart]:
        async for raw in self.graph.astream(
            input_or_command,
            config=identity.runnable_config(),
            context=context,
            stream_mode=["values", "updates", "custom", "tasks", "checkpoints"],
            subgraphs=True,
            durability="sync",
            version="v2",
        ):
            yield WorkflowGraphStreamPart(
                type=str(raw["type"]),
                namespace=tuple(raw.get("ns") or ()),
                data=raw.get("data"),
            )

    async def arun(
        self,
        initial_state: DurableWorkflowState,
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
        part_consumer: Callable[[WorkflowGraphStreamPart], object] | None = None,
    ) -> WorkflowGraphStreamResult:
        self._validate_initial(initial_state, identity, context)
        return await self._consume(
            initial_state,
            identity=identity,
            context=context,
            part_consumer=part_consumer,
        )

    async def aresume(
        self,
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
        resume: WorkflowResume,
        part_consumer: Callable[[WorkflowGraphStreamPart], object] | None = None,
    ) -> WorkflowGraphStreamResult:
        snapshot = await self.aget_state(identity)
        state = self._validated_snapshot(snapshot)
        self._validate_owner(state, identity, context)
        if identity.run_id == state["invocation_run_id"]:
            raise WorkflowGraphExecutionError(
                "workflow_resume_run_id_reused",
                "Resume requires a new run_id on the same workflow thread.",
            )
        pending = _pending_interrupts(snapshot)
        by_action: dict[str, tuple[str, WorkflowBranchInterruptInput]] = {}
        for native_id, request in pending:
            previous = by_action.get(request.action_ref)
            if previous is not None and previous != (native_id, request):
                raise WorkflowGraphExecutionError(
                    "workflow_interrupt_action_conflict",
                    "One business action maps to conflicting native interrupts.",
                )
            by_action[request.action_ref] = (native_id, request)
        unknown = set(resume.values_by_action_ref) - set(by_action)
        if unknown:
            raise WorkflowGraphExecutionError(
                "workflow_resume_action_unknown",
                "Resume references an action that is not pending on this workflow.",
            )
        native_resume: dict[str, dict[str, str]] = {}
        for action_ref, fields in resume.values_by_action_ref.items():
            native_id, request = by_action[action_ref]
            if set(fields) != set(request.required_fields):
                raise WorkflowGraphExecutionError(
                    "workflow_resume_fields_invalid",
                    "Resume fields do not match the pending workflow action.",
                )
            native_resume[native_id] = dict(fields)
        return await self._consume(
            Command(resume=native_resume),
            identity=identity,
            context=context,
            part_consumer=part_consumer,
        )

    async def aget_state(self, identity: WorkflowGraphExecutionIdentity) -> Any:
        return await self.graph.aget_state(identity.runnable_config(), subgraphs=True)

    async def aget_state_history(
        self, identity: WorkflowGraphExecutionIdentity, limit: int
    ) -> tuple[Any, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("state history limit must be between 1 and 100")
        return tuple(
            [
                item
                async for item in self.graph.aget_state_history(
                    identity.runnable_config(), limit=limit
                )
            ]
        )

    async def _consume(
        self,
        input_or_command: DurableWorkflowState | Command[Any],
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
        part_consumer: Callable[[WorkflowGraphStreamPart], object] | None,
    ) -> WorkflowGraphStreamResult:
        parts: list[WorkflowGraphStreamPart] = []
        async for part in self.astream(
            input_or_command, identity=identity, context=context
        ):
            parts.append(part)
            if part_consumer is not None:
                part_consumer(part)
        snapshot = await self.aget_state(identity)
        state = self._validated_snapshot(snapshot)
        self._validate_owner(state, identity, context)
        interrupts = tuple(item for _native_id, item in _pending_interrupts(snapshot))
        public = tuple(
            WorkflowGraphInterrupt(
                action_ref=item.action_ref,
                node_id=item.node_id,
                execution_generation=item.execution_generation,
                required_fields=item.required_fields,
                prompt_code=item.prompt_code,
                safe_prompt=item.safe_prompt,
            )
            for item in interrupts
        )
        tasks = tuple(getattr(snapshot, "tasks", ()) or ())
        next_nodes = tuple(getattr(snapshot, "next", ()) or ())
        if public and (tasks or next_nodes):
            status = "interrupted"
        elif tasks or next_nodes:
            status = "infrastructure_error"
        elif public:
            status = "infrastructure_error"
        elif state["status"] in {"completed", "failed", "cancelled"}:
            status = cast(Any, state["status"])
        else:
            status = "infrastructure_error"
        return WorkflowGraphStreamResult(
            final_state=state,
            parts=tuple(parts),
            status=status,
            interrupts=public,
        )

    @staticmethod
    def _validated_snapshot(snapshot: Any) -> DurableWorkflowState:
        values = getattr(snapshot, "values", None)
        if not isinstance(values, Mapping):
            raise WorkflowGraphExecutionError(
                "workflow_checkpoint_not_found", "Workflow checkpoint was not found."
            )
        try:
            return validate_durable_workflow_state(values)
        except Exception as exc:
            raise WorkflowGraphExecutionError(
                "workflow_checkpoint_incompatible",
                "Workflow checkpoint is incompatible with this graph.",
            ) from exc

    @staticmethod
    def _validate_initial(
        state: DurableWorkflowState,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
    ) -> None:
        DurableWorkflowGraphApp._validate_owner(
            validate_durable_workflow_state(state), identity, context
        )
        if state["invocation_run_id"] != identity.run_id:
            raise WorkflowGraphExecutionError(
                "workflow_run_identity_mismatch",
                "Initial graph run_id does not match workflow state.",
            )

    @staticmethod
    def _validate_owner(
        state: DurableWorkflowState,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
    ) -> None:
        owner = state["identity"]
        owner_map = owner.model_dump(mode="python") if hasattr(owner, "model_dump") else owner
        service_owner = context.services.workflow_identity
        if (
            state["workflow_id"] != identity.workflow_id
            or state["workflow_thread_id"] != identity.thread_id
            or owner_map["user_id"] != identity.user_id
            or owner_map["session_id"] != identity.session_id
            or owner_map["agent_id"] != identity.agent_id
            or service_owner.user_id != identity.user_id
            or service_owner.session_id != identity.session_id
            or service_owner.agent_id != identity.agent_id
            or service_owner.workflow_thread_id != identity.thread_id
        ):
            raise WorkflowGraphExecutionError(
                "workflow_resume_identity_mismatch",
                "Workflow execution identity does not own this graph thread.",
            )


def _pending_interrupts(
    snapshot: Any,
) -> tuple[tuple[str, WorkflowBranchInterruptInput], ...]:
    values: list[tuple[str, WorkflowBranchInterruptInput]] = []
    for task in tuple(getattr(snapshot, "tasks", ()) or ()):
        # LangGraph keeps completed siblings in the interrupted super-step
        # snapshot. Only tasks without a result remain resumable.
        if getattr(task, "result", None) is not None:
            continue
        for native in tuple(getattr(task, "interrupts", ()) or ()):
            native_id = getattr(native, "id", None)
            if not isinstance(native_id, str) or not native_id:
                raise WorkflowGraphExecutionError(
                    "workflow_interrupt_id_invalid",
                    "Native workflow interrupt has no stable id.",
                )
            try:
                request = WorkflowBranchInterruptInput.model_validate_json(
                    json.dumps(getattr(native, "value", None))
                )
            except Exception as exc:
                raise WorkflowGraphExecutionError(
                    "workflow_interrupt_payload_invalid",
                    "Native workflow interrupt payload is invalid.",
                ) from exc
            values.append((native_id, request))
    return tuple(values)


__all__ = [
    "DurableWorkflowGraphApp",
    "WorkflowGraphExecutionError",
    "WorkflowGraphExecutionIdentity",
    "WorkflowGraphInterrupt",
    "WorkflowGraphStreamPart",
    "WorkflowGraphStreamResult",
    "WorkflowResume",
]
