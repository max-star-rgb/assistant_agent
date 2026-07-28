"""Integration coverage for token-triggered rolling context compaction."""

import json

from pydantic import BaseModel, Field

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.assistant_run_service import (
    ConversationTurn,
    InMemoryConversationStore,
    JsonlConversationStore,
    run_assistant_request,
)
from assistant_agent.runtime.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
)
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.context.compaction import (
    project_observations_for_context,
    sanitize_observations_for_context,
)
from assistant_agent.context.compactor import LLMCompactor
from assistant_agent.context.models import ContextSummary
from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry


class _CapturingChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="当前轮回答",
        )


_SUMMARY_TEXT = """## 当前目标
继续处理当前会话。
## 用户约束与偏好
保留用户明确约束。
## 已确认事实
历史事实哨兵。
## 已执行操作与结果
无。
## 已作出的决定
沿用已确认方案。
## 未解决事项
无。
## 最近交互状态
上一轮已经完成。"""


class _ThresholdTokenCounter:
    tokenizer_id = "qwen-test-tokenizer"

    def __init__(self, *, raw_history_tokens: int) -> None:
        self.raw_history_tokens = raw_history_tokens

    def count_text(self, value: str) -> int:
        return len(value)

    def count_chat_request(self, request: ChatRequest) -> int:
        history_messages = [
            message
            for message in request.messages[1:-1]
            if message.get("role") in {"user", "assistant"}
        ]
        return self.raw_history_tokens if history_messages else 20


class _RollingChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, *, compaction_fails: bool = False) -> None:
        self.compaction_fails = compaction_fails
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if request.user_query == "压缩已完成的会话历史":
            if self.compaction_fails:
                return ChatResult(
                    provider=self.provider,
                    model=self.model,
                    usage={
                        "prompt_tokens": 80,
                        "completion_tokens": 0,
                        "total_tokens": 80,
                    },
                    errors=[
                        ChatProviderError(
                            code="summary_failed",
                            message="summary failed",
                        )
                    ],
                )
            return ChatResult(
                provider=self.provider,
                model=self.model,
                finish_reason="stop",
                response_text=_SUMMARY_TEXT,
                usage={
                    "prompt_tokens": 120,
                    "completion_tokens": 40,
                    "total_tokens": 160,
                },
            )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="当前轮回答",
            usage={
                "prompt_tokens": 22,
                "completion_tokens": 3,
                "total_tokens": 25,
            },
        )


class _ProbeInput(BaseModel):
    value: str = Field(min_length=1)


class _ProbeTool(ToolBase):
    name = "context_probe"
    description = "Return one context test value."
    input_schema = _ProbeInput
    output_schema = _ProbeInput
    category = "read"

    def _run(self, input: _ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
        )


class _ToolTurnTokenCounter(_ThresholdTokenCounter):
    def count_chat_request(self, request: ChatRequest) -> int:
        if any(message.get("role") == "tool" for message in request.messages):
            return 7_000
        return 6_000


class _ToolTurnChatAdapter(_RollingChatAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.main_turn = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        if request.user_query == "压缩已完成的会话历史":
            return super().chat(request)
        self.requests.append(request)
        self.main_turn += 1
        if self.main_turn == 1:
            return ChatResult(
                provider=self.provider,
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-context-probe",
                        name="context_probe",
                        arguments={"value": "observation-sentinel"},
                    )
                ],
            )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="工具结果已处理",
        )


def test_context_window_policy_uses_configured_target_trigger_and_hard_ratios() -> None:
    policy = ContextWindowPolicy(
        input_token_limit=10_000,
        target_ratio=0.40,
        trigger_ratio=0.70,
        hard_ratio=0.85,
    )

    below_trigger = policy.evaluate(6_999)
    at_trigger = policy.evaluate(7_000)
    at_hard = policy.evaluate(8_500)

    assert below_trigger.triggered is False
    assert at_trigger.triggered is True
    assert at_trigger.hard is False
    assert at_trigger.target_tokens == 4_000
    assert at_hard.hard is True


def test_default_runtime_sends_all_stored_history_without_summary_or_trimming() -> None:
    adapter = _CapturingChatAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()
    for index in range(1, 13):
        conversation_store.append(
            "context-user",
            "context-session",
            ConversationTurn(
                user_text=f"历史问题 {index}",
                assistant_text=f"历史回答 {index}",
                run_id=f"run-{index}",
                trace_id=f"trace-{index}",
            ),
        )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="context-user",
            session_id="context-session",
            text="当前问题",
            metadata={
                "context_budget_max_chars": 500,
                "conversation_recent_max_tokens": 1,
            },
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert conversation_store.get_summary("context-user", "context-session") is None
    assert "context_summary" not in artifacts.state.request.metadata
    messages = adapter.requests[0].messages
    assert len(conversation_store.get("context-user", "context-session")) == 13
    assert [message["role"] for message in messages] == [
        "system",
        *[role for _ in range(12) for role in ("user", "assistant")],
        "user",
    ]
    assert messages[1]["content"] == "历史问题 1"
    assert messages[-2]["content"] == "历史回答 12"
    assert messages[-1]["content"].endswith("当前问题")
    assert "较早对话摘要" not in str(messages)


def test_unbounded_observation_context_only_removes_unsafe_payloads() -> None:
    long_text = "完整内容" * 500
    observations = [
        {
            "tool_name": "example",
            "summary": long_text,
            "data": {
                "items": [{"index": index, "description": long_text} for index in range(6)],
                "api_key": "secret-value",
                "raw_provider_response": {"hidden": True},
            },
        }
    ]

    sanitized = sanitize_observations_for_context(observations)[0]

    assert sanitized["summary"] == long_text
    assert len(sanitized["data"]["items"]) == 6
    assert sanitized["data"]["items"][-1]["description"] == long_text
    assert "api_key" not in sanitized["data"]
    assert "raw_provider_response" not in sanitized["data"]


def test_prompt_observation_projection_compacts_domain_data() -> None:
    observation = {
        "tool_name": "example",
        "status": "succeeded",
        "summary": "duplicate summary",
        "is_complete": True,
        "output_ref": "artifact://one",
        "data": {
            "items": [{"id": index} for index in range(5)],
        },
    }

    projected = project_observations_for_context([observation])[0]

    assert {
        key: value
        for key, value in projected.items()
        if key not in {"compacted", "compaction"}
    } == {
        "tool_name": "example",
        "status": "succeeded",
        "summary": "duplicate summary",
        "is_complete": True,
        "data": {
            "items": [{"id": 0}, {"id": 1}, {"id": 2}],
        },
        "output_ref": "artifact://one",
    }
    assert projected["compacted"] is True
    assert projected["compaction"]["original_chars"] > projected["compaction"]["compacted_chars"]
    assert project_observations_for_context([projected]) == [projected]


def test_prompt_observation_projection_uses_one_error_contract() -> None:
    observation = {
        "tool_name": "example",
        "status": "failed",
        "summary": "duplicated failure",
        "is_complete": False,
        "data": {},
        "error": {
            "code": "provider_timeout",
            "message": "timed out",
            "retryable": True,
        },
    }

    projected = project_observations_for_context([observation])[0]

    assert {
        key: value
        for key, value in projected.items()
        if key not in {"compacted", "compaction"}
    } == {
        "tool_name": "example",
        "status": "failed",
        "summary": "duplicated failure",
        "is_complete": False,
        "error": {
            "code": "provider_timeout",
            "message": "timed out",
            "retryable": True,
        },
    }


def test_runtime_replaces_all_covered_turns_with_natural_language_summary() -> None:
    adapter = _RollingChatAdapter()
    token_counter = _ThresholdTokenCounter(raw_history_tokens=7_000)
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            context_input_token_limit=10_000,
            context_compaction_safety_margin_tokens=0,
            context_summary_max_tokens=512,
        ),
        chat_adapter=adapter,
        context_compactor=LLMCompactor(
            adapter,
            token_counter=token_counter,
        ),
        context_token_counter=token_counter,
        session_store=InMemorySessionStore(),
        trace_store=trace_store,
    )
    conversation_store = InMemoryConversationStore()
    for index in range(1, 4):
        conversation_store.append(
            "opt-in-user",
            "opt-in-session",
            ConversationTurn(
                user_text=f"历史问题 {index}",
                assistant_text=f"历史回答 {index}",
                run_id=f"run-{index}",
                trace_id=f"trace-{index}",
            ),
        )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="opt-in-user",
            session_id="opt-in-session",
            text="当前问题",
            metadata={"conversation_recent_max_tokens": 1},
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    summary = conversation_store.get_summary("opt-in-user", "opt-in-session")
    assert summary is not None
    assert summary.summary_text == _SUMMARY_TEXT
    assert summary.covered_turn_count == 3
    assert summary.source_turn_count == 3
    remaining = conversation_store.get("opt-in-user", "opt-in-session")
    assert [(turn.user_text, turn.assistant_text) for turn in remaining] == [
        ("当前问题", "当前轮回答")
    ]
    assert len(adapter.requests) == 2
    assert "历史问题 1" in adapter.requests[0].messages[1]["content"]
    messages = adapter.requests[1].messages
    assert [message["role"] for message in messages] == ["system", "user"]
    assert _SUMMARY_TEXT in messages[-1]["content"]
    assert messages[-1]["content"].count("<session_summary") == 1
    assert "历史问题 1" not in str(messages)
    assert artifacts.state.request.metadata["context_compaction_applied"] is True
    assert artifacts.state.request.metadata["context_compaction_target_reached"] is True
    preflight = artifacts.state.request.metadata["context_token_preflight"]
    assert preflight["input_tokens"] == 20
    assert preflight["provider_prompt_tokens"] == 22
    assert preflight["estimation_error_tokens"] == 2
    assert artifacts.state.request.metadata[
        "context_token_preflight_before_compaction"
    ]["input_tokens"] == 7_000
    assert artifacts.state.request.metadata[
        "context_compaction_provider_usage_history"
    ] == [
        {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
        }
    ]
    context_events = [
        event
        for event in trace_store.list_by_run(artifacts.state.run_id)
        if event.canonical_event == "context.build.finished"
    ]
    assert [
        event.attributes["build_reason"]
        for event in context_events
    ] == ["iteration_initial", "post_compaction"]
    assert all(
        event.observation_name == "context.compile"
        for event in context_events
    )
    final_report = context_events[-1].output_summary["context_report_v2"]
    assert final_report["token_accounting_status"] == "available"
    assert final_report["compiled_input_tokens"] == preflight["input_tokens"]
    assert final_report["effective_input_limit"] == preflight[
        "effective_input_limit"
    ]
    assert final_report["compression_stage"] == "compacted"
    assert final_report["sections"]["session_summary"]["compaction"] == (
        "rolling_summary"
    )
    assert "realtime_task_state" not in final_report["sections"]


def test_subsequent_compaction_merges_old_summary_without_restoring_raw_turns() -> None:
    adapter = _RollingChatAdapter()
    token_counter = _ThresholdTokenCounter(raw_history_tokens=7_000)
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            context_input_token_limit=10_000,
            context_compaction_safety_margin_tokens=0,
        ),
        chat_adapter=adapter,
        context_compactor=LLMCompactor(adapter, token_counter=token_counter),
        context_token_counter=token_counter,
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()

    for text in ("第一轮", "第二轮", "第三轮"):
        run_assistant_request(
            UserRequest(
                user_id="rolling-user",
                session_id="rolling-session",
                text=text,
            ),
            runtime=runtime,
            conversation_store=conversation_store,
        )

    summary_requests = [
        request
        for request in adapter.requests
        if request.user_query == "压缩已完成的会话历史"
    ]
    assert len(summary_requests) == 2
    source = json.loads(summary_requests[-1].messages[-1]["content"].split("\n", 1)[1])
    assert _SUMMARY_TEXT in source["existing_summary"]
    assert [turn["user"] for turn in source["completed_turns"]] == ["第二轮"]

    summary = conversation_store.get_summary("rolling-user", "rolling-session")
    assert summary is not None
    assert summary.summary_revision == 2
    assert summary.covered_turn_count == 1
    assert summary.source_turn_count == 2
    assert [turn.user_text for turn in conversation_store.get(
        "rolling-user",
        "rolling-session",
    )] == ["第三轮"]


def test_jsonl_store_replaces_only_the_covered_session_prefix(tmp_path) -> None:
    path = tmp_path / "conversation.jsonl"
    store = JsonlConversationStore(path)
    for index in range(3):
        store.append(
            "jsonl-user",
            "target-session",
            ConversationTurn(
                user_text=f"目标问题 {index}",
                assistant_text=f"目标回答 {index}",
                run_id=f"target-run-{index}",
                trace_id=f"target-trace-{index}",
            ),
        )
    store.append(
        "jsonl-user",
        "other-session",
        ConversationTurn("其他问题", "其他回答", "other-run", "other-trace"),
    )
    summary = ContextSummary(
        schema_version="rolling_context_summary_v1",
        summary_text=_SUMMARY_TEXT,
        summary_revision=1,
        covered_turn_count=2,
        source_turn_count=2,
    )

    store.replace_history_prefix_with_summary(
        "jsonl-user",
        "target-session",
        summary,
        covered_turn_count=2,
    )

    reloaded = JsonlConversationStore(path)
    assert [turn.user_text for turn in reloaded.get(
        "jsonl-user",
        "target-session",
    )] == ["目标问题 2"]
    assert [turn.user_text for turn in reloaded.get(
        "jsonl-user",
        "other-session",
    )] == ["其他问题"]
    assert reloaded.get_summary(
        "jsonl-user",
        "target-session",
    ) == summary


def test_soft_threshold_compaction_failure_keeps_raw_history() -> None:
    adapter = _RollingChatAdapter(compaction_fails=True)
    token_counter = _ThresholdTokenCounter(raw_history_tokens=7_000)
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            context_input_token_limit=10_000,
            context_compaction_safety_margin_tokens=0,
        ),
        chat_adapter=adapter,
        context_compactor=LLMCompactor(adapter, token_counter=token_counter),
        context_token_counter=token_counter,
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "soft-user",
        "soft-session",
        ConversationTurn("旧问题", "旧回答", "run-old", "trace-old"),
    )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="soft-user",
            session_id="soft-session",
            text="当前问题",
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert artifacts.state.status == "completed"
    assert len(adapter.requests) == 2
    assert adapter.requests[-1].user_query != "压缩已完成的会话历史"
    assert conversation_store.get_summary("soft-user", "soft-session") is None
    assert [turn.user_text for turn in conversation_store.get("soft-user", "soft-session")] == [
        "旧问题",
        "当前问题",
    ]
    assert artifacts.state.request.metadata["context_compaction_failed"] is True


def test_hard_threshold_compaction_failure_blocks_main_provider_call() -> None:
    adapter = _RollingChatAdapter(compaction_fails=True)
    token_counter = _ThresholdTokenCounter(raw_history_tokens=8_000)
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            context_input_token_limit=10_000,
            context_compaction_safety_margin_tokens=0,
        ),
        chat_adapter=adapter,
        context_compactor=LLMCompactor(adapter, token_counter=token_counter),
        context_token_counter=token_counter,
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "hard-user",
        "hard-session",
        ConversationTurn("旧问题", "旧回答", "run-old", "trace-old"),
    )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="hard-user",
            session_id="hard-session",
            text="当前问题",
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert artifacts.state.status == "completed"
    assert len(adapter.requests) == 2
    assert all(
        request.user_query == "压缩已完成的会话历史"
        for request in adapter.requests
    )
    assert artifacts.state.response is not None
    assert "上下文过长且压缩失败" in artifacts.state.response.message
    assert artifacts.state.request.metadata["context_compaction_failed"] is True
    assert (
        artifacts.state.request.metadata["context_compaction_error_code"]
        == "ValueError"
    )
    assert artifacts.state.request.metadata["context_compaction_blocked"] is True
    assert artifacts.state.request.metadata[
        "context_compaction_provider_usage_history"
    ] == [
        {"prompt_tokens": 80, "total_tokens": 80},
        {"prompt_tokens": 80, "total_tokens": 80},
    ]
    assert [turn.user_text for turn in conversation_store.get(
        "hard-user",
        "hard-session",
    )] == ["旧问题"]


def test_compaction_preserves_current_run_native_tool_pair() -> None:
    adapter = _ToolTurnChatAdapter()
    token_counter = _ToolTurnTokenCounter(raw_history_tokens=7_000)
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            context_input_token_limit=10_000,
            context_compaction_safety_margin_tokens=0,
        ),
        chat_adapter=adapter,
        context_compactor=LLMCompactor(adapter, token_counter=token_counter),
        context_token_counter=token_counter,
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "tool-user",
        "tool-session",
        ConversationTurn("旧问题", "旧回答", "run-old", "trace-old"),
    )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="tool-user",
            session_id="tool-session",
            text="执行工具",
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert artifacts.state.status == "completed"
    assert len(adapter.requests) == 3
    final_messages = adapter.requests[-1].messages
    assert [message["role"] for message in final_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert final_messages[-2]["tool_calls"][0]["id"] == "call-context-probe"
    assert final_messages[-1]["tool_call_id"] == "call-context-probe"
    assert "observation-sentinel" in final_messages[-1]["content"]
    assert "旧问题" not in str(final_messages)
