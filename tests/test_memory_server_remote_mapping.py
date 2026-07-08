from datetime import datetime, timezone

import pytest

from assistant_agent.memory import remote as remote_module
from assistant_agent.memory.remote import (
    HybridMemoryStore,
    MemoryServerMediaFile,
    MemoryServerRequest,
    MemoryServiceOperationError,
    MemoryServerTaskStatusResult,
    MemoryServerUploadResult,
    RemoteMemoryClient,
    RemoteServiceMemoryStore,
    memory_search_result_from_memory_server_response,
)
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.memory_audit import MemoryPendingConfirmation


class FakeRemoteServiceAdapter:
    def __init__(self) -> None:
        self.saved: list[MemoryItem] = []
        self.deleted: list[tuple[str, str, bool]] = []
        self.save_response: dict | None = None
        self.fail_save = False

    def search(self, query: MemoryQuery):
        return {
            "items": [],
            "query_used": query.model_dump(mode="json"),
            "total": 0,
            "ranking_reason": "remote_service_search",
            "memory_context": "",
            "errors": [],
        }

    def save_explicit(self, item: MemoryItem):
        self.saved.append(item)
        if self.fail_save:
            raise TimeoutError("remote service timeout token=secret")
        return self.save_response or item.model_dump(mode="json")

    def record_candidate(self, payload: dict):
        return {"candidate_id": "candidate-1", "written": False}

    def confirm(self, *, user_id: str, confirmation_id: str):
        return {"confirmation_id": confirmation_id, "status": "confirmed"}

    def reject(self, *, user_id: str, confirmation_id: str):
        return {"confirmation_id": confirmation_id, "status": "rejected"}

    def delete(self, *, user_id: str, memory_id: str, hard: bool = False) -> bool:
        self.deleted.append((user_id, memory_id, hard))
        return True

    def export(self, *, user_id: str):
        return []

    def audit(self, *, user_id: str):
        return [{"event_type": "memory_explicit_saved", "memory_id": "remote-m1"}]

    def health(self):
        return {"status": "ok"}


def test_memory_server_mapping_converts_text_results_and_folds_keyframes_into_artifact_refs() -> None:
    query = MemoryQuery(user_id="u1", session_id="s1", query="早餐", top_k=5)
    response = {
        "results": [
            {
                "type": "text",
                "content": "Jake had breakfast with coffee and toast.",
                "score": 1.2,
                "memory_type": "episodic",
                "source": {
                    "memory_id": "remote-text-1",
                    "source_id": "task-1",
                    "timestamp_start": "2026-04-11T12:00:03+00:00",
                    "timestamp_end": "2026-04-11T12:00:06+00:00",
                },
                "image_url": "file:///data/keyframes/video1/4500.jpg",
                "image_base64": "must-not-leak",
                "metadata": {"topic": "breakfast", "subtopic": "coffee", "large_raw": "ignored"},
            },
            {
                "type": "image",
                "content": "",
                "score": 1.0,
                "memory_type": "keyframe",
                "source": {
                    "memory_id": "remote-text-1",
                    "task_id": "task-1",
                    "file_id": "video1",
                    "timestamp_absolute": "2026-04-11T12:00:04.5+00:00",
                },
                "media": {
                    "kind": "keyframe",
                    "url": "file:///data/keyframes/video1/4500.jpg",
                    "base64": "must-not-leak",
                },
                "image_url": "file:///data/keyframes/video1/4500.jpg",
                "image_base64": "must-not-leak",
                "metadata": {"rank": 0},
            },
        ],
        "total_results": 2,
    }

    result = memory_search_result_from_memory_server_response(response, query)

    assert result.query_used == query
    assert result.total == 1
    assert result.ranking_reason == "memory_server_remote_query"
    assert result.memory_context == "Jake had breakfast with coffee and toast."

    item = result.items[0]
    assert item.memory_id == "memory_server:remote-text-1"
    assert item.user_id == "u1"
    assert item.session_id == "s1"
    assert item.memory_type == "task"
    assert item.source == "memory_server"
    assert item.summary == "Jake had breakfast with coffee and toast."
    assert item.relevance == 1.0
    assert item.created_at == datetime(2026, 4, 11, 12, 0, 3, tzinfo=timezone.utc)
    assert item.tags == ["memory_server", "episodic", "breakfast", "coffee"]
    assert item.artifact_refs == ["file:///data/keyframes/video1/4500.jpg"]
    assert item.content == {
        "remote_memory_type": "episodic",
        "source_id": "task-1",
        "timestamp_start": "2026-04-11T12:00:03+00:00",
        "timestamp_end": "2026-04-11T12:00:06+00:00",
        "topic": "breakfast",
        "subtopic": "coffee",
    }


def test_remote_service_store_delegates_lifecycle_to_adapter_and_validates_response() -> None:
    adapter = FakeRemoteServiceAdapter()
    adapter.save_response = {
        "memory_id": "remote-m1",
        "user_id": "untrusted-user",
        "session_id": "untrusted-session",
        "memory_type": "task",
        "summary": "Remote service saved safe summary.",
        "source": "remote_service",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    store = RemoteServiceMemoryStore(adapter=adapter)
    item = MemoryItem(
        memory_id="local-m1",
        user_id="trusted-user",
        session_id="trusted-session",
        memory_type="task",
        summary="Local request summary.",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    saved = store.save(item)
    deleted = store.delete("trusted-user", "remote-m1")
    hard_deleted = store.hard_delete("trusted-user", "remote-m1")

    assert adapter.saved == [item]
    assert saved.memory_id == "remote-m1"
    assert saved.user_id == "trusted-user"
    assert saved.session_id == "trusted-session"
    assert saved.source == "remote_service"
    assert deleted is True
    assert hard_deleted is True
    assert adapter.deleted == [
        ("trusted-user", "remote-m1", False),
        ("trusted-user", "remote-m1", True),
    ]
    assert store.audit("trusted-user") == [{"event_type": "memory_explicit_saved", "memory_id": "remote-m1"}]
    assert store.health() == {"status": "ok"}


def test_remote_service_store_rejects_unsafe_raw_payload_in_remote_response() -> None:
    adapter = FakeRemoteServiceAdapter()
    adapter.save_response = {
        "memory_id": "remote-unsafe",
        "user_id": "u1",
        "session_id": "s1",
        "memory_type": "task",
        "summary": "Unsafe response.",
        "content": {"raw_provider_payload": {"secret": "must-not-enter-memory"}},
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    store = RemoteServiceMemoryStore(adapter=adapter)
    item = MemoryItem(
        memory_id="local-m1",
        user_id="u1",
        session_id="s1",
        memory_type="task",
        summary="Local request summary.",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError):
        store.save(item)


def test_remote_service_store_failure_raises_recoverable_error_without_raw_secret() -> None:
    adapter = FakeRemoteServiceAdapter()
    adapter.fail_save = True
    store = RemoteServiceMemoryStore(adapter=adapter)
    item = MemoryItem(
        memory_id="local-m1",
        user_id="u1",
        session_id="s1",
        memory_type="task",
        summary="Local request summary.",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(MemoryServiceOperationError) as exc_info:
        store.save(item)

    assert exc_info.value.operation == "save_explicit"
    assert exc_info.value.recoverable is True
    assert "secret" not in str(exc_info.value).lower()


def test_remote_service_store_rebinds_identity_for_search_result_objects() -> None:
    class SearchResultAdapter(FakeRemoteServiceAdapter):
        def search(self, query: MemoryQuery):
            return memory_search_result_from_memory_server_response(
                {
                    "results": [
                        {
                            "type": "text",
                            "content": "Remote preference says short answers are preferred.",
                            "score": 0.9,
                            "memory_type": "preference",
                            "source": {
                                "memory_id": "pref-short",
                                "timestamp_start": "2026-01-01T00:00:00+00:00",
                            },
                        }
                    ]
                },
                MemoryQuery(user_id="untrusted-user", session_id="untrusted-session", query="pref"),
            )

    store = RemoteServiceMemoryStore(adapter=SearchResultAdapter())

    result = store.search(MemoryQuery(user_id="trusted-user", session_id="trusted-session", query="pref"))

    assert result.total == 1
    assert result.items[0].user_id == "trusted-user"
    assert result.items[0].session_id == "trusted-session"


def test_memory_server_mapping_binds_identity_from_query_and_drops_unsafe_media_payloads() -> None:
    query = MemoryQuery(user_id="trusted-user", session_id="trusted-session", query="手机在哪里")
    response = {
        "results": [
            {
                "type": "text",
                "content": "The phone was placed on the kitchen counter.",
                "score": -2,
                "memory_type": "spatial",
                "source": {
                    "memory_id": "phone-location",
                    "source_id": "upload-task-1",
                    "timestamp_start": "2026-04-11T10:00:00Z",
                    "timestamp_end": "2026-04-11T10:01:00Z",
                    "user_id": "untrusted-remote-user",
                    "session_id": "untrusted-remote-session",
                },
                "media": {
                    "kind": "keyframe",
                    "url": "data:image/png;base64,unsafe",
                    "base64": "unsafe",
                },
                "image_url": "data:image/png;base64,unsafe",
                "image_base64": "unsafe",
                "metadata": {"topic": "phone", "subtopic": "location"},
            },
            {
                "type": "image",
                "memory_type": "keyframe",
                "source": {"memory_id": "phone-location"},
                "media": {"url": "data:image/png;base64,unsafe", "base64": "unsafe"},
                "image_url": "data:image/png;base64,unsafe",
            },
        ]
    }

    result = memory_search_result_from_memory_server_response(response, query)

    assert result.total == 1
    item = result.items[0]
    assert item.user_id == "trusted-user"
    assert item.session_id == "trusted-session"
    assert item.memory_type == "video"
    assert item.relevance == 0.0
    assert item.artifact_refs == []
    assert "base64" not in str(item.content).lower()
    assert "unsafe" not in str(item.content).lower()


def test_memory_server_mapping_returns_prompt_safe_errors_for_malformed_results() -> None:
    query = MemoryQuery(user_id="u1", query="anything")
    response = {
        "results": [
            {
                "type": "text",
                "content": "safe item",
                "score": 0.5,
                "memory_type": "semantic",
                "source": {"memory_id": "safe", "timestamp_start": "2026-04-11T10:00:00+00:00"},
            },
            {
                "type": "text",
                "content": "",
                "score": 0.5,
                "memory_type": "semantic",
                "source": {"memory_id": "bad", "timestamp_start": "2026-04-11T10:00:00+00:00"},
                "metadata": {"topic": "empty"},
            },
        ]
    }

    result = memory_search_result_from_memory_server_response(response, query)

    assert [item.memory_id for item in result.items] == ["memory_server:safe"]
    assert result.total == 1
    assert result.errors == [
        {
            "code": "memory_server_result_rejected",
            "message": "remote memory result rejected",
            "recoverable": True,
            "memory_id": "bad",
        }
    ]


def test_remote_memory_client_queries_memory_server_with_safe_defaults() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "results": [
                {
                    "type": "text",
                    "content": "Remote memory hit.",
                    "score": 0.75,
                    "memory_type": "semantic",
                    "source": {
                        "memory_id": "remote-hit",
                        "timestamp_start": "2026-04-11T10:00:00+00:00",
                    },
                }
            ]
        }

    client = RemoteMemoryClient(
        base_url="http://memory.local",
        timeout_seconds=1.5,
        query_strategy="vector",
        include_media_chunks=False,
        transport=transport,
    )
    query = MemoryQuery(
        user_id="u1",
        session_id="s1",
        query="remote",
        top_k=3,
        since=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    result = client.query_memories(query)

    assert [item.memory_id for item in result.items] == ["memory_server:remote-hit"]
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.path == "/v1/memories/query"
    assert request.timeout_seconds == 1.5
    assert request.body == {
        "user_id": "u1",
        "session_id": "s1",
        "query": "remote",
        "top_k": 3,
        "direct_answer": False,
        "after_timestamp": "2026-04-01T00:00:00+00:00",
        "options": {
            "strategy": "vector",
            "include_media_chunks": False,
            "trace": False,
        },
    }


def test_remote_memory_client_returns_search_error_when_transport_fails() -> None:
    def transport(request: MemoryServerRequest) -> dict:
        raise TimeoutError("request timed out with token=secret")

    client = RemoteMemoryClient(
        base_url="http://memory.local",
        timeout_seconds=0.1,
        transport=transport,
    )
    query = MemoryQuery(user_id="u1", query="remote")

    result = client.query_memories(query)

    assert result.items == []
    assert result.total == 0
    assert result.ranking_reason == "memory_server_remote_query_failed"
    assert result.errors == [
        {
            "code": "memory_server_query_failed",
            "message": "memory server query failed",
            "recoverable": True,
            "detail": "request timed out with [redacted]",
        }
    ]


def test_remote_memory_client_health_uses_scoped_get_request() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {"status": "ok", "version": "0.1.0", "code": 200}

    client = RemoteMemoryClient(base_url="http://memory.local", transport=transport)

    response = client.health(user_id="u1", session_id="s1")

    assert response["status"] == "ok"
    assert requests == [
        MemoryServerRequest(
            method="GET",
            path="/v1/health?user_id=u1&session_id=s1",
            body=None,
            timeout_seconds=2.0,
        )
    ]


def test_remote_memory_client_loopback_base_url_bypasses_global_proxy_urlopen(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"status": "ok", "code": 200}'

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse()

    def fake_urlopen(*args, **kwargs):
        raise AssertionError("loopback Memory Server requests must bypass global proxy urlopen")

    def fake_proxy_handler(proxies):
        captured["proxy_handler"] = dict(proxies)
        return ("proxy_handler", dict(proxies))

    def fake_build_opener(handler):
        captured["opener_handler"] = handler
        return FakeOpener()

    monkeypatch.setattr(remote_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(remote_module.urllib.request, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(remote_module.urllib.request, "build_opener", fake_build_opener)

    client = RemoteMemoryClient(base_url="http://127.0.0.1:5200", timeout_seconds=0.5)

    assert client.health(user_id="u1")["status"] == "ok"
    assert captured["proxy_handler"] == {}
    assert captured["opener_handler"] == ("proxy_handler", {})
    assert captured["url"] == "http://127.0.0.1:5200/v1/health?user_id=u1"
    assert captured["timeout"] == 0.5


def test_remote_memory_client_upload_media_posts_file_refs_and_returns_task_result() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "task_id": "20260411T120000Z-a1b2c3",
            "status": "processing",
            "accepted_count": 1,
            "code": 202,
        }

    client = RemoteMemoryClient(base_url="http://memory.local", timeout_seconds=3.0, transport=transport)

    result = client.upload_media(
        user_id="u1",
        session_id="s1",
        files=[
            MemoryServerMediaFile(
                file_id="assistant-agent-u1-s1-video-1",
                file_url="file:///tmp/breakfast.mp4",
                filename="breakfast.mp4",
                media_type="video",
                start_time=datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc),
                metadata={"topic": "breakfast"},
            )
        ],
    )

    assert result == MemoryServerUploadResult(
        task_id="20260411T120000Z-a1b2c3",
        status="processing",
        accepted_count=1,
        code=202,
    )
    assert requests == [
        MemoryServerRequest(
            method="POST",
            path="/v1/media/upload",
            body={
                "user_id": "u1",
                "session_id": "s1",
                "files": [
                    {
                        "file_id": "assistant-agent-u1-s1-video-1",
                        "file_url": "file:///tmp/breakfast.mp4",
                        "filename": "breakfast.mp4",
                        "media_type": "video",
                        "start_time": "2026-04-11T12:00:00Z",
                        "metadata": {"topic": "breakfast"},
                    }
                ],
            },
            timeout_seconds=3.0,
        )
    ]


def test_remote_memory_client_upload_media_rejects_raw_or_base64_metadata() -> None:
    try:
        MemoryServerMediaFile(
            file_id="f1",
            file_url="file:///tmp/breakfast.mp4",
            filename="breakfast.mp4",
            media_type="video",
            start_time=datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc),
            metadata={"base64": "must-not-send"},
        )
    except ValueError as exc:
        assert "unsafe media upload metadata key" in str(exc)
    else:
        raise AssertionError("raw/base64 metadata must be rejected before upload")


def test_remote_memory_client_upload_media_returns_safe_error_when_transport_fails() -> None:
    def transport(request: MemoryServerRequest) -> dict:
        raise TimeoutError("upload timed out with token=secret")

    client = RemoteMemoryClient(base_url="http://memory.local", timeout_seconds=0.1, transport=transport)

    result = client.upload_media(
        user_id="u1",
        session_id="s1",
        files=[
            MemoryServerMediaFile(
                file_id="f1",
                file_url="file:///tmp/breakfast.mp4",
                filename="breakfast.mp4",
                media_type="video",
                start_time=datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc),
            )
        ],
    )

    assert result == MemoryServerUploadResult(
        task_id="",
        status="failed",
        accepted_count=0,
        code=0,
        errors=[
            {
                "code": "memory_server_upload_failed",
                "message": "memory server upload failed",
                "recoverable": True,
                "detail": "upload timed out with [redacted]",
            }
        ],
    )


def test_remote_memory_client_task_status_posts_user_and_task_with_scope_warning() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "task_id": "task-1",
            "status": "completed",
            "total_files": 2,
            "processed_files": 2,
            "failed_files": 0,
            "estimated_completion_seconds": None,
            "statistics": {"memories_created": 3},
            "results": [{"summary": "done"}],
            "errors": [],
            "code": 200,
        }

    client = RemoteMemoryClient(base_url="http://memory.local", timeout_seconds=1.0, transport=transport)

    result = client.task_status(user_id="u1", task_id="task-1")

    assert result == MemoryServerTaskStatusResult(
        task_id="task-1",
        status="completed",
        total_files=2,
        processed_files=2,
        failed_files=0,
        estimated_completion_seconds=None,
        statistics={"memories_created": 3},
        results=[{"summary": "done"}],
        errors=[],
        code=200,
        scope_warning="memory_server_task_lookup_user_scope_not_enforced",
    )
    assert requests == [
        MemoryServerRequest(
            method="POST",
            path="/v1/tasks_status",
            body={"user_id": "u1", "task_id": "task-1"},
            timeout_seconds=1.0,
        )
    ]


def test_hybrid_memory_store_search_merges_local_and_remote_with_local_first_top_k() -> None:
    local = InMemoryStore()
    local.save(
        MemoryItem(
            memory_id="local-1",
            user_id="u1",
            session_id="s1",
            memory_type="task",
            summary="Local remembered coffee.",
            created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        )
    )
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "results": [
                {
                    "type": "text",
                    "content": "Remote remembered toast.",
                    "score": 0.9,
                    "memory_type": "episodic",
                    "source": {
                        "memory_id": "remote-1",
                        "timestamp_start": "2026-04-11T10:00:00+00:00",
                    },
                },
                {
                    "type": "text",
                    "content": "Remote overflow result.",
                    "score": 0.8,
                    "memory_type": "episodic",
                    "source": {
                        "memory_id": "remote-2",
                        "timestamp_start": "2026-04-11T11:00:00+00:00",
                    },
                },
            ]
        }

    store = HybridMemoryStore(
        local_store=local,
        remote_client=RemoteMemoryClient(base_url="http://memory.local", transport=transport),
    )

    result = store.search(MemoryQuery(user_id="u1", session_id="s1", query="remembered", top_k=2))

    assert [item.memory_id for item in result.items] == ["local-1", "memory_server:remote-1"]
    assert result.total == 2
    assert result.ranking_reason == "hybrid_local_then_memory_server"
    assert "Local remembered coffee." in result.memory_context
    assert "Remote remembered toast." in result.memory_context
    assert "Remote overflow result." not in result.memory_context
    assert requests[0].path == "/v1/memories/query"


def test_hybrid_memory_store_search_degrades_to_local_results_when_remote_fails() -> None:
    local = InMemoryStore()
    local.save(
        MemoryItem(
            memory_id="local-1",
            user_id="u1",
            memory_type="task",
            summary="Local survives remote outage.",
            created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        )
    )

    def transport(request: MemoryServerRequest) -> dict:
        raise TimeoutError("memory server timed out")

    store = HybridMemoryStore(
        local_store=local,
        remote_client=RemoteMemoryClient(base_url="http://memory.local", transport=transport),
    )

    result = store.search(MemoryQuery(user_id="u1", query="local", top_k=5))

    assert [item.memory_id for item in result.items] == ["local-1"]
    assert result.total == 1
    assert result.ranking_reason == "hybrid_local_then_memory_server"
    assert result.errors == [
        {
            "code": "memory_server_query_failed",
            "message": "memory server query failed",
            "recoverable": True,
            "detail": "memory server timed out",
        }
    ]


def test_hybrid_memory_store_lifecycle_operations_delegate_to_local_store_only() -> None:
    remote_calls: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        remote_calls.append(request)
        return {"results": []}

    local = InMemoryStore()
    store = HybridMemoryStore(
        local_store=local,
        remote_client=RemoteMemoryClient(base_url="http://memory.local", transport=transport),
    )
    item = MemoryItem(
        memory_id="local-save",
        user_id="u1",
        session_id="s1",
        memory_type="task",
        summary="Saved locally only.",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    confirmation = MemoryPendingConfirmation(
        confirmation_id="confirmation-1",
        user_id="u1",
        session_id="s1",
        memory_type="task",
        destination="task_checkpoint",
        sensitivity="normal",
        summary="confirm me",
        reason="test",
        content_preview={},
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )

    assert store.save(item) == item
    assert store.get("u1", "local-save") == item
    assert store.list_by_user("u1") == [item]
    assert store.save_confirmation(confirmation) == confirmation
    assert store.get_confirmation("u1", "confirmation-1") == confirmation
    assert store.list_confirmations(user_id="u1") == [confirmation]
    assert store.delete_confirmation("u1", "confirmation-1") is True
    assert store.delete("u1", "local-save") is True
    assert store.get("u1", "local-save") is None
    assert remote_calls == []
