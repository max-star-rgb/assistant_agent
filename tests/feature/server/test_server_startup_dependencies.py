from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import scripts.run_server as run_server

from assistant_agent.config import ProviderConfig
from assistant_agent.services.startup_dependencies import (
    StartupDependencyStatus,
    collect_startup_dependency_statuses,
    format_startup_dependency_statuses,
)


def test_startup_dependencies_report_ready_services_and_export_state() -> None:
    requested_urls: list[str] = []

    def probe(url: str, _timeout: float) -> Mapping[str, Any]:
        requested_urls.append(url)
        if url.endswith("/ready"):
            return {"status": "ok", "framework": "mem0", "version": "2.0.11"}
        return {"status": "OK", "version": "3.120.0"}

    statuses = collect_startup_dependency_statuses(
        ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            qwen_api_key="test-chat-key",
            memory_backend="framework",
            memory_framework="mem0",
            memory_framework_base_url="http://127.0.0.1:8890",
            search_provider="tavily",
            tavily_api_key="test-tavily-key",
        ),
        env={
            "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
            "LANGFUSE_HOST": "http://127.0.0.1:3000",
        },
        probe=probe,
    )

    assert statuses == (
        StartupDependencyStatus(name="Memo", state="ready", detail="mem0 2.0.11"),
        StartupDependencyStatus(name="Langfuse", state="ready", detail="export enabled"),
        StartupDependencyStatus(name="Web search", state="ready", detail="tavily"),
    )
    assert sorted(requested_urls) == [
        "http://127.0.0.1:3000/api/public/health",
        "http://127.0.0.1:8890/ready",
    ]
    assert format_startup_dependency_statuses(statuses) == [
        "Dependencies:",
        "  Memo: ready (mem0 2.0.11)",
        "  Langfuse: ready (export enabled)",
        "  Web search: ready (tavily)",
    ]


def test_startup_dependencies_are_disabled_when_not_in_use() -> None:
    def probe(_url: str, _timeout: float) -> Mapping[str, Any]:
        raise OSError("service is not running")

    statuses = collect_startup_dependency_statuses(
        ProviderConfig(),
        env={"ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "false"},
        probe=probe,
    )

    assert statuses == (
        StartupDependencyStatus(name="Memo", state="disabled"),
        StartupDependencyStatus(name="Langfuse", state="disabled"),
        StartupDependencyStatus(name="Web search", state="ready", detail="mock"),
    )


def test_langfuse_can_be_ready_while_export_is_disabled() -> None:
    statuses = collect_startup_dependency_statuses(
        ProviderConfig(),
        env={"ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "false"},
        probe=lambda _url, _timeout: {"status": "OK"},
    )

    assert statuses == (
        StartupDependencyStatus(name="Memo", state="disabled"),
        StartupDependencyStatus(name="Langfuse", state="ready", detail="export disabled"),
        StartupDependencyStatus(name="Web search", state="ready", detail="mock"),
    )


def test_startup_dependencies_fail_open_when_enabled_services_are_unavailable() -> None:
    def probe(_url: str, _timeout: float) -> Mapping[str, Any]:
        raise TimeoutError("probe timed out")

    statuses = collect_startup_dependency_statuses(
        ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            qwen_api_key="test-chat-key",
            memory_backend="framework",
            memory_framework="mem0",
            memory_framework_base_url="http://127.0.0.1:8890",
            search_provider="tavily",
        ),
        env={"ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true"},
        probe=probe,
    )

    assert statuses == (
        StartupDependencyStatus(name="Memo", state="unavailable"),
        StartupDependencyStatus(name="Langfuse", state="unavailable", detail="export enabled"),
        StartupDependencyStatus(name="Web search", state="unavailable", detail="tavily"),
    )


def test_server_launcher_prints_compact_dependency_summary(monkeypatch, capsys) -> None:
    statuses = (
        StartupDependencyStatus(name="Memo", state="ready", detail="mem0 2.0.11"),
        StartupDependencyStatus(name="Langfuse", state="ready", detail="export enabled"),
        StartupDependencyStatus(name="Web search", state="ready", detail="tavily"),
    )
    monkeypatch.setattr(
        run_server,
        "collect_startup_dependency_statuses",
        lambda _config: statuses,
    )

    run_server._print_startup_summary(  # noqa: SLF001 - launcher output contract
        SimpleNamespace(host="127.0.0.1", port=8089),
        ProviderConfig(),
    )

    assert capsys.readouterr().out.splitlines() == [
        "Provider mode: mock",
        "Main LLM: mock / default",
        "Dependencies:",
        "  Memo: ready (mem0 2.0.11)",
        "  Langfuse: ready (export enabled)",
        "  Web search: ready (tavily)",
        "Services:",
        "  media_agent: ws://127.0.0.1:8089/agent-service/v1",
    ]
