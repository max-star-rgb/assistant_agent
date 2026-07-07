import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def _load_smoke_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
