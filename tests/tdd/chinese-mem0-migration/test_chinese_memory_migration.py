from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.chinese_migration import (
    migrate_memories_to_chinese,
)
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.transport import Mem0HttpRequest


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="current-user",
        agent_id="agent.default",
        session_id="migration-session",
    )


def test_inspect_scopes_memories_without_translating_or_updating() -> None:
    requests: list[Mem0HttpRequest] = []
    translation_calls: list[str] = []

    def transport(request: Mem0HttpRequest) -> dict:
        requests.append(request)
        return {
            "results": [
                {"id": "memory-english", "memory": "User likes tea"},
                {"id": "memory-chinese", "memory": "用户喜欢咖啡"},
            ]
        }

    def translate(text: str) -> str:
        translation_calls.append(text)
        return "用户喜欢茶"

    report = migrate_memories_to_chinese(
        identity=_identity(),
        identity_namespace="test",
        transport=transport,
        translate=translate,
        apply=False,
    )

    engine_identity = bind_mem0_identity(_identity(), namespace="test")
    assert requests == [
        Mem0HttpRequest(
            method="GET",
            path="/memories",
            query={
                "user_id": engine_identity.user_id,
                "agent_id": engine_identity.agent_id,
                "limit": "50",
            },
            timeout_seconds=5.0,
        )
    ]
    assert translation_calls == []
    assert report.model_dump() == {
        "total": 2,
        "candidates": 1,
        "already_chinese": 1,
        "updated": 0,
        "updated_memory_ids": [],
        "failed_memory_id": None,
        "error_code": None,
    }


def test_apply_translates_updates_and_verifies_memory_history() -> None:
    requests: list[Mem0HttpRequest] = []
    original = "User paid 48.9 RMB at https://example.com"
    translated = "用户支付了 48.9 元人民币（链接：https://example.com）。"
    current_text = original

    def transport(request: Mem0HttpRequest) -> dict:
        nonlocal current_text
        requests.append(request)
        if request.method == "GET" and request.path == "/memories":
            return {
                "results": [
                    {"id": "memory-english", "memory": current_text},
                    {"id": "memory-chinese", "memory": "用户喜欢咖啡"},
                ]
            }
        if request.method == "PUT":
            assert request.body == {"memory": translated}
            current_text = translated
            return {"id": "memory-english", "memory": current_text}
        if request.path == "/memories/memory-english":
            return {"id": "memory-english", "memory": current_text}
        if request.path == "/memories/memory-english/history":
            return {
                "history": [
                    {
                        "event": "UPDATE",
                        "old_memory": original,
                        "new_memory": translated,
                    }
                ]
            }
        raise AssertionError(request)

    report = migrate_memories_to_chinese(
        identity=_identity(),
        identity_namespace="test",
        transport=transport,
        translate=lambda text: translated if text == original else text,
        apply=True,
    )

    assert report.model_dump() == {
        "total": 2,
        "candidates": 1,
        "already_chinese": 1,
        "updated": 1,
        "updated_memory_ids": ["memory-english"],
        "failed_memory_id": None,
        "error_code": None,
    }
    assert [
        (request.method, request.path)
        for request in requests
    ] == [
        ("GET", "/memories"),
        ("PUT", "/memories/memory-english"),
        ("GET", "/memories/memory-english"),
        ("GET", "/memories/memory-english/history"),
    ]
    assert requests[1].timeout_seconds == 30.0
    assert requests[2].timeout_seconds == 5.0


def test_apply_accepts_english_month_translated_to_numeric_month() -> None:
    original = "The decision was made in July 2026"
    translated = "该决定于2026年7月作出"
    current_text = original

    def transport(request: Mem0HttpRequest) -> dict:
        nonlocal current_text
        if request.method == "GET" and request.path == "/memories":
            return {
                "results": [
                    {"id": "memory-date", "memory": current_text},
                ]
            }
        if request.method == "PUT":
            current_text = translated
            return {"id": "memory-date", "memory": current_text}
        if request.path == "/memories/memory-date":
            return {"id": "memory-date", "memory": current_text}
        if request.path == "/memories/memory-date/history":
            return {
                "history": [
                    {
                        "event": "UPDATE",
                        "old_memory": original,
                        "new_memory": translated,
                    }
                ]
            }
        raise AssertionError(request)

    report = migrate_memories_to_chinese(
        identity=_identity(),
        identity_namespace="test",
        transport=transport,
        translate=lambda _: translated,
        apply=True,
    )

    assert report.updated == 1
    assert report.error_code is None


def test_apply_accepts_thousands_translated_to_chinese_ten_thousands() -> None:
    original = "June payrolls added 57,000 jobs"
    translated = "6月新增就业岗位5.7万个"
    current_text = original

    def transport(request: Mem0HttpRequest) -> dict:
        nonlocal current_text
        if request.method == "GET" and request.path == "/memories":
            return {
                "results": [
                    {"id": "memory-number", "memory": current_text},
                ]
            }
        if request.method == "PUT":
            current_text = translated
            return {"id": "memory-number", "memory": current_text}
        if request.path == "/memories/memory-number":
            return {"id": "memory-number", "memory": current_text}
        if request.path == "/memories/memory-number/history":
            return {
                "history": [
                    {
                        "event": "UPDATE",
                        "old_memory": original,
                        "new_memory": translated,
                    }
                ]
            }
        raise AssertionError(request)

    report = migrate_memories_to_chinese(
        identity=_identity(),
        identity_namespace="test",
        transport=transport,
        translate=lambda _: translated,
        apply=True,
    )

    assert report.updated == 1
    assert report.error_code is None
