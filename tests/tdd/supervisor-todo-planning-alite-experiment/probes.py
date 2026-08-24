from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool
from pydantic import PrivateAttr

from assistant_agent.native_agent.providers import MockAssistantChatModel


class ScriptedSupervisor:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self._responses = list(responses)
        self.bound_tool_names: set[str] = set()
        self.calls = 0
        self.create_agent_calls = 0

    def bind_tools(self, tools: Sequence[BaseTool], **_kwargs: Any):
        self.bound_tool_names = {tool.name for tool in tools}
        return self

    def invoke(self, _messages: Sequence[object]) -> AIMessage:
        self.calls += 1
        if not self._responses:
            raise AssertionError("scripted supervisor responses exhausted")
        return self._responses.pop(0)

    async def ainvoke(self, messages: Sequence[object]) -> AIMessage:
        return self.invoke(messages)

    @classmethod
    def parallel_wave(cls, todo_ids: Sequence[str]) -> "ScriptedSupervisor":
        ids = tuple(todo_ids)
        return cls(
            [
                _tool_calls(
                    (
                        "write_todos",
                        {
                            "todos": [
                                {
                                    "todo_id": todo_id,
                                    "content": f"todo-{todo_id}",
                                    "status": "pending",
                                }
                                for todo_id in ids
                            ]
                        },
                        "write-initial",
                    )
                ),
                _tool_calls(
                    *(
                        ("task", {"todo_id": todo_id}, f"task-{todo_id}")
                        for todo_id in ids
                    )
                ),
                AIMessage(content="final-sentinel"),
            ]
        )

    @classmethod
    def single_success(cls, todo_id: str) -> "ScriptedSupervisor":
        return cls.parallel_wave((todo_id,))

    @classmethod
    def task_without_todo(cls, todo_id: str) -> "ScriptedSupervisor":
        return cls(
            [
                _tool_calls(
                    ("task", {"todo_id": todo_id}, f"task-{todo_id}")
                )
            ]
        )

    @classmethod
    def blocked_then_retry(cls, todo_id: str) -> "ScriptedSupervisor":
        initial = cls.parallel_wave(("A", "B", "C"))
        return cls(
            [
                *initial._responses[:-1],
                _tool_calls(
                    ("task", {"todo_id": todo_id}, f"retry-task-{todo_id}")
                ),
                AIMessage(content="final-sentinel"),
            ]
        )

    @classmethod
    def blocked_c_then_replace_with_d(cls) -> "ScriptedSupervisor":
        initial = cls.parallel_wave(("A", "B", "C"))
        return cls(
            [
                *initial._responses[:-1],
                _tool_calls(
                    (
                        "write_todos",
                        {
                            "todos": [
                                {
                                    "todo_id": "A",
                                    "content": "todo-A",
                                    "status": "completed",
                                },
                                {
                                    "todo_id": "B",
                                    "content": "todo-B",
                                    "status": "completed",
                                },
                                {
                                    "todo_id": "D",
                                    "content": "todo-D",
                                    "status": "pending",
                                },
                            ]
                        },
                        "write-replan",
                    )
                ),
                _tool_calls(("task", {"todo_id": "D"}, "task-D")),
                AIMessage(content="final-sentinel"),
            ]
        )

    @classmethod
    def blocked_c_then_finish(cls) -> "ScriptedSupervisor":
        return cls.parallel_wave(("A", "B", "C"))


def _tool_calls(
    *calls: tuple[str, dict[str, object], str],
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
            for name, args, call_id in calls
        ],
    )


def _private_payload(messages: Sequence[AnyMessage]) -> dict[str, Any]:
    human = next(
        message
        for message in messages
        if isinstance(message, HumanMessage) and isinstance(message.content, str)
    )
    payload = json.loads(human.content)
    if not isinstance(payload, dict):
        raise TypeError("worker private payload must be an object")
    return payload


def _worker_result_call(
    todo_id: str,
    *,
    status: str = "succeeded",
    summary: str | None = None,
) -> AIMessage:
    return _tool_calls(
        (
            "WorkerResult",
            {
                "todo_id": todo_id,
                "status": status,
                "summary": summary or f"{todo_id}-success-sentinel",
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
    _private_payloads: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @property
    def calls_by_todo(self) -> Counter[str]:
        return self._calls_by_todo

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def private_payloads(self) -> list[dict[str, Any]]:
        return list(self._private_payloads)

    def _response_message(
        self,
        messages: list[AnyMessage],
        **_kwargs: Any,
    ) -> AIMessage:
        payload = _private_payload(messages)
        return _worker_result_call(str(payload["todo_id"]))

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        payload = _private_payload(messages)
        todo_id = str(payload["todo_id"])
        self._private_payloads.append(payload)
        self._calls_by_todo[todo_id] += 1
        self._active += 1
        self._max_concurrency = max(self._max_concurrency, self._active)
        if self._active == len(self.expected_todos):
            self._all_started.set()
        try:
            await asyncio.wait_for(self._all_started.wait(), timeout=2)
            return ChatResult(
                generations=[
                    ChatGeneration(message=_worker_result_call(todo_id))
                ]
            )
        finally:
            self._active -= 1


class ToolCallingWorkerModel(MockAssistantChatModel):
    todo_id: str

    def _response_message(
        self,
        messages: list[AnyMessage],
        **_kwargs: Any,
    ) -> AIMessage:
        payload = _private_payload(messages)
        if payload["todo_id"] != self.todo_id:
            raise ValueError("unexpected worker todo")
        if not any(
            isinstance(message, ToolMessage) and message.name == "read_probe"
            for message in messages
        ):
            return _tool_calls(
                (
                    "read_probe",
                    {"todo_id": self.todo_id},
                    f"read-probe-{self.todo_id}",
                )
            )
        return _worker_result_call(
            self.todo_id,
            summary="read-probe-result-sentinel",
        )


class ScenarioWorkerModel(MockAssistantChatModel):
    outcomes: dict[str, list[str]]
    _calls_by_todo: Counter[str] = PrivateAttr(default_factory=Counter)

    @property
    def calls_by_todo(self) -> Counter[str]:
        return self._calls_by_todo

    def _response_message(
        self,
        messages: list[AnyMessage],
        **_kwargs: Any,
    ) -> AIMessage:
        todo_id = str(_private_payload(messages)["todo_id"])
        position = self._calls_by_todo[todo_id]
        configured = self.outcomes.get(todo_id, [])
        if position >= len(configured):
            raise AssertionError(f"worker outcome exhausted for {todo_id}")
        status = configured[position]
        self._calls_by_todo[todo_id] += 1
        return _worker_result_call(
            todo_id,
            status=status,
            summary=(
                f"{todo_id}-success-sentinel"
                if status == "succeeded"
                else f"{todo_id}-blocked-sentinel"
            ),
        )


def create_read_probe_tool(recorder: list[str]) -> BaseTool:
    @tool("read_probe")
    def read_probe(todo_id: str) -> str:
        """Return one deterministic experiment sentinel."""

        recorder.append(todo_id)
        return "read-probe-result-sentinel"

    return read_probe.model_copy(update={"metadata": {"effect": "read"}})
