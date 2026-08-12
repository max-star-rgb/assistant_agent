"""Stable compiled application for the assistant turn graph."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph
from assistant_agent.runtime.assistant_graph_state import AssistantTurnState
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
    """Completed graph state together with the native stream that produced it."""

    final_state: AssistantTurnState
    parts: tuple[GraphStreamPart, ...]


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
        input_state: AssistantTurnState,
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
        """Consume one native stream and return its last root ``values`` state."""

        parts: list[GraphStreamPart] = []
        final_state: AssistantTurnState | object = _MISSING_FINAL_STATE
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
            final_state=cast(AssistantTurnState, final_state),
            parts=tuple(parts),
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


def _default_langsmith_config() -> LangSmithConfig:
    try:
        return LangSmithConfig.from_env()
    except Exception:
        return LangSmithConfig(enabled=False)
