"""Temporary RED/GREEN coverage for read-only worker worktree access."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from langgraph_sdk.auth.types import StudioUser

from assistant_agent.coding import backend as backend_module
from assistant_agent.coding.backend import ReadOnlyCodingWorkspaceBackend
from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.workspace import CodingWorkspaceError
from assistant_agent.agent_server import services
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
)


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


def test_worker_factory_uses_read_only_worktree_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fast_calls: list[dict[str, Any]] = []
    config = SimpleNamespace(
        context_input_token_limit=1000,
        context_compaction_trigger_ratio=0.75,
        context_compaction_target_ratio=0.15,
        current_location=None,
        memory_extraction_delay_seconds=0,
    )
    resources = SimpleNamespace(
        visual_history_probe=None,
        live_view_resolver=None,
    )

    def build_fast_agent(*args: Any, **kwargs: Any) -> object:
        del args
        fast_calls.append(kwargs)
        return object()

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
    monkeypatch.setattr(services, "create_context_token_counter", lambda config: None)
    monkeypatch.setattr(
        services,
        "project_tool_profiles",
        lambda: [SimpleNamespace(profile_id="filesystem")],
    )
    monkeypatch.setattr(services, "async_task_tool_profile", lambda: object())
    monkeypatch.setattr(
        services, "build_async_subagent_middleware", lambda *args: object()
    )
    monkeypatch.setattr(services, "build_fast_agent", build_fast_agent)
    monkeypatch.setattr(services, "build_planning_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(services, "build_execution_attestation", lambda *args: object())
    monkeypatch.setattr(services, "build_coding_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(services, "build_assistant_root_graph", lambda **kwargs: object())
    monkeypatch.setattr(services, "build_memory_extraction_graph", lambda **kwargs: object())

    asyncio.run(services.AgentServerExecutionOwner.compose(store=None))

    assert isinstance(fast_calls[1]["filesystem_backend"], ReadOnlyCodingWorkspaceBackend)
    assert fast_calls[1]["filesystem_tool_names"] == ("ls", "read_file", "glob", "grep")
