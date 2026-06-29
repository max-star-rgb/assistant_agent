import ast
from pathlib import Path

from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.tools.base import ToolContext
from multimodal_agent.tools.memory_tool import MemorySaveTool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEMORY_TOOL_PATH = PROJECT_ROOT / "src/multimodal_agent/tools/memory_tool.py"


def test_memory_tool_does_not_import_memory_store_or_retrieval_backends() -> None:
    tree = ast.parse(MEMORY_TOOL_PATH.read_text(encoding="utf-8"))
    imported_modules = _imported_modules(tree)

    prohibited = {
        "multimodal_agent.memory.store",
        "multimodal_agent.memory.jsonl_store",
        "multimodal_agent.memory.retrieval",
        "multimodal_agent.memory.retriever",
        "multimodal_agent.memory.profile",
    }

    assert imported_modules.isdisjoint(prohibited)


def test_memory_save_tool_returns_confirmation_required_result() -> None:
    manager = MemoryManager(InMemoryStore())
    tool = MemorySaveTool()

    result = tool.run(
        {"content": {"summary": "我的项目路径是 /home/alice/private/project"}},
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is False
    assert result.contract is not None
    assert result.contract.status == "partial"
    assert result.data is not None
    assert result.data["requires_confirmation"] is True
    assert result.data["confirmation_id"].startswith("memory_confirmation_")
    assert manager.list_by_user("u1") == []


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules
