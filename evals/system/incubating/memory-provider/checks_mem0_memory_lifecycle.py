"""Mem0-native add/get-all contracts used by the runtime."""

from datetime import datetime, timezone

from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.transport import Mem0HttpRequest
from assistant_agent.memory.models import CompletedTurn
from assistant_agent.identity import RequestIdentity


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="memory-user",
        agent_id="memory-agent",
        session_id="memory-session",
    )


def test_turn_ingestion_delegates_extraction_to_one_native_mem0_add() -> None:
    requests: list[Mem0HttpRequest] = []

    def transport(request: Mem0HttpRequest) -> dict:
        requests.append(request)
        return {"results": [{"id": "memory-1"}]}

    client = Mem0Client(
        base_url="http://mem0.test",
        identity_namespace="test",
        transport=transport,
    )
    result = client.ingest_completed_turn(
        CompletedTurn(
            identity=_identity(),
            user_text="我喜欢简洁回答",
            assistant_text="好的",
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
    engine_identity = bind_mem0_identity(_identity(), namespace="test")
    assert request.body["user_id"] == engine_identity.user_id
    assert request.body["agent_id"] == engine_identity.agent_id
    assert request.body["run_id"] == engine_identity.run_id
    assert request.timeout_seconds == 30.0


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

    client = Mem0Client(
        base_url="http://mem0.test",
        identity_namespace="test",
        transport=transport,
    )
    result = client.recall_long_term_memory(_identity(), top_k=5)

    assert [memory.text for memory in result] == [
        "用户喜欢简洁回答"
    ]
    engine_identity = bind_mem0_identity(_identity(), namespace="test")
    assert requests == [
        Mem0HttpRequest(
            method="GET",
            path="/memories",
            query={
                "user_id": engine_identity.user_id,
                "agent_id": engine_identity.agent_id,
                "limit": "5",
            },
            timeout_seconds=5.0,
        )
    ]


def test_ingestion_failure_is_structured_and_does_not_raise() -> None:
    def transport(request: Mem0HttpRequest) -> dict:
        raise RuntimeError(request.path)

    client = Mem0Client(
        base_url="http://mem0.test",
        identity_namespace="test",
        transport=transport,
    )
    result = client.ingest_completed_turn(
        CompletedTurn(
            identity=_identity(),
            user_text="你好",
            assistant_text="你好",
            occurred_at=datetime.now(timezone.utc),
            source_turn="turn-1",
        )
    )

    assert result.accepted is False
    assert result.errors == [{"code": "mem0_ingestion_failed"}]
