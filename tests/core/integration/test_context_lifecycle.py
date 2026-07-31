from __future__ import annotations

import json

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.context.compactor import ContextCompactionResult
from assistant_agent.context.models import ContextSummary
from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.assistant_run_service import (
    ConversationTurn,
    InMemoryConversationStore,
    JsonlConversationStore,
    run_assistant_request,
)
from assistant_agent.runtime.chat_adapter import (
    ChatRequest,
    ChatResult,
)
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import ProbeTool, offline_config, sealed_registry


@pytest.fixture(autouse=True)
def default_registry_assembly_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_default_registry(*args, **kwargs):
        raise AssertionError("default-registry-called")

    monkeypatch.setattr(
        "assistant_agent.runtime.runtime.create_default_registry",
        reject_default_registry,
    )


class ThresholdTokenCounter:
    tokenizer_id = "tokenizer-sentinel"

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


class CompactionChatAdapter:
    provider = "scripted"
    model = "model-sentinel"

    def __init__(
        self,
        *,
        tool_turn: bool = False,
    ) -> None:
        self.tool_turn = tool_turn
        self.main_turns = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        self.main_turns += 1
        if self.tool_turn and self.main_turns == 1:
            return ChatResult(
                provider=self.provider,
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-sentinel",
                        name=ProbeTool.name,
                        arguments={"value": "observation-sentinel"},
                    )
                ],
            )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="response-sentinel",
            usage={
                "prompt_tokens": 22,
                "completion_tokens": 3,
                "total_tokens": 25,
            },
        )


class StructuredCompactor:
    compactor_type = "structured-fake"

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails

    def compact(
        self,
        *,
        conversation,
        current_request,
        observations,
        budget_report=None,
        existing_summary=None,
        source_token_count=0,
        summary_max_tokens=0,
    ) -> ContextCompactionResult:
        if self.fails:
            raise ValueError("compaction-failed")
        return ContextCompactionResult(
            summary=ContextSummary(
                schema_version="context_summary_v1",
                task_state="summary-sentinel",
                summary_revision=(
                    (existing_summary.summary_revision if existing_summary else 0)
                    + 1
                ),
                covered_turn_count=len(conversation),
                source_turn_count=len(conversation),
                source_token_count=source_token_count,
            ),
            compactor_type=self.compactor_type,
        )


class ToolTurnTokenCounter(ThresholdTokenCounter):
    def count_chat_request(self, request: ChatRequest) -> int:
        if any(message.get("role") == "tool" for message in request.messages):
            return 7_000
        return 6_000


def _runtime(
    adapter: CompactionChatAdapter,
    token_counter: ThresholdTokenCounter,
    *,
    compaction_fails: bool = False,
    trace_store: InMemoryTraceStore | None = None,
) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=ProviderConfig(
            langgraph_checkpointer_backend="none",
            context_input_token_limit=10_000,
            context_compaction_safety_margin_tokens=0,
            context_summary_max_tokens=512,
        ),
        chat_adapter=adapter,
        context_compactor=StructuredCompactor(fails=compaction_fails),
        context_token_counter=token_counter,
        session_store=InMemorySessionStore(),
        trace_store=trace_store,
    )


@pytest.mark.core_invariant("CTX-001")
def test_context_window_policy_uses_configured_ratios() -> None:
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


@pytest.mark.core_invariant("CTX-001")
def test_compaction_replaces_only_covered_history_prefix(tmp_path) -> None:
    path = tmp_path / "conversation.jsonl"
    store = JsonlConversationStore(path)
    for index in range(3):
        store.append(
            "user-sentinel",
            "target-session",
            ConversationTurn(
                user_text=f"target-user-{index}-sentinel",
                assistant_text=f"target-assistant-{index}-sentinel",
                run_id=f"target-run-{index}",
                trace_id=f"target-trace-{index}",
            ),
        )
    store.append(
        "user-sentinel",
        "other-session",
        ConversationTurn(
            "other-user-sentinel",
            "other-assistant-sentinel",
            "other-run",
            "other-trace",
        ),
    )
    summary = ContextSummary(
        schema_version="context_summary_v1",
        task_state="summary-sentinel",
        summary_revision=1,
        covered_turn_count=2,
        source_turn_count=2,
    )

    store.replace_history_prefix_with_summary(
        "user-sentinel",
        "target-session",
        summary,
        covered_turn_count=2,
    )

    reloaded = JsonlConversationStore(path)
    assert [
        turn.user_text
        for turn in reloaded.get("user-sentinel", "target-session")
    ] == ["target-user-2-sentinel"]
    assert [
        turn.user_text
        for turn in reloaded.get("user-sentinel", "other-session")
    ] == ["other-user-sentinel"]
    assert reloaded.get_summary("user-sentinel", "target-session") == summary


@pytest.mark.core_invariant("CTX-001")
def test_soft_compaction_failure_keeps_raw_history() -> None:
    adapter = CompactionChatAdapter()
    runtime = _runtime(
        adapter,
        ThresholdTokenCounter(raw_history_tokens=7_000),
        compaction_fails=True,
    )
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "user-sentinel",
        "session-sentinel",
        ConversationTurn(
            "old-user-sentinel",
            "old-assistant-sentinel",
            "old-run",
            "old-trace",
        ),
    )
    try:
        artifacts = run_assistant_request(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="current-user-sentinel",
            ),
            runtime=runtime,
            conversation_store=conversation_store,
        )

        assert artifacts.state.status == "completed"
        assert adapter.main_turns == 1
        assert conversation_store.get_summary(
            "user-sentinel",
            "session-sentinel",
        ) is None
        assert [
            turn.user_text
            for turn in conversation_store.get(
                "user-sentinel",
                "session-sentinel",
            )
        ] == ["old-user-sentinel", "current-user-sentinel"]
        assert artifacts.state.request.metadata["context_compaction_failed"] is True
    finally:
        runtime.close()


@pytest.mark.core_invariant("CTX-001")
def test_hard_compaction_failure_blocks_provider_call() -> None:
    adapter = CompactionChatAdapter()
    runtime = _runtime(
        adapter,
        ThresholdTokenCounter(raw_history_tokens=8_500),
        compaction_fails=True,
    )
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "user-sentinel",
        "session-sentinel",
        ConversationTurn(
            "old-user-sentinel",
            "old-assistant-sentinel",
            "old-run",
            "old-trace",
        ),
    )
    try:
        artifacts = run_assistant_request(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="current-user-sentinel",
            ),
            runtime=runtime,
            conversation_store=conversation_store,
        )

        assert artifacts.state.status == "completed"
        assert adapter.main_turns == 0
        assert artifacts.state.request.metadata["context_compaction_failed"] is True
        assert artifacts.state.request.metadata["context_compaction_blocked"] is True
        assert [
            turn.user_text
            for turn in conversation_store.get(
                "user-sentinel",
                "session-sentinel",
            )
        ] == ["old-user-sentinel"]
    finally:
        runtime.close()


@pytest.mark.core_invariant("CTX-001")
def test_compaction_preserves_current_native_tool_pair() -> None:
    adapter = CompactionChatAdapter(tool_turn=True)
    runtime = _runtime(
        adapter,
        ToolTurnTokenCounter(raw_history_tokens=7_000),
    )
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "user-sentinel",
        "session-sentinel",
        ConversationTurn(
            "old-user-sentinel",
            "old-assistant-sentinel",
            "old-run",
            "old-trace",
        ),
    )
    try:
        artifacts = run_assistant_request(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="current-user-sentinel",
            ),
            runtime=runtime,
            conversation_store=conversation_store,
        )

        assert artifacts.state.status == "completed"
        assert [
            call.tool_name for call in artifacts.state.tool_calls
        ] == [ProbeTool.name]
        final_messages = adapter.requests[-1].messages
        assert [message["role"] for message in final_messages] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        assert final_messages[-2]["tool_calls"][0]["id"] == "call-sentinel"
        assert final_messages[-1]["tool_call_id"] == "call-sentinel"
        assert "observation-sentinel" in final_messages[-1]["content"]
        assert "old-user-sentinel" not in str(final_messages)
    finally:
        runtime.close()


@pytest.mark.core_invariant("CTX-001")
def test_compiled_accounting_matches_provider_request() -> None:
    adapter = CompactionChatAdapter()
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        trace_store=trace_store,
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="request-sentinel",
            )
        )

        request = adapter.requests[0]
        context_event = next(
            event
            for event in trace_store.list_by_run(state.run_id)
            if event.canonical_event == "context.build.finished"
        )
        report = context_event.output_summary["context_report_v2"]
        message_chars = len(
            json.dumps(
                request.messages,
                ensure_ascii=False,
                default=str,
            )
        )
        tool_chars = len(
            json.dumps(
                request.tools,
                ensure_ascii=False,
                default=str,
            )
        )

        assert report["schema_version"] == "context_report_v2"
        assert report["compiled_accounting_status"] == "available"
        assert report["compiled_message_chars"] == message_chars
        assert report["compiled_tool_schema_chars"] == tool_chars
        assert report["compiled_response_format_chars"] == 0
        assert report["compiled_request_chars"] == message_chars + tool_chars
        assert report["sections"]["system_prompt"]["chars"] == len(
            request.messages[0]["content"]
        )
        assert report["sections"]["tool_schema"]["chars"] == tool_chars
    finally:
        runtime.close()
