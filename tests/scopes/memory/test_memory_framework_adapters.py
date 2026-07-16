from datetime import datetime, timezone

import pytest

from assistant_agent.memory.framework import (
    FrameworkHttpRequest,
    HindsightMemoryEngineAdapter,
    Mem0MemoryEngineAdapter,
    bind_engine_identity,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory_framework import (
    FrameworkRecallRequest,
    FrameworkRetainRequest,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        tenant_id="tenant@example.com",
        user_id="alice@example.com",
        project_id="secret-project",
        session_id="raw-session",
    )


def test_engine_identity_is_stable_and_contains_no_raw_identity() -> None:
    first = bind_engine_identity(_identity(), namespace="test-namespace")
    second = bind_engine_identity(_identity(), namespace="test-namespace")

    assert first == second
    serialized = first.model_dump_json()
    for raw_value in ("tenant@example.com", "alice@example.com", "secret-project", "raw-session"):
        assert raw_value not in serialized
    assert first.bank_id.startswith("bank_")
    assert first.user_id.startswith("usr_")
    assert first.agent_id.startswith("agt_")
    assert first.run_id.startswith("run_")


def test_hindsight_maps_retain_and_recall_to_versioned_bank_api() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        if request.path.endswith("/memories"):
            return {"success": True, "items_count": 1}
        if request.path.endswith("/memories/list"):
            return {"items": [{"id": "retained-engine-id", "text": "用户喜欢深色极简风格"}]}
        return {
            "results": [
                {
                    "id": "engine-memory-1",
                    "text": "用户喜欢深色极简风格",
                    "type": "world",
                    "occurred_start": "2026-07-14T00:00:00Z",
                }
            ]
        }

    adapter = HindsightMemoryEngineAdapter(
        base_url="http://hindsight.local",
        transport=transport,
    )
    scope = bind_engine_identity(_identity(), namespace="test")

    retained = adapter.retain(
        FrameworkRetainRequest(
            identity=scope,
            project_memory_id="memory-1",
            text="用户喜欢深色极简风格",
            memory_type="preference",
            source="explicit_user_request",
            created_at=NOW,
            idempotency_key="retain:memory-1",
        )
    )
    recalled = adapter.recall(
        FrameworkRecallRequest(identity=scope, query="用户喜欢什么风格", top_k=5)
    )

    assert retained.accepted is True
    assert retained.engine_ids == ["retained-engine-id"]
    assert requests[0].path == f"/v1/default/banks/{scope.bank_id}/memories"
    assert requests[0].body["items"][0]["document_id"] == "memory-1"
    assert requests[0].body["items"][0]["tags"] == scope.hindsight_tags
    assert requests[1].path == f"/v1/default/banks/{scope.bank_id}/memories/list"
    assert requests[1].query["document_id"] == "memory-1"
    assert requests[2].path == f"/v1/default/banks/{scope.bank_id}/memories/recall"
    assert requests[2].body["tags_match"] == "all_strict"
    assert recalled.records[0].engine_id == "engine-memory-1"
    assert recalled.records[0].relevance is None


def test_hindsight_retain_serializes_metadata_values_for_its_string_schema() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        if request.path.endswith("/memories/list"):
            return {"items": [{"id": "engine-id"}]}
        return {"success": True}

    scope = bind_engine_identity(_identity(), namespace="test")
    adapter = HindsightMemoryEngineAdapter(base_url="http://hindsight.local", transport=transport)
    adapter.retain(
        FrameworkRetainRequest(
            identity=scope,
            project_memory_id="memory-1",
            text="safe summary",
            memory_type="task",
            source="explicit_user_request",
            created_at=NOW,
            metadata={"tags": ["one", "two"], "scope": "project"},
            idempotency_key="retain:memory-1",
        )
    )

    metadata = requests[0].body["items"][0]["metadata"]
    assert metadata["tags"] == '["one", "two"]'
    assert all(isinstance(value, str) for value in metadata.values())


def test_hindsight_history_accepts_the_api_list_response() -> None:
    history = [{"id": "event-1", "event": "ADD"}]
    scope = bind_engine_identity(_identity(), namespace="test")
    adapter = HindsightMemoryEngineAdapter(
        base_url="http://hindsight.local",
        transport=lambda request: history,
    )

    assert adapter.history(identity=scope, engine_id="memory-1") == history


def test_hindsight_retain_rejects_success_without_a_queryable_memory() -> None:
    def transport(request: FrameworkHttpRequest):
        if request.path.endswith("/memories/list"):
            return {"items": []}
        return {"success": True}

    scope = bind_engine_identity(_identity(), namespace="test")
    adapter = HindsightMemoryEngineAdapter(base_url="http://hindsight.local", transport=transport)
    result = adapter.retain(
        FrameworkRetainRequest(
            identity=scope,
            project_memory_id="memory-1",
            text="safe summary",
            memory_type="task",
            source="explicit_user_request",
            created_at=NOW,
            idempotency_key="retain:memory-1",
        )
    )

    assert result.accepted is False


@pytest.mark.parametrize(
    ("scope_name", "expected_filter_keys"),
    [
        ("session", {"user_id", "agent_id", "run_id"}),
        ("project", {"user_id", "agent_id"}),
        ("task", {"user_id", "agent_id"}),
        ("user_profile", {"user_id"}),
    ],
)
def test_mem0_uses_scope_aware_filters_and_never_accepts_model_supplied_identity(
    scope_name,
    expected_filter_keys,
) -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        if request.path == "/memories":
            return {"results": [{"id": "mem0-1", "memory": "偏好深色", "event": "ADD"}]}
        return {"results": [{"id": "mem0-1", "memory": "偏好深色", "score": 0.91}]}

    adapter = Mem0MemoryEngineAdapter(base_url="http://mem0.local", transport=transport)
    scope = bind_engine_identity(_identity(), namespace="test")
    adapter.retain(
        FrameworkRetainRequest(
            identity=scope,
            project_memory_id="memory-1",
            text="偏好深色",
            memory_type="preference",
            source="explicit_user_request",
            created_at=NOW,
            metadata={"user_id": "attacker", "project_id": "attacker"},
            idempotency_key="retain:memory-1",
            scope=scope_name,
        )
    )
    recalled = adapter.recall(FrameworkRecallRequest(identity=scope, query="偏好", top_k=3, scope=scope_name))

    retain_body = requests[0].body
    expected_filters = scope.mem0_filters_for_scope(scope_name)
    assert {key for key in retain_body if key in {"user_id", "agent_id", "run_id"}} == expected_filter_keys
    assert {key: retain_body[key] for key in expected_filter_keys} == expected_filters
    assert retain_body["infer"] is False
    assert retain_body["metadata"]["project_memory_id"] == "memory-1"
    assert "user_id" not in retain_body["metadata"]
    assert requests[1].path == "/search"
    assert requests[1].body["filters"] == expected_filters
    assert recalled.records[0].relevance == 0.91


def test_framework_request_rejects_raw_or_secret_payloads() -> None:
    scope = bind_engine_identity(_identity(), namespace="test")

    with pytest.raises(ValueError, match="unsafe framework memory payload"):
        FrameworkRetainRequest(
            identity=scope,
            project_memory_id="memory-1",
            text="safe summary",
            memory_type="task",
            source="explicit_user_request",
            created_at=NOW,
            metadata={"raw_provider_response": {"token": "secret"}},
            idempotency_key="retain:memory-1",
        )
