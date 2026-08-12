"""Stable compiled application for the assistant turn graph."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph
from assistant_agent.runtime.assistant_loop_nodes import AssistantLoopState
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext


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
    """Completed graph state together with the native stream that produced it."""

    final_state: AssistantLoopState
    parts: tuple[GraphStreamPart, ...]


class AssistantTurnGraphApp:
    """Own the one compiled assistant graph shared by a runtime instance."""

    def __init__(self) -> None:
        self._graph = build_assistant_loop_graph()

    @classmethod
    def from_compiled_graph(cls, graph: Any) -> "AssistantTurnGraphApp":
        """Wrap an already compiled graph without compiling another one."""

        app = cls.__new__(cls)
        app._graph = graph
        return app

    @property
    def graph(self) -> Any:
        """Return the compiled graph without allowing replacement."""

        return self._graph

    async def astream(
        self,
        input_state: AssistantLoopState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> AsyncIterator[GraphStreamPart]:
        """Stream normalized native events from the compiled graph."""

        async for raw in self._graph.astream(
            input_state,
            config=identity.runnable_config(),
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
        input_state: AssistantLoopState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> GraphStreamResult:
        """Consume one native stream and return its last root ``values`` state."""

        parts: list[GraphStreamPart] = []
        final_state: AssistantLoopState | object = _MISSING_FINAL_STATE
        async for part in self.astream(
            input_state,
            identity=identity,
            context=context,
        ):
            parts.append(part)
            if part.type == "values" and not part.namespace:
                final_state = part.data
        if final_state is _MISSING_FINAL_STATE:
            raise GraphExecutionError(
                "graph_final_state_missing",
                "LangGraph stream ended without root final values.",
            )
        return GraphStreamResult(
            final_state=cast(AssistantLoopState, final_state),
            parts=tuple(parts),
        )
