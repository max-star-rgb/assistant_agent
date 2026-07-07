import re
from pathlib import Path


USER_DOCS = (
    "README.md",
    "AGENTS.md",
    "docs/CONTEXT_ENGINEERING_STATUS.md",
    "docs/context-engineering-walkthrough.md",
    "docs/memory-module-walkthrough.md",
    "docs/memory-service-architecture.md",
    "docs/agent-communication-routing.md",
)


def test_user_facing_docs_exist() -> None:
    for path in USER_DOCS:
        assert Path(path).exists()


def test_readme_links_to_consolidated_docs_without_requiring_phase_docs() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "[AGENTS.md](AGENTS.md)" in readme
    assert "human navigation page" in readme
    assert "docs/development/" in readme
    assert "README 暂时只保留占位入口" not in readme
    assert "项目稳定后再重写面向人的 README" not in readme


def test_user_docs_keep_offline_safety_boundary() -> None:
    combined = "\n".join(Path(path).read_text(encoding="utf-8") for path in USER_DOCS)

    assert "mock/local/offline" in combined
    assert "不能写入仓库" in combined
    assert re.search(r"\bsk-[a-z0-9._-]{8,}", combined.lower()) is None
    assert "authorization:" not in combined.lower()
