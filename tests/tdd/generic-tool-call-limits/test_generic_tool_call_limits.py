from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel


class _RepeatLimitedToolsModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        tool_messages = [
            message for message in messages if isinstance(message, ToolMessage)
        ]
        if len(tool_messages) >= 4:
            return AIMessage(content="terminal-answer")
        attempt = len(tool_messages) // 2 + 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "limited_alpha",
                    "args": {"value": "alpha"},
                    "id": f"call-alpha-{attempt}",
                    "type": "tool_call",
                },
                {
                    "name": "limited_beta",
                    "args": {"value": "beta"},
                    "id": f"call-beta-{attempt}",
                    "type": "tool_call",
                },
            ],
        )


def test_same_tool_and_same_arguments_execute_only_once_without_metadata() -> None:
    executions = {"limited_alpha": 0, "limited_beta": 0}

    def run_alpha(value: str) -> str:
        """Run the alpha probe."""

        executions["limited_alpha"] += 1
        return value

    def run_beta(value: str) -> str:
        """Run the beta probe."""

        executions["limited_beta"] += 1
        return value

    tools = [
        StructuredTool.from_function(
            run_alpha,
            name="limited_alpha",
            metadata={"effect": "generate"},
        ),
        StructuredTool.from_function(
            run_beta,
            name="limited_beta",
            metadata={"effect": "generate"},
        ),
    ]
    graph = build_fast_agent(_RepeatLimitedToolsModel(), tools)

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="exercise-limits")],
                "execution_mode": "fast",
            },
            context=AssistantRunContext(),
        )
    )

    limiter_nodes = [
        name
        for name in graph.get_graph().nodes
        if name.startswith("PerToolCallLimitMiddleware")
    ]
    limit_errors = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.status == "error"
    ]
    assert limiter_nodes == ["PerToolCallLimitMiddleware.after_model"]
    assert executions == {"limited_alpha": 1, "limited_beta": 1}
    assert {message.name for message in limit_errors} == {
        "limited_alpha",
        "limited_beta",
    }


class _ManyDistinctCallsModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="terminal-answer")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool_name,
                    "args": {"value": index},
                    "id": f"call-{tool_name}-{index}",
                    "type": "tool_call",
                }
                for tool_name in ("alpha_probe", "beta_probe")
                for index in range(12)
            ]
            + [
                {
                    "name": "alpha_probe",
                    "args": {"value": 12},
                    "id": "call-alpha_probe-12",
                    "type": "tool_call",
                }
            ],
        )


def test_each_tool_allows_twelve_distinct_arguments_without_global_total_limit() -> None:
    executions: dict[str, list[int]] = {"alpha_probe": [], "beta_probe": []}

    def alpha_probe(value: int) -> str:
        """Record one alpha execution."""

        executions["alpha_probe"].append(value)
        return str(value)

    def beta_probe(value: int) -> str:
        """Record one beta execution."""

        executions["beta_probe"].append(value)
        return str(value)

    graph = build_fast_agent(
        _ManyDistinctCallsModel(),
        [
            StructuredTool.from_function(alpha_probe, metadata={"effect": "read"}),
            StructuredTool.from_function(beta_probe, metadata={"effect": "read"}),
        ],
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="exercise-distinct-limits")],
                "execution_mode": "fast",
            },
            context=AssistantRunContext(),
        )
    )

    assert executions == {
        "alpha_probe": list(range(12)),
        "beta_probe": list(range(12)),
    }
    errors = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.status == "error"
    ]
    assert [(message.name, message.tool_call_id) for message in errors] == [
        ("alpha_probe", "call-alpha_probe-12")
    ]
    assert result["messages"][-1].content == "terminal-answer"


class _TwelveModelRoundsModel(MockAssistantChatModel):
    _calls: int = PrivateAttr(default=0)

    @property
    def calls(self) -> int:
        return self._calls

    def _response_message(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del messages, kwargs
        self._calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "round_probe",
                    "args": {"value": self._calls},
                    "id": f"round-{self._calls}",
                    "type": "tool_call",
                }
            ],
        )


def test_default_run_budget_allows_twelve_model_calls() -> None:
    model = _TwelveModelRoundsModel()

    def round_probe(value: int) -> str:
        """Return one model-round sentinel."""

        return str(value)

    graph = build_fast_agent(
        model,
        [StructuredTool.from_function(round_probe, metadata={"effect": "read"})],
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="exercise-model-limit")],
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )

    assert model.calls == 12
    terminal = result["messages"][-1]
    assert isinstance(terminal, AIMessage)
    assert terminal.tool_calls == []
