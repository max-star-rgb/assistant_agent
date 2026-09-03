from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest
from deepagents.backends import FilesystemBackend, LocalShellBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from assistant_agent.agent_server import media_app
from assistant_agent.native_agent import assistant_agent
from assistant_agent.native_agent.assistant_agent import (
    RuntimeConfigurableSummarizationMiddleware,
    VerificationGateMiddleware,
    build_assistant_agent,
    build_general_purpose_worker,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import AssistantAgentState
from assistant_agent.native_agent.tool_profiles import project_tool_profiles


def _tool(name: str) -> BaseTool:
    def probe(value: str) -> str:
        """Return one sentinel value."""

        return value

    return StructuredTool.from_function(
        probe,
        name=name,
    )


def test_main_uses_factory_filesystem_and_unified_hitl(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        assistant_agent,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or "compiled",
    )
    result = build_assistant_agent(
        MockAssistantChatModel(),
        [
            _tool("read_probe"),
            _tool("write_probe"),
            _tool("dangerous_probe"),
            _tool("generate_probe"),
            StructuredTool.from_function(
                lambda value: value,
                name="mcp_probe",
                description="Return one MCP sentinel.",
                    metadata={"source": "mcp"},
            ),
        ],
        backend=object(),
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
        additional_middleware=(
            SimpleNamespace(
                tools=[
                    _tool("start_async_task"),
                    _tool("check_async_task"),
                ]
            ),
        ),
        general_purpose_tool_names={"read_probe"},
        interrupt_tool_names={
            "write_probe",
            "dangerous_probe",
            "generate_probe",
            "mcp_probe",
            "start_async_task",
        },
    )

    assert result == "compiled"
    assert not any(
        isinstance(item, FilesystemMiddleware) for item in captured["middleware"]
    )
    assert captured["interrupt_on"].keys() >= {
        "write_file",
        "edit_file",
        "delete",
        "execute",
        "write_probe",
        "dangerous_probe",
        "generate_probe",
        "mcp_probe",
        "start_async_task",
    }
    assert "check_async_task" not in captured["interrupt_on"]
    assert (
        sum(isinstance(item, TodoListMiddleware) for item in captured["middleware"])
        == 1
    )
    assert [item["name"] for item in captured["subagents"]] == [
        "general-purpose",
        "reviewer",
        "coder",
        "browser-operator",
    ]
    assert captured["name"] == "AssistantAgent"


def test_main_and_worker_use_the_configured_summarization_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, dict[str, Any]] = {}

    def capture_agent(**kwargs: Any) -> RunnableLambda:
        key = "reviewer" if kwargs["name"] == "AssistantReviewer" else "worker"
        captured[key] = kwargs
        return RunnableLambda(lambda state: state)

    monkeypatch.setattr(assistant_agent, "create_agent", capture_agent)
    monkeypatch.setattr(
        assistant_agent,
        "create_deep_agent",
        lambda **kwargs: captured.setdefault("main", kwargs) or object(),
    )

    def token_counter(messages: Any) -> int:
        return len(tuple(messages))

    options = {
        "context_window_tokens": 90_000,
        "compaction_trigger_ratio": 0.62,
        "compaction_target_ratio": 0.21,
        "token_counter": token_counter,
    }
    skills_backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    worker = build_general_purpose_worker(
        MockAssistantChatModel(),
        [],
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        skills_backend=skills_backend,
        **options,
    )
    build_assistant_agent(
        MockAssistantChatModel(),
        [],
        backend=object(),
        worker_graph=worker,
        skills_backend=skills_backend,
        **options,
    )

    for key in ("main", "worker"):
        summarizer = next(
            item
            for item in captured[key]["middleware"]
            if isinstance(item, RuntimeConfigurableSummarizationMiddleware)
        )
        assert summarizer.trigger == ("tokens", 55_800)
        assert summarizer.keep == ("tokens", 18_900)
        assert summarizer.token_counter is token_counter
    assert (
        next(
            item
            for item in captured["worker"]["middleware"]
            if isinstance(item, RuntimeConfigurableSummarizationMiddleware)
        ).model
        is captured["worker"]["model"]
    )
    assert captured["reviewer"]["tools"] == []


def test_main_graph_has_memory_lifecycle_without_parent_wrapper(tmp_path: Path) -> None:
    graph = build_assistant_agent(
        MockAssistantChatModel(),
        [],
        backend=object(),
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )
    nodes = set(graph.get_graph().nodes)

    assert {
        "MemoryLifecycleMiddleware.before_agent",
        "model",
        "MemoryLifecycleMiddleware.after_agent",
    } <= nodes
    assert "assistant_agent" not in nodes
    assert (
        not {"execution_router", "fast_agent", "planning_agent", "coding_agent"} & nodes
    )


def test_media_stream_keeps_unified_assistant_model_public() -> None:
    stream = media_app._NativeAssistantTextStream()
    message_id = "assistant-message"

    stream.consume(
        {
            "event": "messages/metadata",
            "data": {
                message_id: {
                    "metadata": {
                        "langgraph_node": "model",
                        "langgraph_checkpoint_ns": "assistant_agent:sentinel",
                    }
                }
            },
        }
    )

    assert stream.consume(
        {
            "event": "messages/partial",
            "data": [
                {
                    "id": message_id,
                    "type": "AIMessageChunk",
                    "content": "stream-sentinel",
                }
            ],
        }
    ) == [(1, "stream-sentinel")]


class _WriteOnceModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(
            isinstance(message, ToolMessage) and message.name == "write_probe"
            for message in messages
        ):
            return AIMessage(content="write-complete")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_probe",
                    "args": {"value": "sentinel"},
                    "id": "write-call",
                    "type": "tool_call",
                }
            ],
        )


class _WriteThenReviewModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        query = next(
            (
                message.content
                for message in reversed(messages)
                if isinstance(message, HumanMessage)
            ),
            "",
        )
        if isinstance(query, str) and "执行证据" in query:
            return AIMessage(content="pass")
        if any(
            isinstance(message, ToolMessage) and message.name == "task"
            for message in messages
        ):
            return AIMessage(content="verified-final")
        if any(
            isinstance(message, ToolMessage) and message.name == "write_probe"
            for message in messages
        ):
            return AIMessage(content="candidate")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_probe",
                    "args": {"value": "sentinel"},
                    "id": "write-for-review",
                    "type": "tool_call",
                }
            ],
        )


class _ReadOnceModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(
            isinstance(message, ToolMessage) and message.name == "read_probe"
            for message in messages
        ):
            return AIMessage(content="read-final")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_probe",
                    "args": {"value": "sentinel"},
                    "id": "read-without-review",
                    "type": "tool_call",
                }
            ],
        )


class _AutonomousReviewModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(
            isinstance(message, ToolMessage) and message.name == "task"
            for message in messages
        ):
            return AIMessage(content="autonomous-review-final")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "reviewer",
                        "description": "自主审查当前过程",
                    },
                    "id": "autonomous-review",
                    "type": "tool_call",
                }
            ],
        )


class _FailingReviewerModel(_WriteThenReviewModel):
    def _response_message(self, messages, **kwargs):
        if any(
            isinstance(message, HumanMessage)
            and isinstance(message.content, str)
            and "执行证据" in message.content
            for message in messages
        ):
            raise RuntimeError("reviewer unavailable")
        return super()._response_message(messages, **kwargs)


def _compiled_agent(
    tmp_path: Path,
    model: BaseChatModel,
    tools: Sequence[BaseTool] = (),
    *,
    interrupt_tool_names=frozenset(),
):
    worker = build_general_purpose_worker(
        model,
        tools,
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )
    return build_assistant_agent(
        model,
        tools,
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=worker,
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
        interrupt_tool_names=interrupt_tool_names,
    )


def test_simple_request_does_not_require_todo_or_task(tmp_path: Path) -> None:
    graph = _compiled_agent(tmp_path, MockAssistantChatModel())
    result = graph.invoke(
        {"messages": [HumanMessage(content="simple-sentinel")]},
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "simple-thread"}},
    )
    assert isinstance(result["messages"][-1], AIMessage)
    assert not any(isinstance(message, ToolMessage) for message in result["messages"])


def test_successful_write_forces_reviewer_task_before_final_answer(
    tmp_path: Path,
) -> None:
    graph = _compiled_agent(
        tmp_path,
        _WriteThenReviewModel(),
        [_tool("write_probe")],
        interrupt_tool_names={"write_probe"},
    )

    result = graph.invoke(
        {"messages": [HumanMessage(content="change-sentinel")]},
        context=AssistantRunContext(require_tool_approval=False),
        config={"configurable": {"thread_id": "forced-review-thread"}},
    )

    task_calls = [
        call
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "task"
    ]
    assert [call["args"]["subagent_type"] for call in task_calls] == ["reviewer"]
    assert not any(
        isinstance(message, AIMessage) and message.text == "candidate"
        for message in result["messages"]
    )
    assert result["messages"][-1].text == "verified-final"


def test_successful_read_does_not_force_reviewer_task(tmp_path: Path) -> None:
    graph = _compiled_agent(
        tmp_path,
        _ReadOnceModel(),
        [_tool("read_probe")],
    )

    result = graph.invoke(
        {"messages": [HumanMessage(content="read-sentinel")]},
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "read-without-review-thread"}},
    )

    assert result["messages"][-1].text == "read-final"
    assert not any(
        call["name"] == "task"
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    )


def test_model_can_call_reviewer_task_autonomously(
    tmp_path: Path,
) -> None:
    graph = _compiled_agent(
        tmp_path,
        _AutonomousReviewModel(),
    )

    result = graph.invoke(
        {"messages": [HumanMessage(content="change-sentinel")]},
        context=AssistantRunContext(require_tool_approval=False),
        config={"configurable": {"thread_id": "autonomous-review-thread"}},
    )

    task_call_ids = [
        call["id"]
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "task"
    ]
    assert task_call_ids == ["autonomous-review"]
    assert result["messages"][-1].text == "autonomous-review-final"


def test_low_recursion_budget_fails_closed_instead_of_exhausting_graph(
    tmp_path: Path,
) -> None:
    graph = _compiled_agent(
        tmp_path,
        _WriteThenReviewModel(),
        [_tool("write_probe")],
        interrupt_tool_names={"write_probe"},
    )

    result = graph.invoke(
        {"messages": [HumanMessage(content="change-sentinel")]},
        context=AssistantRunContext(require_tool_approval=False),
        config={"recursion_limit": 12},
    )

    assert "未能通过强制验证" in result["messages"][-1].text


def test_reviewer_model_failures_retry_then_fail_closed(tmp_path: Path) -> None:
    graph = _compiled_agent(
        tmp_path,
        _FailingReviewerModel(),
        [_tool("write_probe")],
        interrupt_tool_names={"write_probe"},
    )

    result = graph.invoke(
        {"messages": [HumanMessage(content="change-sentinel")]},
        context=AssistantRunContext(require_tool_approval=False),
        config={"recursion_limit": 40},
    )

    forced_reviews = [
        call
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "task" and call["id"].startswith("forced-review-")
    ]
    assert len(forced_reviews) == 2
    assert "未能通过强制验证" in result["messages"][-1].text


@pytest.mark.parametrize(
    "calls,results",
    [
        (
            [
                {
                    "name": "task",
                    "args": {"subagent_type": "coder", "description": "change"},
                    "id": "coder-call",
                    "type": "tool_call",
                }
            ],
            [ToolMessage(content="done", tool_call_id="coder-call")],
        ),
        (
            [
                {
                    "name": "task",
                    "args": {"subagent_type": "reviewer", "description": "review"},
                    "id": "parallel-review",
                    "type": "tool_call",
                },
                {
                    "name": "write_probe",
                    "args": {"value": "changed"},
                    "id": "parallel-write",
                    "type": "tool_call",
                },
            ],
            [
                ToolMessage(content="pass", tool_call_id="parallel-review"),
                ToolMessage(content="done", tool_call_id="parallel-write"),
            ],
        ),
    ],
)
def test_coder_or_parallel_mutation_still_requires_review(calls, results) -> None:
    update = VerificationGateMiddleware({"write_probe"}).before_model(
        {
            "messages": [
                HumanMessage(content="change"),
                AIMessage(content="", tool_calls=calls),
                *results,
            ],
            "remaining_steps": 100,
        },
        None,
    )

    forced_call = update["messages"][0].tool_calls[0]
    assert forced_call["name"] == "task"
    assert forced_call["args"]["subagent_type"] == "reviewer"


@pytest.mark.parametrize("decision,expected", [("approve", 1), ("reject", 0)])
def test_write_interrupts_before_handler_and_resumes_once(
    tmp_path: Path,
    decision: str,
    expected: int,
) -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one governed write."""

        executed.append(value)
        return "ok"

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
    )
    assistant = _compiled_agent(
        tmp_path,
        _WriteOnceModel(),
        [tool],
        interrupt_tool_names={"write_probe"},
    )
    builder = StateGraph(AssistantAgentState, context_schema=AssistantRunContext)
    builder.add_node("assistant", assistant)
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"write-{decision}"}}

    interrupted = graph.invoke(
        {"messages": [HumanMessage(content="write sentinel")]},
        context=AssistantRunContext(),
        config=config,
    )
    assert executed == []
    assert (
        interrupted["__interrupt__"][0].value["action_requests"][0]["name"]
        == "write_probe"
    )

    resumed = graph.invoke(
        Command(resume={"decisions": [{"type": decision}]}),
        context=AssistantRunContext(),
        config=config,
    )
    assert len(executed) == expected
    assert (
        sum(
            isinstance(message, ToolMessage) and message.tool_call_id == "write-call"
            for message in resumed["messages"]
        )
        == 1
    )


def test_filesystem_profile_includes_execute() -> None:
    filesystem = next(
        profile
        for profile in project_tool_profiles()
        if profile.profile_id == "filesystem"
    )

    assert filesystem.tool_names[-1] == "execute"
