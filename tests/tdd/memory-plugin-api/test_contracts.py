from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.memory.models import LongTermMemory, SessionMemorySnapshot
from assistant_agent.memory.plugins.contracts import (
    MemoryBudgetHint,
    MemoryContextItem,
    MemoryContextRequest,
    MemoryIdentity,
    MemoryMessage,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemoryPluginIssue,
    MemorySessionOpenResult,
    NeverCancelledMemoryToken,
)


def test_memory_plugin_descriptor_is_versioned_and_memory_only() -> None:
    descriptor = MemoryPluginDescriptor(
        plugin_id="probe.memory",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities={"text", "image"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )

    assert descriptor.api_version == "assistant_memory_plugin_v1"
    assert descriptor.kind == "memory"


def test_memory_context_item_rejects_prompt_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryContextItem.model_validate(
            {
                "memory_id": "memory-sentinel",
                "text": "memory-sentinel",
                "source": "long_term",
                "role": "system",
            }
        )


def test_context_request_is_frozen() -> None:
    request = MemoryContextRequest(
        memory_session_id="memory-session-sentinel",
        session_handle=None,
        identity=MemoryIdentity(
            user_id="user-sentinel",
            agent_id="agent-sentinel",
            session_id="session-sentinel",
        ),
        current_turn=MemoryMessage(role="user", text="request-sentinel"),
        media_refs=[],
        context_budget_hint=MemoryBudgetHint(max_items=8, max_chars=2048),
        deadline=datetime.now(timezone.utc),
        cancellation=NeverCancelledMemoryToken(),
    )

    with pytest.raises(ValidationError):
        request.memory_session_id = "changed"


def test_memory_context_item_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryContextItem.model_validate(
            {
                "memory_id": "memory-sentinel",
                "text": "memory-sentinel",
                "source": "long_term",
                "unexpected": "value",
            }
        )


@pytest.mark.parametrize("relevance", [-0.01, 1.01])
def test_memory_context_item_rejects_out_of_range_relevance(
    relevance: float,
) -> None:
    with pytest.raises(ValidationError):
        MemoryContextItem(
            memory_id="memory-sentinel",
            text="memory-sentinel",
            source="long_term",
            relevance=relevance,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("memory_id", ""), ("source", "untrusted_source")],
)
def test_memory_context_item_rejects_invalid_required_values(
    field: str,
    value: str,
) -> None:
    payload = {
        "memory_id": "memory-sentinel",
        "text": "memory-sentinel",
        "source": "long_term",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        MemoryContextItem.model_validate(payload)


def test_memory_context_item_rejects_non_json_metadata() -> None:
    with pytest.raises(ValidationError):
        MemoryContextItem(
            memory_id="memory-sentinel",
            text="memory-sentinel",
            source="long_term",
            metadata={"unserializable": object()},
        )


def test_memory_plugin_issue_has_bounded_message() -> None:
    with pytest.raises(ValidationError):
        MemoryPluginIssue(
            code="memory_plugin_internal_error",
            message="x" * 1025,
            recoverable=False,
        )


def test_open_session_result_has_bounded_session_handle() -> None:
    with pytest.raises(ValidationError):
        MemorySessionOpenResult(
            status="ready",
            session_handle="x" * 513,
        )


def test_legacy_mem0_memory_remains_a_standard_context_item() -> None:
    memory = LongTermMemory(
        memory_id="memory-sentinel",
        text="memory-sentinel",
        created_at=datetime.now(timezone.utc),
    )
    snapshot = SessionMemorySnapshot(memories=[memory])

    assert memory.source == "long_term"
    assert snapshot.memories[0].source == "long_term"
    assert snapshot.plugin_id is None
