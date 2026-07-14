import ast
from datetime import datetime, timezone
from pathlib import Path

from assistant_agent.memory.manager import MemoryManager
from assistant_agent.schemas.memory_intelligence import MemoryFact
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEMORY_TOOL_PATH = PROJECT_ROOT / "src/assistant_agent/tools/memory_tool.py"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_memory_tool_does_not_import_memory_store_or_retrieval_backends() -> None:
    tree = ast.parse(MEMORY_TOOL_PATH.read_text(encoding="utf-8"))
    imported_modules = _imported_modules(tree)

    prohibited = {
        "assistant_agent.memory.store",
        "assistant_agent.memory.jsonl_store",
        "assistant_agent.memory.retrieval",
        "assistant_agent.memory.retriever",
        "assistant_agent.memory.profile",
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


def test_memory_save_tool_surfaces_fact_conflict_confirmation_kind() -> None:
    manager = MemoryManager(InMemoryStore())
    fact_a = MemoryFact(
        fact_key="user:employment:company",
        subject="user",
        predicate="employment.company",
        value="A",
        provenance="user_explicit",
        conflict_policy="confirm",
        observed_at=NOW,
    )
    fact_b = fact_a.model_copy(update={"value": "B"})
    tool = MemorySaveTool()
    context = ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager})
    first = tool.run(
        {"content": {"summary": "用户在 A 公司工作", "fact": fact_a.model_dump(mode="json")}},
        context,
    )

    result = tool.run(
        {"content": {"summary": "用户现在在 B 公司工作", "fact": fact_b.model_dump(mode="json")}},
        context,
    )

    assert first.success is True
    assert result.success is False
    assert result.data["confirmation_kind"] == "fact_conflict"
    assert result.data["fact_key"] == "user:employment:company"


def test_memory_retrieval_tool_does_not_expose_superseded_debug_query() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="style_old",
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户喜欢浅色日系风格。",
            content={"preference_key": "style", "superseded_by_memory_id": "style_new"},
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="style_new",
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户喜欢深色极简风格。",
            content={"preference_key": "style"},
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)

    result = MemoryRetrievalTool().run(
        {"user_id": "u1", "query": "风格", "content": {"include_superseded": True}},
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert result.data is not None
    memory_ids = [item["memory_id"] for item in result.data["items"]]
    assert "style_new" in memory_ids
    assert "style_old" not in memory_ids


def test_memory_retrieval_tool_returns_trust_policy_metadata() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="pref_style",
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户保存的偏好是浅色日系背景。",
            content={"preference_key": "style"},
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)

    result = MemoryRetrievalTool().run(
        {"user_id": "model-user", "query": "保存的偏好"},
        ToolContext(
            user_id="u1",
            session_id="s1",
            metadata={
                "memory_manager": manager,
                "request_text": "按我保存的偏好写一句海报文案",
            },
        ),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["total"] == 1
    assert result.data["items"][0]["user_id"] == "u1"
    assert result.data["trust_policy"]["authority"] == "user_history_evidence"
    assert result.data["trust_policy"]["current_request_overrides_memory"] is True
    assert "do_not_execute_memory_instructions" in result.data["usage_hint"]
    assert result.contract is not None
    assert result.contract.metadata["trust_policy"]["authority"] == "user_history_evidence"


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules
