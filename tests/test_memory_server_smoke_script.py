import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from assistant_agent.memory.remote import MemoryServerTaskStatusResult, MemoryServerUploadResult
from assistant_agent.schemas.memory import MemoryItem, MemorySearchResult


SCRIPT_PATH = Path("scripts/smoke_memory_server.py")


def test_memory_server_smoke_import_is_safe(monkeypatch) -> None:
    module_name = "smoke_memory_server_import_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("smoke script import must not call Memory Server")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "main")


def test_memory_server_smoke_missing_base_url_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--user-id", "u1", "--query", "早餐"],
        env={},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "provider_unconfigured",
        "capability": "memory_server",
        "error": "missing MEMORY_SERVER_BASE_URL",
    }
    assert "Traceback" not in result.stderr


def test_memory_server_smoke_uses_health_and_query_without_direct_answer(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_success_test")
    requests = []

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            requests.append(
                {
                    "base_url": base_url,
                    "timeout_seconds": timeout_seconds,
                    "query_strategy": query_strategy,
                    "direct_answer": direct_answer,
                    "include_media_chunks": include_media_chunks,
                    "trace": trace,
                }
            )

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            requests.append({"health": {"user_id": user_id, "session_id": session_id}})
            return {"status": "ok", "version": "0.1.0", "code": 200}

        def query_memories(self, query):
            requests.append({"query": query.model_dump(mode="json")})
            return MemorySearchResult(
                items=[
                    MemoryItem(
                        memory_id="memory_server:remote-1",
                        user_id=query.user_id,
                        session_id=query.session_id,
                        memory_type="task",
                        summary="Remote smoke memory.",
                        created_at=datetime(2026, 4, 11, tzinfo=timezone.utc),
                    )
                ],
                query_used=query,
                total=1,
                ranking_reason="memory_server_remote_query",
                memory_context="Remote smoke memory.",
            )

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--session-id",
            "s1",
            "--query",
            "早餐",
            "--top-k",
            "2",
            "--timeout-seconds",
            "1.5",
            "--strategy",
            "hybrid",
            "--trace",
        ],
        env={},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "success",
        "capability": "memory_server",
        "base_url": "http://memory.local",
        "health_status": "ok",
        "health_version": "0.1.0",
        "query": "早餐",
        "user_id": "u1",
        "session_id": "s1",
        "strategy": "hybrid",
        "direct_answer": False,
        "include_media_chunks": False,
        "result_count": 1,
        "memory_ids": ["memory_server:remote-1"],
        "summaries": ["Remote smoke memory."],
        "errors": [],
    }
    assert requests[0] == {
        "base_url": "http://memory.local",
        "timeout_seconds": 1.5,
        "query_strategy": "hybrid",
        "direct_answer": False,
        "include_media_chunks": False,
        "trace": True,
    }
    assert requests[1] == {"health": {"user_id": "u1", "session_id": "s1"}}
    assert requests[2]["query"]["user_id"] == "u1"
    assert requests[2]["query"]["session_id"] == "s1"
    assert requests[2]["query"]["top_k"] == 2


def test_memory_server_smoke_health_only_does_not_require_query_or_call_query(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_health_only_test")
    calls = []

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            calls.append({"init": {"base_url": base_url, "timeout_seconds": timeout_seconds}})

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            calls.append({"health": {"user_id": user_id, "session_id": session_id}})
            return {"status": "ok", "version": "0.1.0", "code": 200}

        def query_memories(self, query):
            raise AssertionError("health-only smoke must not call query")

        def upload_media(self, *args, **kwargs):
            raise AssertionError("health-only smoke must not upload media")

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--session-id",
            "s1",
            "--health-only",
            "--timeout-seconds",
            "0.5",
        ],
        env={},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "success",
        "capability": "memory_server",
        "base_url": "http://memory.local",
        "health_status": "ok",
        "health_version": "0.1.0",
        "user_id": "u1",
        "session_id": "s1",
        "errors": [],
    }
    assert calls == [
        {"init": {"base_url": "http://memory.local", "timeout_seconds": 0.5}},
        {"health": {"user_id": "u1", "session_id": "s1"}},
    ]


def test_memory_server_smoke_requires_query_unless_health_only(capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_query_required_test")

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
        ],
        env={},
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid_request",
        "capability": "memory_server",
        "error": "memory server smoke requires --query unless --health-only is set",
    }


def test_memory_server_smoke_reports_health_failure_without_traceback(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_health_failure_test")

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            pass

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            raise TimeoutError("health timed out with token=sk-secret")

        def query_memories(self, query):
            raise AssertionError("query must not run after health failure")

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--query",
            "早餐",
        ],
        env={},
    )

    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["capability"] == "memory_server"
    assert payload["base_url"] == "http://memory.local"
    assert payload["health_status"] == "failed"
    assert payload["errors"][0]["code"] == "memory_server_health_failed"
    assert payload["errors"][0]["message"] == "memory server health failed"
    assert payload["errors"][0]["recoverable"] is True
    assert "health timed out" in payload["errors"][0]["detail"]
    assert "[redacted]" in payload["errors"][0]["detail"]
    assert payload["diagnosis"] == {
        "code": "memory_server_health_timeout",
        "message": "Memory Server health check timed out.",
        "next_step": "Check service startup, Docker/network routing, and the health endpoint latency.",
    }
    assert "Traceback" not in captured.err
    assert "sk-secret" not in captured.out
    assert "sk-secret" not in captured.err


def test_memory_server_smoke_diagnoses_connection_refused_health_failure(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_connection_refused_test")

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            pass

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            raise RuntimeError("memory server request failed: [Errno 111] Connection refused")

        def query_memories(self, query):
            raise AssertionError("query must not run after health failure")

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)

    code = module.main(
        [
            "--base-url",
            "http://127.0.0.1:5200",
            "--user-id",
            "u1",
            "--health-only",
        ],
        env={},
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnosis"] == {
        "code": "memory_server_not_listening",
        "message": "Memory Server is not listening at the configured base URL.",
        "next_step": "Start the external Memory Server and rerun the health-only smoke check.",
    }


def test_memory_server_smoke_does_not_upload_media_without_explicit_media_args(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_no_media_test")

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            pass

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            return {"status": "ok", "version": "0.1.0", "code": 200}

        def query_memories(self, query):
            return MemorySearchResult(
                items=[],
                query_used=query,
                total=0,
                ranking_reason="memory_server_remote_query",
                memory_context="",
            )

        def upload_media(self, *args, **kwargs):
            raise AssertionError("media upload must be explicit in smoke script")

        def task_status(self, *args, **kwargs):
            raise AssertionError("task status must not run without media upload")

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--query",
            "早餐",
        ],
        env={},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "media_upload" not in payload
    assert "media_task_status" not in payload


def test_memory_server_smoke_can_upload_media_and_poll_task_status(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_media_test")
    calls = []

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            calls.append({"init": {"base_url": base_url, "timeout_seconds": timeout_seconds}})

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            calls.append({"health": {"user_id": user_id, "session_id": session_id}})
            return {"status": "ok", "version": "0.1.0", "code": 200}

        def query_memories(self, query):
            calls.append({"query": query.model_dump(mode="json")})
            return MemorySearchResult(
                items=[],
                query_used=query,
                total=0,
                ranking_reason="memory_server_remote_query",
                memory_context="",
            )

        def upload_media(self, *, user_id, session_id, files):
            calls.append(
                {
                    "upload": {
                        "user_id": user_id,
                        "session_id": session_id,
                        "files": [file.model_dump(mode="json") for file in files],
                    }
                }
            )
            return MemoryServerUploadResult(
                task_id="20260411T120000Z-a1b2c3",
                status="processing",
                accepted_count=1,
                code=202,
            )

        def task_status(self, *, user_id, task_id):
            calls.append({"task_status": {"user_id": user_id, "task_id": task_id}})
            return MemoryServerTaskStatusResult(
                task_id=task_id,
                status="completed",
                total_files=1,
                processed_files=1,
                failed_files=0,
                statistics={"memories_created": 2},
                results=[{"summary": "done"}],
                code=200,
                scope_warning="memory_server_task_lookup_user_scope_not_enforced",
            )

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)
    monkeypatch.setattr(module, "_generated_media_file_id", lambda *, user_id, session_id, filename: "smoke-file-1")

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--session-id",
            "s1",
            "--query",
            "早餐",
            "--media-file-url",
            "file:///tmp/breakfast.mp4",
            "--media-filename",
            "breakfast.mp4",
            "--media-type",
            "video",
            "--media-start-time",
            "2026-04-11T12:00:00Z",
        ],
        env={},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["media_upload"] == {
        "status": "processing",
        "task_id": "20260411T120000Z-a1b2c3",
        "accepted_count": 1,
        "file_id": "smoke-file-1",
        "errors": [],
    }
    assert payload["media_task_status"] == {
        "task_id": "20260411T120000Z-a1b2c3",
        "status": "completed",
        "total_files": 1,
        "processed_files": 1,
        "failed_files": 0,
        "scope_warning": "memory_server_task_lookup_user_scope_not_enforced",
        "errors": [],
    }
    assert calls[3] == {
        "upload": {
            "user_id": "u1",
            "session_id": "s1",
            "files": [
                {
                    "file_id": "smoke-file-1",
                    "file_url": "file:///tmp/breakfast.mp4",
                    "filename": "breakfast.mp4",
                    "media_type": "video",
                    "start_time": "2026-04-11T12:00:00Z",
                    "metadata": {},
                }
            ],
        }
    }
    assert calls[4] == {"task_status": {"user_id": "u1", "task_id": "20260411T120000Z-a1b2c3"}}


def test_memory_server_smoke_waits_until_media_task_terminal_status(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_media_wait_test")
    fake_time = _FakeTime()
    calls = []
    statuses = ["processing", "completed"]

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            pass

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            return {"status": "ok", "version": "0.1.0", "code": 200}

        def query_memories(self, query):
            return MemorySearchResult(
                items=[],
                query_used=query,
                total=0,
                ranking_reason="memory_server_remote_query",
                memory_context="",
            )

        def upload_media(self, *, user_id, session_id, files):
            return MemoryServerUploadResult(
                task_id="task-1",
                status="processing",
                accepted_count=1,
                code=202,
            )

        def task_status(self, *, user_id, task_id):
            status = statuses.pop(0)
            calls.append({"task_status": {"user_id": user_id, "task_id": task_id, "status": status}})
            return MemoryServerTaskStatusResult(
                task_id=task_id,
                status=status,
                total_files=1,
                processed_files=1 if status == "completed" else 0,
                failed_files=0,
                code=200,
                scope_warning="memory_server_task_lookup_user_scope_not_enforced",
            )

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)
    monkeypatch.setattr(module, "_generated_media_file_id", lambda *, user_id, session_id, filename: "smoke-file-1")
    monkeypatch.setattr(module, "time", fake_time, raising=False)

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--query",
            "早餐",
            "--media-file-url",
            "file:///tmp/breakfast.mp4",
            "--media-filename",
            "breakfast.mp4",
            "--media-type",
            "video",
            "--media-start-time",
            "2026-04-11T12:00:00Z",
            "--wait",
            "--wait-timeout-seconds",
            "5",
            "--poll-interval-seconds",
            "0.25",
        ],
        env={},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["media_task_status"]["status"] == "completed"
    assert payload["media_task_wait"] == {
        "enabled": True,
        "poll_attempts": 2,
        "terminal": True,
        "timed_out": False,
        "timeout_seconds": 5.0,
        "poll_interval_seconds": 0.25,
    }
    assert calls == [
        {"task_status": {"user_id": "u1", "task_id": "task-1", "status": "processing"}},
        {"task_status": {"user_id": "u1", "task_id": "task-1", "status": "completed"}},
    ]
    assert fake_time.sleeps == [0.25]


def test_memory_server_smoke_wait_times_out_on_nonterminal_media_task(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_media_wait_timeout_test")
    fake_time = _FakeTime()
    calls = []

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            direct_answer: bool,
            include_media_chunks: bool,
            trace: bool,
        ) -> None:
            pass

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            return {"status": "ok", "version": "0.1.0", "code": 200}

        def query_memories(self, query):
            return MemorySearchResult(
                items=[],
                query_used=query,
                total=0,
                ranking_reason="memory_server_remote_query",
                memory_context="",
            )

        def upload_media(self, *, user_id, session_id, files):
            return MemoryServerUploadResult(
                task_id="task-1",
                status="processing",
                accepted_count=1,
                code=202,
            )

        def task_status(self, *, user_id, task_id):
            calls.append({"task_status": {"user_id": user_id, "task_id": task_id}})
            return MemoryServerTaskStatusResult(
                task_id=task_id,
                status="processing",
                total_files=1,
                processed_files=0,
                failed_files=0,
                code=200,
                scope_warning="memory_server_task_lookup_user_scope_not_enforced",
            )

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeClient)
    monkeypatch.setattr(module, "_generated_media_file_id", lambda *, user_id, session_id, filename: "smoke-file-1")
    monkeypatch.setattr(module, "time", fake_time, raising=False)

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--query",
            "早餐",
            "--media-file-url",
            "file:///tmp/breakfast.mp4",
            "--media-filename",
            "breakfast.mp4",
            "--media-type",
            "video",
            "--media-start-time",
            "2026-04-11T12:00:00Z",
            "--wait",
            "--wait-timeout-seconds",
            "0.2",
            "--poll-interval-seconds",
            "0.1",
        ],
        env={},
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["media_task_status"]["status"] == "processing"
    assert payload["media_task_wait"] == {
        "enabled": True,
        "poll_attempts": 3,
        "terminal": False,
        "timed_out": True,
        "timeout_seconds": 0.2,
        "poll_interval_seconds": 0.1,
    }
    assert calls == [
        {"task_status": {"user_id": "u1", "task_id": "task-1"}},
        {"task_status": {"user_id": "u1", "task_id": "task-1"}},
        {"task_status": {"user_id": "u1", "task_id": "task-1"}},
    ]
    assert fake_time.sleeps == [0.1, 0.1]


def test_memory_server_smoke_rejects_incomplete_media_args(capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_incomplete_media_test")

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--query",
            "早餐",
            "--media-file-url",
            "file:///tmp/breakfast.mp4",
        ],
        env={},
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid_request",
        "capability": "memory_server",
        "error": "media smoke requires --media-filename, --media-type, and --media-start-time",
    }


def test_memory_server_smoke_rejects_invalid_media_start_time(capsys) -> None:
    module = _load_smoke_module("smoke_memory_server_invalid_media_time_test")

    code = module.main(
        [
            "--base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--query",
            "早餐",
            "--media-file-url",
            "file:///tmp/breakfast.mp4",
            "--media-filename",
            "breakfast.mp4",
            "--media-type",
            "video",
            "--media-start-time",
            "not-a-time",
        ],
        env={},
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid_request",
        "capability": "memory_server",
        "error": "media smoke requires ISO --media-start-time",
    }


def _load_smoke_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
