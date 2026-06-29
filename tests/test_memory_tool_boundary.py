import ast
from pathlib import Path


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


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules
