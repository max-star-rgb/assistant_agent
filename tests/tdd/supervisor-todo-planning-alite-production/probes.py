from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from assistant_agent.native_agent.providers import MockAssistantChatModel


def tool_calls(*calls: tuple[str, dict[str, object], str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
            for name, args, call_id in calls
        ],
    )


class ScriptedSupervisor:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tool_names: set[str] = set()

    def bind_tools(self, tools: Sequence[BaseTool], **_kwargs: Any):
        self.bound_tool_names = {item.name for item in tools}
        return self

    def invoke(self, _messages: Sequence[object]) -> AIMessage:
        if not self.responses:
            raise AssertionError("supervisor responses exhausted")
        return self.responses.pop(0)

    async def ainvoke(self, messages: Sequence[object]) -> AIMessage:
        return self.invoke(messages)

    @classmethod
    def parallel_wave(cls, todo_ids: Sequence[str]) -> "ScriptedSupervisor":
        return cls(
            [
                tool_calls(
                    (
                        "write_todos",
                        {
                            "todos": [
                                {
                                    "todo_id": todo_id,
                                    "content": f"todo-{todo_id}",
                                    "status": "pending",
                                }
                                for todo_id in todo_ids
                            ]
                        },
                        "write-initial",
                    )
                ),
                tool_calls(
                    *(
                        ("task", {"todo_id": todo_id}, f"task-{todo_id}")
                        for todo_id in todo_ids
                    )
                ),
                AIMessage(content="final-sentinel"),
            ]
        )


def private_payload(messages: Sequence[AnyMessage]) -> dict[str, Any]:
    human = next(item for item in messages if isinstance(item, HumanMessage))
    payload = json.loads(str(human.content))
    if not isinstance(payload, dict):
        raise TypeError("worker payload must be an object")
    return payload


def worker_result_call(
    todo_id: str,
    *,
    status: str = "succeeded",
    summary: str | None = None,
) -> AIMessage:
    return tool_calls(
        (
            "WorkerResult",
            {
                "todo_id": todo_id,
                "status": status,
                "summary": summary or f"{todo_id}-{status}-sentinel",
            },
            f"worker-result-{todo_id}",
        )
    )


class BarrierWorkerModel(MockAssistantChatModel):
    expected_todos: set[str]
    _active: int = PrivateAttr(default=0)
    _all_started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _calls_by_todo: Counter[str] = PrivateAttr(default_factory=Counter)
    _max_concurrency: int = PrivateAttr(default=0)
    _payloads: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @property
    def calls_by_todo(self) -> Counter[str]:
        return self._calls_by_todo

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return list(self._payloads)

    def _response_message(self, messages: list[AnyMessage], **_kwargs: Any) -> AIMessage:
        return worker_result_call(str(private_payload(messages)["todo_id"]))

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        payload = private_payload(messages)
        todo_id = str(payload["todo_id"])
        self._payloads.append(payload)
        self._calls_by_todo[todo_id] += 1
        self._active += 1
        self._max_concurrency = max(self._max_concurrency, self._active)
        if self._active == len(self.expected_todos):
            self._all_started.set()
        try:
            await asyncio.wait_for(self._all_started.wait(), timeout=2)
            return ChatResult(
                generations=[ChatGeneration(message=worker_result_call(todo_id))]
            )
        finally:
            self._active -= 1


class OperationalFailureWorkerModel(BarrierWorkerModel):
    fail_once_for: str

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        todo_id = str(private_payload(messages)["todo_id"])
        result = await super()._agenerate(messages, stop, run_manager, **kwargs)
        if todo_id == self.fail_once_for and self.calls_by_todo[todo_id] == 1:
            # Let the sibling Worker nodes publish their completed writes before
            # this branch fails, so the test exercises pending-write resume rather
            # than a race between still-running siblings and the first exception.
            await asyncio.sleep(0.05)
            raise TimeoutError(f"{todo_id}-operational-sentinel")
        return result


class SequencedWorkerModel(MockAssistantChatModel):
    outcomes: dict[str, list[str]]
    _calls_by_todo: Counter[str] = PrivateAttr(default_factory=Counter)

    @property
    def calls_by_todo(self) -> Counter[str]:
        return self._calls_by_todo

    def _response_message(self, messages: list[AnyMessage], **_kwargs: Any) -> AIMessage:
        todo_id = str(private_payload(messages)["todo_id"])
        call_index = self._calls_by_todo[todo_id]
        self._calls_by_todo[todo_id] += 1
        outcomes = self.outcomes[todo_id]
        status = outcomes[min(call_index, len(outcomes) - 1)]
        return worker_result_call(todo_id, status=status)


class ToolLoopWorkerModel(MockAssistantChatModel):
    _seen_messages: list[list[AnyMessage]] = PrivateAttr(default_factory=list)

    @property
    def seen_messages(self) -> list[list[AnyMessage]]:
        return list(self._seen_messages)

    def _response_message(self, messages: list[AnyMessage], **_kwargs: Any) -> AIMessage:
        self._seen_messages.append(list(messages))
        todo_id = str(private_payload(messages)["todo_id"])
        if not any(
            isinstance(message, ToolMessage) and message.name == "business_probe"
            for message in messages
        ):
            return tool_calls(
                ("business_probe", {"todo_id": todo_id}, f"business-{todo_id}")
            )
        return worker_result_call(todo_id)
