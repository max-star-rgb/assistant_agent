"""Evaluation-only target for invoking the production native parent graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.store.base import BaseStore

from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
)


@dataclass(frozen=True)
class NativeGraphEvaluationResult:
    """Minimal observable result retained by evaluation runners."""

    thread_id: str
    run_id: str
    messages: tuple[AnyMessage, ...]

    @property
    def response_message(self) -> AIMessage:
        for message in reversed(self.messages):
            if isinstance(message, AIMessage):
                return message
        raise RuntimeError("native graph evaluation produced no AIMessage")

    @property
    def response_text(self) -> str:
        return self.response_message.text


class _EvaluationUser(dict):
    permissions = ()

    def __init__(self, identity: str) -> None:
        super().__init__(identity=identity, permissions=[])
        self.identity = identity


class NativeGraphEvaluationTarget:
    """Own one native composition for evaluation, never product lifecycle state."""

    def __init__(self, owner: AgentServerExecutionOwner) -> None:
        self._owner = owner

    @classmethod
    async def open(
        cls,
        *,
        store: BaseStore | None = None,
    ) -> "NativeGraphEvaluationTarget":
        return cls(await AgentServerExecutionOwner.compose(store=store))

    async def ainvoke(
        self,
        *,
        identity: str,
        thread_id: str,
        run_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> NativeGraphEvaluationResult:
        context = AssistantRunContext()
        run_metadata = dict(metadata or {})
        run_metadata.update(
            assistant_runtime_metadata(
                AssistantRuntimeFacts(entry_profile="evaluation")
            )
        )
        result = await self._owner.graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content=text, additional_kwargs=dict(metadata or {}))
                ],
            },
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "assistant_id": ASSISTANT_GRAPH_ID,
                    "graph_id": ASSISTANT_GRAPH_ID,
                    "langgraph_auth_user": _EvaluationUser(identity),
                },
                "metadata": run_metadata,
            },
            context=context,
        )
        messages = tuple(result.get("messages", ()))
        evaluation = NativeGraphEvaluationResult(
            thread_id=thread_id,
            run_id=run_id,
            messages=messages,
        )
        evaluation.response_message
        return evaluation

    async def aclose(self) -> None:
        await self._owner.aclose()


__all__ = ["NativeGraphEvaluationResult", "NativeGraphEvaluationTarget"]
