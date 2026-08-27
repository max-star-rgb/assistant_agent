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
from deepagents.middleware import FilesystemMiddleware, SkillsMiddleware
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
from assistant_agent.native_agent import fast_agent as fast_agent_module
from assistant_agent.native_agent.assistant_agent import (
    build_read_only_worker,
    isolated_read_only_worker,
)
from assistant_agent.native_agent.fast_agent import build_fast_agent
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


@pytest.mark.parametrize("snapshot", [None, "a" * 40])
def test_backend_passes_trusted_snapshot_to_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot: str | None
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

    facts = AssistantRuntimeFacts(repository_snapshot_sha=snapshot)
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
            "metadata": assistant_runtime_metadata(facts),
        },
    )

    ReadOnlyCodingWorkspaceBackend(Service(), "repo-sentinel").ls("/")

    assert calls == [
        {
            "identity": "user-sentinel",
            "thread_id": "thread-sentinel",
            "repo_id": "repo-sentinel",
            "base_commit": snapshot,
        }
    ]


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


def test_fast_agent_uses_distinct_skills_and_filesystem_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_middleware: list[object] = []
    skills_backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    worktree_backend = ReadOnlyCodingWorkspaceBackend(object(), "repo-sentinel")

    def create_agent(**kwargs: Any) -> object:
        captured_middleware.extend(kwargs["middleware"])
        return object()

    monkeypatch.setattr(fast_agent_module, "create_agent", create_agent)

    build_fast_agent(
        MockAssistantChatModel(),
        [],
        filesystem_backend=worktree_backend,
        skills_backend=skills_backend,
        tool_profiles=(),
    )

    skills = next(
        middleware
        for middleware in captured_middleware
        if isinstance(middleware, SkillsMiddleware)
    )
    filesystem = next(
        middleware
        for middleware in captured_middleware
        if isinstance(middleware, FilesystemMiddleware)
    )
    assert skills._backend is skills_backend
    assert filesystem.backend is worktree_backend


def test_worker_factory_uses_read_only_worktree_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker_calls: list[dict[str, Any]] = []
    assistant_calls: list[dict[str, Any]] = []
    worker = object()
    assistant = object()
    root_kwargs: dict[str, Any] = {}
    config = SimpleNamespace(
        current_location=None,
        memory_extraction_delay_seconds=0,
    )
    resources = SimpleNamespace(
        visual_history_probe=None,
        live_view_resolver=None,
    )

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
        "_compose_sync",
        lambda config, store: (
            object(),
            resources,
            object(),
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
    monkeypatch.setattr(services, "build_execution_attestation", lambda *args: object())
    monkeypatch.setattr(
        services,
        "build_assistant_root_graph",
        lambda **kwargs: root_kwargs.update(kwargs) or object(),
    )
    monkeypatch.setattr(services, "build_memory_extraction_graph", lambda **kwargs: object())

    asyncio.run(services.AgentServerExecutionOwner.compose(store=None))

    assert len(worker_calls) == len(assistant_calls) == 1
    assert isinstance(worker_calls[0]["backend"], ReadOnlyCodingWorkspaceBackend)
    assert isinstance(assistant_calls[0]["backend"], CodingWorkspaceBackend)
    assert assistant_calls[0]["worker_graph"] is worker
    assert root_kwargs["fast_agent"] is assistant
    assert root_kwargs["planning_agent"] is assistant
    assert root_kwargs["coding_agent"] is assistant
