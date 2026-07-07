from pathlib import Path


DEPLOYMENT_SOURCES = (
    Path("Dockerfile"),
    Path("docker-compose.yml"),
    Path(".dockerignore"),
    Path("README.md"),
    Path("AGENTS.md"),
)

OBSERVABILITY_SOURCES = (
    Path("README.md"),
    Path("docs/CONTEXT_ENGINEERING_STATUS.md"),
    Path("docs/context-engineering-walkthrough.md"),
    Path("docs/agent-communication-routing.md"),
    Path("docs/observability-harness.md"),
    Path("src/assistant_agent/api/app.py"),
    Path("src/assistant_agent/api/routes_agent.py"),
    Path("src/assistant_agent/services/trace_query.py"),
)


def test_local_deployment_sources_exist() -> None:
    for path in (
        *DEPLOYMENT_SOURCES,
        *OBSERVABILITY_SOURCES,
    ):
        assert path.exists()


def test_docker_files_default_to_mock_offline() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    combined = dockerfile + "\n" + compose

    for key in (
        "MULTIMODAL_AGENT_VISION_PROVIDER",
        "MULTIMODAL_AGENT_CHAT_PROVIDER",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER",
        "MULTIMODAL_AGENT_PRODUCT_PROVIDER",
        "MULTIMODAL_AGENT_PRICE_PROVIDER",
        "MULTIMODAL_AGENT_RENDER_PROVIDER",
        "MULTIMODAL_AGENT_VIDEO_PROVIDER",
    ):
        assert key in combined
    assert "RUN_INTEGRATION_TESTS=0" in dockerfile
    assert 'RUN_INTEGRATION_TESTS: "0"' in compose
    assert "Kubernetes" not in compose


def test_deployment_docs_do_not_contain_real_secrets() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (*DEPLOYMENT_SOURCES, Path(".env.example"))
    )

    assert "sk-" not in combined.lower()
    assert "bearer " not in combined.lower()
    assert "authorization:" not in combined.lower()
    assert "<set-in-local-shell>" in Path(".env.example").read_text(encoding="utf-8")


def test_observability_docs_cover_local_debug_fields() -> None:
    doc = "\n".join(path.read_text(encoding="utf-8") for path in OBSERVABILITY_SOURCES)

    for expected in (
        "/health",
        "GET /runs/{run_id}",
        "GET /traces/{trace_id}",
        "GET /runs/{run_id}/tool-calls",
        "provider error",
        "budget",
        "run_id",
        "trace_id",
        "tool_calls",
    ):
        assert expected in doc
