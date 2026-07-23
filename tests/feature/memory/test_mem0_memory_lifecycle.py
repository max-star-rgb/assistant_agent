"""Mem0-native add/get-all contracts used by the runtime."""

from datetime import datetime, timezone

from assistant_agent.memory.mem0.adapters import Mem0RestAdapter
from assistant_agent.memory.mem0.base import Mem0HttpRequest
from assistant_agent.schemas.mem0 import (
    Mem0ConversationMessage,
    Mem0RecallRequest,
    Mem0TurnCaptureRequest,
    Mem0Identity,
)


def _identity() -> Mem0Identity:
    return Mem0Identity(
        user_id="usr_" + "1" * 32,
        agent_id="agt_" + "2" * 32,
    )


def test_turn_capture_delegates_extraction_to_one_native_mem0_add() -> None:
    requests: list[Mem0HttpRequest] = []

    def transport(request: Mem0HttpRequest) -> dict:
        requests.append(request)
        return {"results": [{"id": "memory-1"}]}

    adapter = Mem0RestAdapter(
        base_url="http://mem0.test",
        transport=transport,
    )
    result = adapter.capture_turn(
        Mem0TurnCaptureRequest(
            identity=_identity(),
            messages=[
                Mem0ConversationMessage(
                    role="user",
                    content="我喜欢简洁回答",
                ),
                Mem0ConversationMessage(
                    role="assistant",
                    content="好的",
                ),
            ],
            occurred_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
            source_turn="turn-1",
        )
    )

    assert result.accepted is True
    assert result.memory_ids == ["memory-1"]
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.path == "/memories"
    assert request.body is not None
    assert request.body["messages"] == [
        {"role": "user", "content": "我喜欢简洁回答"},
        {"role": "assistant", "content": "好的"},
    ]
    assert "infer" not in request.body
    assert request.body["user_id"] == _identity().user_id
    assert request.body["agent_id"] == _identity().agent_id


def test_session_recall_uses_native_get_all_with_identity_filters() -> None:
    requests: list[Mem0HttpRequest] = []

    def transport(request: Mem0HttpRequest) -> dict:
        requests.append(request)
        return {
            "results": [
                {
                    "id": "memory-1",
                    "memory": "用户喜欢简洁回答",
                    "created_at": "2026-07-23T00:00:00Z",
                }
            ]
        }

    adapter = Mem0RestAdapter(
        base_url="http://mem0.test",
        transport=transport,
    )
    result = adapter.recall(
        Mem0RecallRequest(identity=_identity(), top_k=5)
    )

    assert [record.text for record in result.records] == [
        "用户喜欢简洁回答"
    ]
    assert requests == [
        Mem0HttpRequest(
            method="GET",
            path="/memories",
            query={
                "user_id": _identity().user_id,
                "agent_id": _identity().agent_id,
                "limit": "5",
            },
            timeout_seconds=5.0,
        )
    ]


def test_capture_failure_is_structured_and_does_not_raise() -> None:
    def transport(request: Mem0HttpRequest) -> dict:
        raise RuntimeError(request.path)

    adapter = Mem0RestAdapter(
        base_url="http://mem0.test",
        transport=transport,
    )
    result = adapter.capture_turn(
        Mem0TurnCaptureRequest(
            identity=_identity(),
            messages=[
                Mem0ConversationMessage(role="user", content="你好"),
                Mem0ConversationMessage(role="assistant", content="你好"),
            ],
            occurred_at=datetime.now(timezone.utc),
            source_turn="turn-1",
        )
    )

    assert result.accepted is False
    assert result.errors == [{"code": "mem0_capture_failed"}]
