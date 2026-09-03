from pathlib import Path

from assistant_agent.identity import DEFAULT_AGENT_ID
from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID as MULTI_AGENT_DEFAULT


def test_default_agent_id_is_owned_outside_optional_multi_agent() -> None:
    assert DEFAULT_AGENT_ID == MULTI_AGENT_DEFAULT == "agent.default"

    repo_root = Path(__file__).resolve().parents[3]
    consumers = (
        "src/assistant_agent/identity.py",
        "src/assistant_agent/automation/durable_tasks/models.py",
        "src/assistant_agent/automation/notification_models.py",
        "scripts/migrate_mem0_memories_to_chinese.py",
    )
    for relative_path in consumers:
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "assistant_agent.multi_agent" not in source
