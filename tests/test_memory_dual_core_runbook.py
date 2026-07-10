from pathlib import Path


def test_memory_dual_core_runbook_covers_recommended_configuration_modes() -> None:
    runbook = Path("docs/development/memory-dual-core-operator-runbook.md").read_text(
        encoding="utf-8"
    )

    for expected in (
        "MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite",
        "MULTIMODAL_AGENT_MEMORY_BACKEND=dual_core",
        "MULTIMODAL_AGENT_MEMORY_LOCAL_BACKEND=sqlite",
        "MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true",
        "MEMORY_SERVER_BASE_URL=http://127.0.0.1:5200",
        "MULTIMODAL_AGENT_MEMORY_BACKEND=remote_service",
        "MULTIMODAL_AGENT_MEMORY_REMOTE_SERVICE_ADAPTER=http",
        "HttpRemoteMemoryServiceAdapter",
        "hybrid_remote",
        "legacy alias",
        "memory_core_status",
        "memory_remote_degraded",
        "memory_remote_lifecycle_failed",
        "scripts/smoke_memory_dual_core.py",
        "--offline-only",
        "--memory-server-base-url",
        "scripts/run_evals.py --suite memory_quality",
        "scripts/smoke_memory_server.py",
    ):
        assert expected in runbook
    assert "sk-" not in runbook.lower()
    assert "authorization:" not in runbook.lower()


def test_memory_architecture_links_dual_core_operator_runbook() -> None:
    architecture = Path("docs/memory-service-architecture.md").read_text(
        encoding="utf-8"
    )

    assert "docs/development/memory-dual-core-operator-runbook.md" in architecture
