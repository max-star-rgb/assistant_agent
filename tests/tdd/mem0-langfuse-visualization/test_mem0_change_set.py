from __future__ import annotations

from datetime import datetime, timezone

from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.mem0.models import (
    Mem0CompletedTurn,
    Mem0Identity,
    Mem0IngestionResult,
)


def test_ingestion_preserves_only_valid_mem0_changes() -> None:
    def transport(request):
        assert request.method == "POST"
        assert request.path == "/memories"
        return {
            "results": [
                {
                    "id": "memory-add-sentinel",
                    "memory": "用户偏好使用中文",
                    "event": "add",
                    "ignored": "provider-private-sentinel",
                },
                {
                    "id": "memory-update-sentinel",
                    "memory": "用户偏好简体中文",
                    "event": "UPDATE",
                },
                {
                    "id": "memory-delete-sentinel",
                    "event": "DELETE",
                },
                {"memory": "missing-id-sentinel", "event": "ADD"},
                {"id": "unsupported-event-sentinel", "event": "MERGE"},
                {
                    "memory_id": "alias-id-sentinel",
                    "text": "alias-text-sentinel",
                    "event": "ADD",
                },
                {
                    "id": "non-string-memory-sentinel",
                    "memory": ["not", "text"],
                    "event": "ADD",
                },
                "not-a-mapping",
            ]
        }

    client = Mem0Client(
        base_url="http://mem0.invalid",
        transport=transport,
    )

    result = client.ingest_completed_turn(
        Mem0CompletedTurn(
            identity=Mem0Identity(
                user_id="usr_00000000000000000000000000000000",
                agent_id="agt_00000000000000000000000000000000",
                run_id="run_00000000000000000000000000000000",
            ),
            user_text="request-sentinel",
            assistant_text="response-sentinel",
            occurred_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            source_turn="source-turn-sentinel",
        )
    )

    assert result.accepted is True
    assert result.memory_ids == [
        "memory-add-sentinel",
        "memory-update-sentinel",
        "memory-delete-sentinel",
    ]
    assert [change.model_dump(mode="json") for change in result.changes] == [
        {
            "memory_id": "memory-add-sentinel",
            "memory": "用户偏好使用中文",
            "event": "ADD",
        },
        {
            "memory_id": "memory-update-sentinel",
            "memory": "用户偏好简体中文",
            "event": "UPDATE",
        },
        {
            "memory_id": "memory-delete-sentinel",
            "memory": None,
            "event": "DELETE",
        },
    ]


def test_legacy_ingestion_result_distinguishes_missing_changes() -> None:
    result = Mem0IngestionResult(
        accepted=True,
        memory_ids=["memory-legacy-sentinel"],
    )

    assert result.changes is None
