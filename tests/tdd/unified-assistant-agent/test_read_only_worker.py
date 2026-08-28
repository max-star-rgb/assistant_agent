"""Temporary RED/GREEN coverage for read-only worker worktree access."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from deepagents.middleware import SkillsMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langgraph_sdk.auth.types import StudioUser

from assistant_agent.coding import backend as backend_module
from assistant_agent.coding.backend import (
    CodingWorkspaceBackend,
    ReadOnlyCodingWorkspaceBackend,
)
from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.workspace import CodingWorkspaceError
from assistant_agent.agent_server import services
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID, WORKER_GRAPH_ID
from assistant_agent.config import ProviderConfig
from assistant_agent.native_agent import assistant_agent as assistant_agent_module
from assistant_agent.native_agent.assistant_agent import (
    build_assistant_agent,
    build_read_only_worker,
    isolated_read_only_worker,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.tool_profiles import ToolProfile
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
)


def _tool(name: str, effect: str) -> BaseTool:
    def probe(value: str = "sentinel") -> str:
        """Return the supplied sentinel."""

        return value

    return StructuredTool.from_function(
        probe,
        name=name,
        metadata={"effect": effect},
    )


def test_worker_exposes_only_read_files_and_read_business_tools(
    tmp_path: Path,
) -> None:
    worker = build_read_only_worker(
        MockAssistantChatModel(),
        [_tool("read_probe", "read"), _tool("write_probe", "write")],
        backend=ReadOnlyCodingWorkspaceBackend(SimpleNamespace(), "repo-sentinel"),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )
    tools = set(worker.get_graph().nodes["tools"].data.tools_by_name)

    assert tools == {"ls", "read_file", "glob", "grep", "read_probe"}


@pytest.mark.parametrize(
    "reserved_name",
    [
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "task",
        "write_todos",
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
        "activate_tool_profile",
    ],
)
def test_worker_rejects_read_labelled_reserved_business_tool_names(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    with pytest.raises(ValueError, match="reserved infrastructure name"):
        build_read_only_worker(
            MockAssistantChatModel(),
            [_tool(reserved_name, "read")],
            backend=ReadOnlyCodingWorkspaceBackend(
                SimpleNamespace(), "repo-sentinel"
            ),
            skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
            tool_profiles=(
                ToolProfile(
                    profile_id="probe",
                    description="Probe tools.",
                    tool_names=("read_probe",),
                ),
            ),
        )


def test_worker_rejects_duplicate_business_tool_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate business tool name"):
        build_read_only_worker(
            MockAssistantChatModel(),
            [_tool("read_probe", "read"), _tool("read_probe", "read")],
            backend=ReadOnlyCodingWorkspaceBackend(
                SimpleNamespace(), "repo-sentinel"
            ),
            skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        )


def test_task_projection_drops_parent_and_worker_private_state() -> None:
    observed: list[dict[str, Any]] = []

    def worker(state: dict[str, Any]) -> dict[str, Any]:
        observed.append(state)
        return {
            "messages": [
                *state["messages"],
                AIMessage(content="internal-draft"),
                AIMessage(content="worker-report"),
            ],
            "provider_search_profile": "sentinel",
            "async_tasks": {"worker-task": {"status": "running"}},
            "active_tool_profile_ids": ["sentinel"],
        }

    runnable = isolated_read_only_worker(RunnableLambda(worker))
    result = runnable.invoke(
        {
            "messages": [HumanMessage(content="task-description")],
            "memory_context": ("memory",),
            "memory_status": "ready",
            "provider_search_profile": "travel_general",
            "async_tasks": {"parent-task": {"status": "running"}},
            "active_tool_profile_ids": ["browser"],
            "future_sentinel": "private",
        }
    )

    assert set(observed[0]) == {"messages", "memory_context"}
    assert set(result) == {"messages"}
    assert [message.content for message in result["messages"]] == ["worker-report"]


def test_task_projection_returns_only_nonempty_structured_response() -> None:
    runnable = isolated_read_only_worker(
        RunnableLambda(
            lambda state: {
                "messages": [AIMessage(content="worker-report")],
                "structured_response": {"answer": "sentinel"},
                "async_tasks": {"blocked": {}},
            }
        )
    )

    result = runnable.invoke(
        {
            "messages": [HumanMessage(content="task-description")],
            "memory_context": (),
        }
    )

    assert set(result) == {"messages", "structured_response"}
    assert result["structured_response"] == {"answer": "sentinel"}


def test_task_projection_returns_explicit_failure_for_empty_worker_result() -> None:
    runnable = isolated_read_only_worker(
        RunnableLambda(
            lambda state: {
                "messages": [AIMessage(content=" \n")],
                "structured_response": {},
            }
        )
    )

    result = runnable.invoke({"messages": [HumanMessage(content="task-description")]})

    message = result["messages"][0]
    assert isinstance(message, AIMessage)
    assert message.text.strip()
    assert len(message.text) <= 200
    assert message.response_metadata["error_code"] == "empty_worker_result"
    assert result["structured_response"] == {}


def test_task_projection_keeps_nonempty_structured_only_result_valid() -> None:
    runnable = isolated_read_only_worker(
        RunnableLambda(
            lambda state: {
                "messages": [],
                "structured_response": {"answer": "structured-sentinel"},
            }
        )
    )

    result = runnable.invoke({"messages": [HumanMessage(content="task-description")]})

    assert result["structured_response"] == {"answer": "structured-sentinel"}
    assert "error_code" not in result["messages"][0].response_metadata


def test_task_projection_forwards_runnable_config_sync_and_async() -> None:
    observed: list[tuple[str, set[str], str]] = []

    def worker(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        observed.append(
            ("sync", set(state), config["configurable"]["projection_sentinel"])
        )
        return {"messages": [AIMessage(content="sync-report")]}

    async def aworker(
        state: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        observed.append(
            ("async", set(state), config["configurable"]["projection_sentinel"])
        )
        return {"messages": [AIMessage(content="async-report")]}

    runnable = isolated_read_only_worker(RunnableLambda(worker, afunc=aworker))
    state = {
        "messages": [HumanMessage(content="task-description")],
        "memory_context": ("memory",),
        "future_sentinel": "private",
    }

    sync_result = runnable.invoke(
        state,
        {"configurable": {"projection_sentinel": "sync-config"}},
    )
    async_result = asyncio.run(
        runnable.ainvoke(
            state,
            {"configurable": {"projection_sentinel": "async-config"}},
        )
    )

    assert [sync_result["messages"][0].content] == ["sync-report"]
    assert [async_result["messages"][0].content] == ["async-report"]
    assert observed == [
        ("sync", {"messages", "memory_context"}, "sync-config"),
        ("async", {"messages", "memory_context"}, "async-config"),
    ]


def test_read_only_backend_has_no_mutation_or_execute_capability() -> None:
    backend = ReadOnlyCodingWorkspaceBackend(object(), "repo-sentinel")

    assert isinstance(backend, BackendProtocol)
    assert not isinstance(backend, SandboxBackendProtocol)
    with pytest.raises(NotImplementedError):
        backend.write("/blocked.txt", "blocked")
    with pytest.raises(NotImplementedError):
        backend.edit("/blocked.txt", "a", "b")
    with pytest.raises(NotImplementedError):
        backend.delete("/blocked.txt")
    with pytest.raises(NotImplementedError):
        backend.upload_files([("/blocked.txt", b"blocked")])
    assert not hasattr(backend, "execute")


def test_main_backend_forces_base_commit_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    class Service:
        def resolve(self, identity, thread_id, repo_id, **kwargs):
            calls.append(
                {
                    "identity": identity,
                    "thread_id": thread_id,
                    "repo_id": repo_id,
                    **kwargs,
                }
            )
            return SimpleNamespace(root=tmp_path)

    facts = AssistantRuntimeFacts(entry_profile="system_eval")
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "thread-sentinel"},
            "metadata": {
                "graph_id": ASSISTANT_GRAPH_ID,
                **assistant_runtime_metadata(facts),
            },
        },
    )

    ReadOnlyCodingWorkspaceBackend(Service(), "repo-sentinel").ls("/")

    assert calls == [
        {
            "identity": "user-sentinel",
            "thread_id": "thread-sentinel",
            "repo_id": "repo-sentinel",
            "base_commit": None,
        }
    ]


def test_worker_backend_passes_exact_snapshot_for_worker_graph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = SimpleNamespace(
        resolve=Mock(return_value=SimpleNamespace(root=tmp_path))
    )
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "worker-thread"},
            "metadata": {
                "graph_id": WORKER_GRAPH_ID,
                **assistant_runtime_metadata(
                    AssistantRuntimeFacts(
                        entry_profile="async_worker",
                        repository_snapshot_sha="a" * 40,
                    )
                ),
            },
        },
    )

    ReadOnlyCodingWorkspaceBackend(service, "repo-sentinel").ls("/")

    service.resolve.assert_called_once_with(
        "user-sentinel",
        "worker-thread",
        "repo-sentinel",
        base_commit="a" * 40,
    )


def test_main_backend_rejects_injected_worker_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(resolve=Mock())
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "main-thread"},
            "metadata": {
                "graph_id": ASSISTANT_GRAPH_ID,
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "a" * 40,
                },
            },
        },
    )

    with pytest.raises(CodingWorkspaceError) as exc_info:
        CodingWorkspaceBackend(service, "repo-sentinel").ls("/")

    assert exc_info.value.code == "workspace_snapshot_invalid"
    service.resolve.assert_not_called()


def test_async_worker_never_falls_back_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(resolve=Mock())
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "worker-thread"},
            "metadata": {
                "graph_id": WORKER_GRAPH_ID,
                ASSISTANT_RUNTIME_METADATA_KEY: {"entry_profile": "async_worker"}
            },
        },
    )

    with pytest.raises(CodingWorkspaceError) as exc_info:
        ReadOnlyCodingWorkspaceBackend(service, "repo-sentinel").ls("/")

    assert exc_info.value.code == "workspace_snapshot_required"
    service.resolve.assert_not_called()


def test_async_worker_rejects_malformed_snapshot_before_workspace_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(resolve=Mock())
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "worker-thread"},
            "metadata": {
                "graph_id": WORKER_GRAPH_ID,
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "invalid",
                }
            },
        },
    )

    with pytest.raises(CodingWorkspaceError) as exc_info:
        ReadOnlyCodingWorkspaceBackend(service, "repo-sentinel").ls("/")

    assert exc_info.value.code == "workspace_snapshot_invalid"
    service.resolve.assert_not_called()


@pytest.mark.parametrize("snapshot", ["", 0, False])
def test_async_worker_classifies_present_invalid_snapshot_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: object,
) -> None:
    service = SimpleNamespace(resolve=Mock())
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "worker-thread"},
            "metadata": {
                "graph_id": WORKER_GRAPH_ID,
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": snapshot,
                }
            },
        },
    )

    with pytest.raises(CodingWorkspaceError) as exc_info:
        ReadOnlyCodingWorkspaceBackend(service, "repo-sentinel").ls("/")

    assert exc_info.value.code == "workspace_snapshot_invalid"
    service.resolve.assert_not_called()


def test_assistant_agent_uses_distinct_skills_and_filesystem_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    skills_backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    worktree_backend = ReadOnlyCodingWorkspaceBackend(object(), "repo-sentinel")

    monkeypatch.setattr(
        assistant_agent_module,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    build_assistant_agent(
        MockAssistantChatModel(),
        [],
        backend=worktree_backend,
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=skills_backend,
        tool_profiles=(),
    )

    skills = next(
        middleware
        for middleware in captured["middleware"]
        if isinstance(middleware, SkillsMiddleware)
    )
    assert skills._backend is skills_backend
    assert captured["backend"] is worktree_backend


def test_worker_factory_uses_read_only_worktree_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker_calls: list[dict[str, Any]] = []
    assistant_calls: list[dict[str, Any]] = []
    worker = object()
    assistant = object()
    memory_backend = object()
    model_view = object()
    model = SimpleNamespace(
        _llm_type="assistant-agent-dashscope-native",
        model_copy=lambda **kwargs: model_view,
    )
    config = SimpleNamespace(
        current_location=None,
        memory_extraction_delay_seconds=0,
        context_input_token_limit=96_000,
        context_compaction_trigger_ratio=0.64,
        context_compaction_target_ratio=0.22,
    )
    resources = SimpleNamespace(
        visual_history_probe=None,
        live_view_resolver=None,
    )
    def counter(messages: Any) -> int:
        return len(tuple(messages))

    counter_factory_calls: list[object] = []

    def build_worker(*args: Any, **kwargs: Any) -> object:
        worker_calls.append({"args": args, **kwargs})
        return worker

    def build_assistant(*args: Any, **kwargs: Any) -> object:
        assistant_calls.append({"args": args, **kwargs})
        return assistant

    async def create_native_tool_inventory(*args: Any, **kwargs: Any) -> list[object]:
        del args, kwargs
        return []

    monkeypatch.setattr(services.ProviderConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        services,
        "create_context_token_counter",
        lambda candidate: (
            counter_factory_calls.append(candidate)
            or SimpleNamespace(count_messages=counter)
        ),
    )
    monkeypatch.setattr(
        services,
        "_compose_sync",
        lambda config, store: (
            model,
            resources,
            memory_backend,
            tmp_path,
            "repo-sentinel",
            CodingConfig(),
        ),
    )
    monkeypatch.setattr(
        services,
        "create_native_tool_inventory",
        create_native_tool_inventory,
    )
    monkeypatch.setattr(services, "project_tool_profiles", lambda: ())
    monkeypatch.setattr(services, "async_task_tool_profile", lambda: object())
    monkeypatch.setattr(
        services,
        "build_async_subagent_middleware",
        lambda *args: SimpleNamespace(tools=[]),
    )
    monkeypatch.setattr(services, "build_read_only_worker", build_worker)
    monkeypatch.setattr(services, "build_assistant_agent", build_assistant)
    monkeypatch.setattr(services, "build_memory_extraction_graph", lambda **kwargs: object())

    owner = asyncio.run(services.AgentServerExecutionOwner.compose(store=None))

    assert len(worker_calls) == len(assistant_calls) == 1
    assert worker_calls[0]["args"][0] is model
    assert isinstance(worker_calls[0]["backend"], ReadOnlyCodingWorkspaceBackend)
    assert isinstance(assistant_calls[0]["backend"], CodingWorkspaceBackend)
    assert assistant_calls[0]["worker_graph"] is worker
    assert assistant_calls[0]["memory_backend"] is memory_backend
    assert assistant_calls[0]["memory_extraction_delay_seconds"] == 0
    assert owner.graph is assistant
    assert counter_factory_calls == [config]
    for call in (worker_calls[0], assistant_calls[0]):
        assert call["context_window_tokens"] == 96_000
        assert call["compaction_trigger_ratio"] == 0.64
        assert call["compaction_target_ratio"] == 0.22
        assert call["token_counter"] is counter


def test_real_deepseek_without_tokenizer_fails_before_provider_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="deepseek",
        chat_api_key="test-only-key",
        chat_base_url="https://example.invalid/v1",
        chat_model="deepseek-v4",
        context_compactor_mode="llm",
        context_tokenizer_path=None,
    )
    compose_sync_called = False

    def compose_sync(*args: Any, **kwargs: Any) -> None:
        nonlocal compose_sync_called
        del args, kwargs
        compose_sync_called = True

    monkeypatch.setattr(services.ProviderConfig, "from_env", lambda: config)
    monkeypatch.setattr(services, "_compose_sync", compose_sync)

    with pytest.raises(ValueError, match="CONTEXT_TOKENIZER_PATH"):
        asyncio.run(services.AgentServerExecutionOwner.compose(store=None))

    assert compose_sync_called is False
