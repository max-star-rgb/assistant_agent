from pathlib import Path


def test_local_deployment_files_exist() -> None:
    for path in (
        Path("Dockerfile"),
        Path("docker-compose.yml"),
        Path(".dockerignore"),
        Path("docs/configuration.md"),
        Path("docs/deployment-local.md"),
        Path("docs/observability-local.md"),
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
        path.read_text(encoding="utf-8")
        for path in (
            Path("docs/configuration.md"),
            Path("docs/deployment-local.md"),
            Path("docs/observability-local.md"),
            Path("Dockerfile"),
            Path("docker-compose.yml"),
        )
    )

    assert "sk-" not in combined.lower()
    assert "bearer " not in combined.lower()
    assert "authorization:" not in combined.lower()
    assert "<set-in-local-shell>" not in combined


def test_observability_docs_cover_local_debug_fields() -> None:
    doc = Path("docs/observability-local.md").read_text(encoding="utf-8")

    for expected in (
        "GET /health",
        "GET /runs/{run_id}",
        "GET /traces/{trace_id}",
        "GET /runs/{run_id}/tool-calls",
        "provider errors",
        "budget",
        "Memory Operations",
        "run_id",
        "trace_id",
        "tool_calls",
    ):
        assert expected in doc
