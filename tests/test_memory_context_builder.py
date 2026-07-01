from datetime import datetime, timezone

from assistant_agent.agent.state import AgentState
from assistant_agent.memory.context_builder import MemoryContextBuilder
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.retrieval import format_memory_context
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest


def test_memory_context_builder_respects_max_chars_and_includes_refs() -> None:
    items = [
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="artifact",
            summary="最近生成过一张白色运动鞋海报。",
            artifact_refs=["mock://image/poster-1"],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        MemoryItem(
            memory_id="m2",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢日系极简浅色背景。" * 10,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]

    context = format_memory_context(items, max_chars=90)

    assert len(context) <= 90
    assert context.startswith("相关历史：")
    assert "mock://image/poster-1" in context


def test_token_aware_memory_context_builder_omits_items_over_budget() -> None:
    builder = MemoryContextBuilder()
    items = [
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢浅色。",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        MemoryItem(
            memory_id="m2",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢非常详细的日系极简浅色背景。" * 20,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    first_item_budget = builder.estimate_tokens("相关历史：\n偏好/事实记忆：\n- [preference] 用户喜欢浅色。")

    context = builder.build(items, budget_tokens=first_item_budget, max_chars=1000)

    assert [item.memory_id for item in context.items] == ["m1"]
    assert context.total_tokens <= first_item_budget
    assert context.budget_tokens == first_item_budget
    assert context.omitted_count == 1
    assert context.rejected_reasons == ["m2:memory_context_token_budget_exceeded"]


def test_token_aware_memory_context_builder_rejects_sensitive_and_expired_items() -> None:
    context = MemoryContextBuilder().build(
        [
            MemoryItem(
                memory_id="safe",
                user_id="u1",
                memory_type="task",
                summary="安全任务记忆。",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            MemoryItem(
                memory_id="secret",
                user_id="u1",
                memory_type="task",
                summary="敏感任务记忆。",
                sensitivity="sensitive",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            MemoryItem(
                memory_id="expired",
                user_id="u1",
                memory_type="task",
                summary="过期任务记忆。",
                expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
        ],
        budget_tokens=100,
        max_chars=1000,
    )

    assert [item.memory_id for item in context.items] == ["safe"]
    assert context.omitted_count == 2
    assert context.rejected_reasons == [
        "secret:sensitive_memory_not_injected",
        "expired:expired_memory_not_injected",
    ]


def test_memory_manager_records_token_context_metadata() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢浅色。",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save(
        MemoryItem(
            memory_id="m2",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢非常详细的日系极简浅色背景。" * 20,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    manager = MemoryManager(store)
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="浅色",
        metadata={"memory_context_max_tokens": 28},
    )
    state = AgentState.from_request(request)

    context = manager.load_into_state(state, request, max_context_chars=1000)

    assert [item.memory_id for item in context.items] == ["m1"]
    assert state.request.metadata["memory_context_tokens"] <= 28
    assert state.request.metadata["memory_context_budget_tokens"] == 28
    assert state.request.metadata["memory_context_omitted_count"] == 1
    assert state.request.metadata["memory_context_injected_ids"] == ["m1"]
    assert state.request.metadata["memory_context_retrieval_version"] == "memory_context_builder_v1"
