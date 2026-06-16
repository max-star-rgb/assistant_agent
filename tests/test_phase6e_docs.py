from pathlib import Path


USER_DOCS = (
    "README.md",
    "docs/quickstart.md",
    "docs/architecture.md",
    "docs/capabilities.md",
    "docs/configuration.md",
    "docs/provider-setup.md",
    "docs/demo-flows.md",
    "docs/deployment-local.md",
    "docs/development.md",
    "docs/security.md",
    "docs/troubleshooting.md",
    "docs/release-checklist.md",
)


def test_user_facing_docs_exist() -> None:
    for path in USER_DOCS:
        assert Path(path).exists()


def test_readme_links_to_consolidated_docs_without_requiring_phase_docs() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for path in USER_DOCS[1:]:
        assert f"({path})" in readme
    assert "ordinary users should not need to read them" in readme


def test_user_docs_keep_offline_safety_boundary() -> None:
    combined = "\n".join(Path(path).read_text(encoding="utf-8") for path in USER_DOCS)

    assert "mock/local/offline" in combined
    assert "Do not commit" in combined
    assert "sk-" not in combined.lower()
    assert "bearer " not in combined.lower()
    assert "authorization:" not in combined.lower()
