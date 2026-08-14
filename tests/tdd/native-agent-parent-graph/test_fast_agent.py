"""RED/GREEN coverage for the reusable LangChain create_agent subgraph."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel


class RecordingModel(BaseChatModel):
    calls: list[list[AnyMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "recording"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del stop, run_manager, kwargs
        self.calls.append(list(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="完成"))]
        )


class ToolCallingModel(RecordingModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del stop, run_manager, kwargs
        self.calls.append(list(messages))
        if not any(isinstance(message, ToolMessage) for message in messages):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup",
                        "args": {"query": "sentinel"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            message = AIMessage(content="工具已完成")
        return ChatResult(generations=[ChatGeneration(message=message)])


class WriteCallingModel(RecordingModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del stop, run_manager, kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="写入完成"))]
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_probe",
                                "args": {"value": "sentinel"},
                                "id": "write-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def _context() -> AssistantRunContext:
    return AssistantRunContext()


def test_fast_agent_uses_dynamic_memory_prompt_and_standard_messages() -> None:
    model = RecordingModel(calls=[])
    agent = build_fast_agent(model, [])

    result = agent.invoke(
        {
            "messages": [HumanMessage(content="你好")],
            "memory_context": ("用户偏好简洁",),
            "memory_status": "ready",
        },
        context=_context(),
    )

    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "完成"
    system = next(
        message for message in model.calls[0] if isinstance(message, SystemMessage)
    )
    assert "用户偏好简洁" in str(system.content)
    assert "不可信历史数据" in str(system.content)


def test_fast_agent_runs_standard_tool_loop_and_tool_message() -> None:
    def lookup(query: str) -> str:
        """Return one local lookup result."""

        return f"found:{query}"

    tool = StructuredTool.from_function(
        lookup,
        name="lookup",
        metadata={"effect": "read"},
    )
    agent = build_fast_agent(ToolCallingModel(calls=[]), [tool])

    result = agent.invoke(
        {"messages": [HumanMessage(content="查询")]},
        context=_context(),
    )

    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert tool_messages[0].content == "found:sentinel"
    assert result["messages"][-1].content == "工具已完成"


def test_fast_agent_exposes_native_message_stream() -> None:
    agent = build_fast_agent(MockAssistantChatModel(), [])

    async def collect():
        return [
            chunk
            async for chunk in agent.astream(
                {"messages": [HumanMessage(content="流式")]},
                context=_context(),
                stream_mode="messages",
            )
        ]

    chunks = asyncio.run(collect())

    assert any(isinstance(message, AIMessageChunk) for message, _metadata in chunks)


def test_fast_agent_retries_only_allowlisted_read_tool() -> None:
    attempts = 0

    def lookup(query: str) -> str:
        """Fail once before returning a read result."""

        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return f"found:{query}"

    tool = StructuredTool.from_function(
        lookup,
        name="lookup",
        metadata={"effect": "read"},
    )
    agent = build_fast_agent(ToolCallingModel(calls=[]), [tool])

    result = agent.invoke(
        {"messages": [HumanMessage(content="查询")]},
        context=_context(),
    )

    assert attempts == 2
    assert any(
        isinstance(message, ToolMessage) and message.content == "found:sentinel"
        for message in result["messages"]
    )


def test_fast_mode_executes_trusted_write_tool_without_interrupt() -> None:
    calls = 0

    def write_probe(value: str) -> str:
        """Write one value after approval."""

        nonlocal calls
        calls += 1
        return value

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    agent = build_fast_agent(WriteCallingModel(calls=[]), [tool])

    result = agent.invoke(
        {
            "messages": [HumanMessage(content="写入")],
            "execution_mode": "fast",
        },
        context=_context(),
    )

    assert calls == 1
    assert "__interrupt__" not in result
    assert result["messages"][-1].content == "写入完成"


def test_planning_mode_interrupts_before_trusted_write_tool() -> None:
    calls = 0

    def write_probe(value: str) -> str:
        """Write one value after approval."""

        nonlocal calls
        calls += 1
        return value

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    agent = build_fast_agent(WriteCallingModel(calls=[]), [tool])

    result = agent.invoke(
        {
            "messages": [HumanMessage(content="规划后写入")],
            "execution_mode": "planning",
        },
        context=_context(),
    )

    assert calls == 0
    assert (
        result["__interrupt__"][0].value["action_requests"][0]["name"] == "write_probe"
    )


def test_fast_agent_installs_native_limits_retry_summary_and_hitl() -> None:
    def read_probe(value: str) -> str:
        """Read one value."""

        return value

    def write_probe(value: str) -> str:
        """Write one value."""

        return value

    tools = [
        StructuredTool.from_function(
            read_probe,
            name="read_probe",
            metadata={"effect": "read"},
        ),
        StructuredTool.from_function(
            write_probe,
            name="write_probe",
            metadata={"effect": "write"},
        ),
    ]

    agent = build_fast_agent(
        RecordingModel(calls=[]),
        tools,
        model_call_limit=3,
        tool_call_limit=4,
        context_window_tokens=1_000,
    )
    node_names = set(agent.get_graph().nodes)

    assert agent.name == "AssistantFastAgent"
    assert any("ModelCallLimitMiddleware" in name for name in node_names)
    assert any("ToolCallLimitMiddleware" in name for name in node_names)
    assert any("SummarizationMiddleware" in name for name in node_names)
    assert any("HumanInTheLoopMiddleware" in name for name in node_names)
