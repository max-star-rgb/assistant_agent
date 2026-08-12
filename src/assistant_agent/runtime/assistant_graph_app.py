"""Stable compiled application for the assistant turn graph."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast

from langgraph.types import Command

from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.assistant_interrupts import (
    AssistantInterrupt,
    AssistantInterruptContractError,
    AssistantResume,
    validate_assistant_interrupt_request,
    validate_assistant_resume,
    validate_resume_for_interrupt,
)
from assistant_agent.runtime.assistant_graph_profiles import (
    AssistantGraphProfile,
    AssistantGraphProfileName,
    assistant_graph_profile,
)
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.observability.langsmith_config import LangSmithConfig
from assistant_agent.observability.langsmith_native import (
    native_graph_trace_scope,
    native_langsmith_tracing,
)


_MISSING_FINAL_STATE = object()


class GraphExecutionError(RuntimeError):
    """Structured failure raised when a graph run cannot produce a final state."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class GraphExecutionIdentity:
    """LangGraph execution identity for one assistant conversation turn."""

    thread_id: str
    run_id: str
    agent_id: str

    @classmethod
    def for_assistant_turn(
        cls,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> "GraphExecutionIdentity":
        raw = json.dumps(
            ["assistant", agent_id, user_id, session_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()[:32]
        return cls(
            thread_id=f"assistant:{digest}",
            run_id=run_id,
            agent_id=agent_id,
        )

    def runnable_config(self) -> dict[str, dict[str, str]]:
        return {
            "configurable": {
                "thread_id": self.thread_id,
                "run_id": self.run_id,
            }
        }


@dataclass(frozen=True)
class GraphStreamPart:
    """One normalized item from a LangGraph v2 execution stream."""

    type: str
    namespace: tuple[str, ...]
    data: Any


@dataclass(frozen=True)
class GraphStreamResult:
    """Authoritative graph outcome plus the native stream that produced it."""

    final_state: AssistantTurnState
    parts: tuple[GraphStreamPart, ...]
    status: Literal["completed", "interrupted"] = "completed"
    interrupts: tuple[AssistantInterrupt, ...] = ()
    checkpoint_config: dict[str, Any] | None = None


class AssistantTurnGraphApp:
    """Own the one compiled assistant graph shared by a runtime instance."""

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        langsmith_config: LangSmithConfig | None = None,
    ) -> None:
        self._graph = build_assistant_loop_graph(
            checkpointer=checkpointer,
            profile="standard",
            graph_name="AssistantTurnGraph",
        )
        self._profile_graphs: dict[AssistantGraphProfileName, Any] = {}
        self._langsmith_config = langsmith_config or _default_langsmith_config()

    @classmethod
    def from_compiled_graph(
        cls,
        graph: Any,
        *,
        langsmith_config: LangSmithConfig | None = None,
    ) -> "AssistantTurnGraphApp":
        """Wrap an already compiled graph without compiling another one."""

        app = cls.__new__(cls)
        app._graph = graph
        app._profile_graphs = {}
        app._langsmith_config = langsmith_config or _default_langsmith_config()
        return app

    @property
    def graph(self) -> Any:
        """Return the compiled graph without allowing replacement."""

        return self._graph

    def graph_for_profile(
        self,
        profile: AssistantGraphProfileName | AssistantGraphProfile,
    ) -> Any:
        """Return a reusable child graph that inherits its parent's saver."""

        canonical = assistant_graph_profile(profile)
        cached = self._profile_graphs.get(canonical.name)
        if cached is not None:
            return cached
        graph = build_assistant_loop_graph(
            checkpointer=None,
            profile=canonical.name,
            graph_name=f"AssistantTurnGraph.{canonical.name}",
        )
        graph.config = {
            "metadata": {"graph_profile": canonical.name},
            "tags": [
                "assistant_turn_graph",
                f"assistant_profile:{canonical.name}",
            ],
        }
        self._profile_graphs[canonical.name] = graph
        return graph

    def invoke(
        self,
        input_state: AssistantTurnState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> AssistantTurnState:
        """Invoke the compiled graph inside the same native tracing context."""

        with self._native_tracing(identity):
            with native_graph_trace_scope() as callbacks:
                config = self._runnable_config(identity, callbacks=callbacks)
                return cast(
                    AssistantTurnState,
                    self._graph.invoke(
                        input_state,
                        config=config,
                        context=context,
                    ),
                )

    async def astream(
        self,
        input_state: AssistantTurnState | Command[Any],
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> AsyncIterator[GraphStreamPart]:
        """Stream normalized native events from the compiled graph."""

        with self._native_tracing(identity):
            with native_graph_trace_scope() as callbacks:
                config = self._runnable_config(identity, callbacks=callbacks)
                async for raw in self._graph.astream(
                    input_state,
                    config=config,
                    context=context,
                    stream_mode=[
                        "values",
                        "updates",
                        "messages",
                        "custom",
                        "tasks",
                        "checkpoints",
                    ],
                    subgraphs=True,
                    version="v2",
                ):
                    yield GraphStreamPart(
                        type=str(raw["type"]),
                        namespace=tuple(raw.get("ns") or ()),
                        data=raw.get("data"),
                    )

    async def arun(
        self,
        input_state: AssistantTurnState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> GraphStreamResult:
        """Consume one native stream and classify its authoritative snapshot."""

        return await self._consume_stream(
            input_state,
            identity=identity,
            context=context,
        )

    async def aresume(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        resume: AssistantResume,
    ) -> GraphStreamResult:
        """Resume one pending native interrupt on the same thread and a new run."""

        snapshot = await self.aget_state(identity)
        values = getattr(snapshot, "values", None)
        if not values:
            raise GraphExecutionError(
                "graph_checkpoint_not_found",
                "No assistant checkpoint exists for this thread.",
            )
        try:
            state = validate_assistant_turn_state(values)
        except Exception as exc:
            raise GraphExecutionError(
                "graph_checkpoint_incompatible",
                "Assistant checkpoint cannot be resumed by this graph version.",
            ) from exc
        if state.get("profile") != "standard":
            raise GraphExecutionError(
                "graph_profile_mismatch",
                "Assistant checkpoint profile does not match this graph app.",
            )
        pending_payload = state.get("pending_interrupt")
        if pending_payload is None or not _snapshot_interrupt_objects(snapshot):
            raise GraphExecutionError(
                "graph_interrupt_not_pending",
                "Assistant checkpoint has no pending native interrupt.",
            )
        try:
            request = validate_assistant_interrupt_request(pending_payload)
            validated_resume = validate_assistant_resume(
                resume.model_dump(mode="json")
                if hasattr(resume, "model_dump")
                else resume
            )
            validate_resume_for_interrupt(request, validated_resume)
        except AssistantInterruptContractError as exc:
            raise GraphExecutionError(exc.code, exc.message) from exc

        persisted_run = state["run"]
        persisted_request = state["request"]
        expected_thread = GraphExecutionIdentity.for_assistant_turn(
            agent_id=str(persisted_run["agent_id"]),
            user_id=str(persisted_request["user_id"]),
            session_id=str(persisted_request["session_id"]),
            run_id=identity.run_id,
        ).thread_id
        if identity.thread_id != expected_thread or identity.agent_id != persisted_run[
            "agent_id"
        ]:
            raise GraphExecutionError(
                "graph_resume_identity_mismatch",
                "Resume identity does not own the pending assistant thread.",
            )
        if identity.run_id == persisted_run["run_id"]:
            raise GraphExecutionError(
                "graph_resume_run_id_reused",
                "Resume requires a new invocation run_id on the same thread.",
            )
        if context.agent_state is None:
            raise GraphExecutionError(
                "graph_resume_context_missing",
                "Resume requires invocation-local AgentState.",
            )
        if (
            context.agent_state.user_id != persisted_request["user_id"]
            or context.agent_state.session_id != persisted_request["session_id"]
            or context.agent_state.agent_id != persisted_run["agent_id"]
            or context.agent_state.run_id != identity.run_id
        ):
            raise GraphExecutionError(
                "graph_resume_context_mismatch",
                "Resume runtime context does not match the pending assistant thread.",
            )
        # The checkpoint keeps the logical turn trace while each resume has a new
        # graph invocation run.  The native node commits that new run_id before
        # downstream governed work can execute.
        context.agent_state.trace_id = str(persisted_run["trace_id"])

        return await self._consume_stream(
            Command(resume=validated_resume.model_dump(mode="json")),
            identity=identity,
            context=context,
        )

    async def aget_state(self, identity: GraphExecutionIdentity) -> Any:
        """Read the native latest StateSnapshot for an assistant thread."""

        getter = getattr(self._graph, "aget_state", None)
        if not callable(getter):
            raise GraphExecutionError(
                "graph_state_api_unavailable",
                "Compiled graph does not expose native async state access.",
            )
        return await getter(identity.runnable_config(), subgraphs=True)

    async def aget_state_history(
        self,
        identity: GraphExecutionIdentity,
        limit: int,
    ) -> tuple[Any, ...]:
        """Read bounded newest-first native checkpoint history."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("state history limit must be between 1 and 100")
        history = getattr(self._graph, "aget_state_history", None)
        if not callable(history):
            raise GraphExecutionError(
                "graph_state_history_api_unavailable",
                "Compiled graph does not expose native async state history.",
            )
        return tuple([
            item
            async for item in history(
                identity.runnable_config(),
                limit=limit,
            )
        ])

    async def _consume_stream(
        self,
        input_value: AssistantTurnState | Command[Any],
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> GraphStreamResult:
        """Consume the stream, then use native state—not stream shape—as outcome."""

        parts: list[GraphStreamPart] = []
        final_state: AssistantTurnState | object = _MISSING_FINAL_STATE
        async for part in self.astream(
            input_value,
            identity=identity,
            context=context,
        ):
            parts.append(part)
            if part.type == "values" and not part.namespace:
                final_state = part.data
        if getattr(self._graph, "checkpointer", None) is None:
            if final_state is _MISSING_FINAL_STATE:
                raise GraphExecutionError(
                    "graph_final_state_missing",
                    "LangGraph stream ended without root final values.",
                )
            return GraphStreamResult(
                final_state=cast(AssistantTurnState, final_state),
                parts=tuple(parts),
                status="completed",
                interrupts=(),
                checkpoint_config=None,
            )

        snapshot = await self.aget_state(identity)
        snapshot_values = getattr(snapshot, "values", None)
        if not snapshot_values:
            raise GraphExecutionError(
                "graph_final_state_missing",
                "LangGraph state snapshot contains no root values.",
            )
        try:
            authoritative_state = validate_assistant_turn_state(snapshot_values)
            interrupts = _public_interrupts(snapshot)
        except Exception as exc:
            if isinstance(exc, GraphExecutionError):
                raise
            raise GraphExecutionError(
                "graph_snapshot_invalid",
                "LangGraph state snapshot is incompatible or unsafe.",
            ) from exc
        tasks = tuple(getattr(snapshot, "tasks", ()) or ())
        next_nodes = tuple(getattr(snapshot, "next", ()) or ())
        has_pending = bool(tasks or next_nodes)
        if interrupts and has_pending:
            status: Literal["completed", "interrupted"] = "interrupted"
        elif has_pending:
            raise GraphExecutionError(
                "graph_pending_without_interrupt",
                "Graph has pending work without a resumable interrupt.",
            )
        elif interrupts:
            raise GraphExecutionError(
                "graph_interrupt_without_pending_task",
                "Graph exposes an interrupt without pending work.",
            )
        elif authoritative_state["run"]["status"] not in {
            "completed",
            "failed",
            "cancelled",
        }:
            raise GraphExecutionError(
                "graph_terminal_state_invalid",
                "Graph ended without a terminal assistant state.",
            )
        else:
            status = "completed"
        return GraphStreamResult(
            final_state=authoritative_state,
            parts=tuple(parts),
            status=status,
            interrupts=interrupts,
            checkpoint_config=cast(dict[str, Any], snapshot.config),
        )

    def _native_tracing(self, identity: GraphExecutionIdentity) -> Any:
        return native_langsmith_tracing(
            self._langsmith_config,
            metadata={
                "run_id": identity.run_id,
                "thread_id": identity.thread_id,
                "agent_id": identity.agent_id,
                "execution_engine": "assistant_turn_graph",
                "graph_profile": "standard",
            },
            tags=["assistant_turn_graph"],
        )

    @staticmethod
    def _runnable_config(
        identity: GraphExecutionIdentity,
        *,
        callbacks: list[Any],
    ) -> dict[str, Any]:
        config: dict[str, Any] = dict(identity.runnable_config())
        config["metadata"] = {
            "run_id": identity.run_id,
            "thread_id": identity.thread_id,
            "agent_id": identity.agent_id,
            "execution_engine": "assistant_turn_graph",
            "graph_profile": "standard",
        }
        config["tags"] = ["assistant_turn_graph"]
        existing_callbacks = list(config.get("callbacks") or [])
        config["callbacks"] = [*existing_callbacks, *callbacks]
        return config


def _snapshot_interrupt_objects(snapshot: Any) -> tuple[Any, ...]:
    """Collect native interrupts from root/tasks/subgraphs without namespace keys."""

    collected: list[Any] = list(getattr(snapshot, "interrupts", ()) or ())
    for task in tuple(getattr(snapshot, "tasks", ()) or ()):
        collected.extend(tuple(getattr(task, "interrupts", ()) or ()))
        child = getattr(task, "state", None)
        if child is not None and hasattr(child, "tasks"):
            collected.extend(_snapshot_interrupt_objects(child))
    return tuple(collected)


def _public_interrupts(snapshot: Any) -> tuple[AssistantInterrupt, ...]:
    """Project native Interrupt values and dedupe solely by stable Interrupt.id."""

    by_id: dict[str, AssistantInterrupt] = {}
    for native in _snapshot_interrupt_objects(snapshot):
        interrupt_id = getattr(native, "id", None)
        if not isinstance(interrupt_id, str) or not interrupt_id:
            raise GraphExecutionError(
                "graph_interrupt_id_invalid",
                "Native graph interrupt has no stable id.",
            )
        try:
            request = validate_assistant_interrupt_request(getattr(native, "value", None))
            projected = AssistantInterrupt(
                interrupt_id=interrupt_id,
                kind=request.kind,
                prompt=request.prompt,
                action_ref=request.action_ref,
                allowed_resume_kinds=request.allowed_resume_kinds,
            )
        except Exception as exc:
            raise GraphExecutionError(
                "graph_interrupt_payload_invalid",
                "Native graph interrupt payload violates the assistant contract.",
            ) from exc
        existing = by_id.get(interrupt_id)
        if existing is not None and existing != projected:
            raise GraphExecutionError(
                "graph_interrupt_id_conflict",
                "One native interrupt id resolved to conflicting payloads.",
            )
        by_id[interrupt_id] = projected
    return tuple(by_id.values())


def _default_langsmith_config() -> LangSmithConfig:
    try:
        return LangSmithConfig.from_env()
    except Exception:
        return LangSmithConfig(enabled=False)
