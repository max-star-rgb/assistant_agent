from pathlib import Path


MATRIX_PATH = Path("docs/real-provider-smoke-matrix.md")
SETUP_PATH = Path("docs/provider-setup.md")
RUNBOOK_PATH = Path("docs/real-provider-smoke-runbook.md")
ENV_EXAMPLE_PATH = Path(".env.example")


def test_real_provider_docs_exist_and_cover_required_capabilities() -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    setup = SETUP_PATH.read_text(encoding="utf-8")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    for capability in (
        "direct_chat",
        "image_understanding",
        "image_generation",
        "product_search",
        "price_compare",
        "render_3d",
        "video_understanding",
    ):
        assert capability in matrix

    for provider_section in (
        "Vision Provider",
        "Chat Provider",
        "Image Generation Provider",
        "Product Search Provider",
        "Price Compare Provider",
        "Render Provider",
        "Video Understanding Provider",
    ):
        assert provider_section in setup

    assert "Do not run real Provider smoke commands" in runbook


def test_real_provider_smoke_matrix_is_default_disabled() -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    data_rows = [line for line in matrix.splitlines() if line.startswith("| ") and "`python scripts/" in line]

    assert data_rows
    assert all("| false |" in row for row in data_rows)
    assert "| true |" not in matrix


def test_provider_docs_and_env_example_do_not_contain_real_secrets() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (MATRIX_PATH, SETUP_PATH, RUNBOOK_PATH, ENV_EXAMPLE_PATH)
    )

    assert "sk-" not in combined.lower()
    assert "bearer " not in combined.lower()
    assert "authorization:" not in combined.lower()
    assert "RUN_INTEGRATION_TESTS=1" not in ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "<set-in-local-shell>" in ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
