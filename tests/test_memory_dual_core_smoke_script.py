import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from assistant_agent.schemas.memory import MemoryItem, MemorySearchResult


SCRIPT_PATH = Path("scripts/smoke_memory_dual_core.py")


def test_memory_dual_core_smoke_import_is_safe(monkeypatch) -> None:
    module_name = "smoke_memory_dual_core_import_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("dual-core smoke import must not call Memory Server")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "main")


def test_memory_dual_core_smoke_offline_only_runs_local_acceptance(capsys) -> None:
    module = _load_smoke_module("smoke_memory_dual_core_offline_test")

    code = module.main(["--offline-only"], env={})

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"
    assert payload["capability"] == "memory_dual_core"
    assert payload["offline_only"] is True
    assert payload["external_memory_server"] == "skipped"
    assert set(payload["checks"]) == {
        "local_sqlite_core",
        "dual_core_degradation",
        "remote_service_lifecycle_failure",
        "memory_quality_eval",
    }
    assert all(check["passed"] is True for check in payload["checks"].values())
    assert payload["checks"]["local_sqlite_core"]["store"] == "SQLiteMemoryStore"
    assert payload["checks"]["dual_core_degradation"]["event_type"] == "memory_remote_degraded"
    assert (
        payload["checks"]["remote_service_lifecycle_failure"]["event_type"]
        == "memory_remote_lifecycle_failed"
    )
    rendered = json.dumps(payload, ensure_ascii=False).lower()
    assert "memory.local" not in rendered
    assert "token" not in rendered
    assert "secret" not in rendered
    assert "traceback" not in rendered


def test_memory_dual_core_smoke_remote_check_is_explicit_and_prompt_safe(monkeypatch, capsys) -> None:
    module = _load_smoke_module("smoke_memory_dual_core_remote_test")
    calls = []

    class FakeRemoteMemoryClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout_seconds: float,
            query_strategy: str,
            include_media_chunks: bool,
            direct_answer: bool,
        ) -> None:
            calls.append(
                {
                    "base_url": base_url,
                    "timeout_seconds": timeout_seconds,
                    "query_strategy": query_strategy,
                    "include_media_chunks": include_media_chunks,
                    "direct_answer": direct_answer,
                }
            )

        def health(self, *, user_id: str | None = None, session_id: str | None = None) -> dict:
            calls.append({"health": {"user_id": user_id, "session_id": session_id}})
            return {"status": "ok", "version": "0.1.0"}

        def query_memories(self, query):
            calls.append({"query": query.model_dump(mode="json")})
            return MemorySearchResult(
                items=[
                    MemoryItem(
                        memory_id="memory_server:remote-1",
                        user_id=query.user_id,
                        session_id=query.session_id,
                        memory_type="task",
                        summary="Remote check memory.",
                        created_at=datetime(2026, 4, 11, tzinfo=timezone.utc),
                    )
                ],
                query_used=query,
                total=1,
                ranking_reason="memory_server_remote_query",
                memory_context="Remote check memory.",
            )

    monkeypatch.setattr(module, "RemoteMemoryClient", FakeRemoteMemoryClient)

    code = module.main(
        [
            "--memory-server-base-url",
            "http://memory.local",
            "--user-id",
            "u1",
            "--session-id",
            "s1",
            "--query",
            "上次早餐",
            "--timeout-seconds",
            "1.5",
            "--strategy",
            "hybrid",
        ],
        env={},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"
    assert payload["offline_only"] is False
    assert payload["external_memory_server"] == "checked"
    assert payload["checks"]["external_memory_server"] == {
        "passed": True,
        "health_status": "ok",
        "health_version": "0.1.0",
        "result_count": 1,
        "error_codes": [],
    }
    assert calls[0] == {
        "base_url": "http://memory.local",
        "timeout_seconds": 1.5,
        "query_strategy": "hybrid",
        "include_media_chunks": False,
        "direct_answer": False,
    }
    assert calls[1] == {"health": {"user_id": "u1", "session_id": "s1"}}
    assert calls[2]["query"]["user_id"] == "u1"
    rendered = json.dumps(payload, ensure_ascii=False).lower()
    assert "memory.local" not in rendered
    assert "上次早餐" not in rendered


def _load_smoke_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
