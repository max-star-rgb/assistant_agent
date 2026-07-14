import re
from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_memory_tools_remain_thin_and_do_not_import_memory_backends_or_policy() -> None:
    source = _source("src/assistant_agent/tools/memory_tool.py")

    assert "from assistant_agent.memory.store" not in source
    assert "from assistant_agent.memory.retrieval" not in source
    assert "from assistant_agent.memory.retriever" not in source
    assert "from assistant_agent.memory.write_policy" not in source
    assert "MemoryManager" in source


def test_context_builder_does_not_own_memory_store_or_write_policy() -> None:
    context_paths = [
        "src/assistant_agent/services/context/builder.py",
        "src/assistant_agent/services/context/renderer.py",
        "src/assistant_agent/services/context/report.py",
    ]
    for path in context_paths:
        source = _source(path)
        assert "from assistant_agent.memory.store" not in source
        assert "from assistant_agent.memory.write_policy" not in source
        assert re.search(r"\bMemoryStore\b", source) is None


def test_agent_delegation_context_filters_parent_memory_and_history() -> None:
    source = _source("src/assistant_agent/services/agent_delegation_context.py")

    assert "memory_context" in source
    assert "conversation_history" in source
    assert "omitted_context" in source
    assert "provider_payload" in source
    assert "provider_response" in source
    assert "raw_or_secret_payload_not_forwarded" in source


def test_default_registry_does_not_enable_delegation_by_default() -> None:
    source = _source("src/assistant_agent/tools/registry.py")

    assert "enable_agent_delegation: bool = False" in source
    assert "delegate_to_agent" in source
