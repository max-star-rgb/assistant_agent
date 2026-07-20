import json
from datetime import datetime, timezone

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent, trace_debug_summary


TERMINAL_CANONICAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}


class ScriptedNativeChatAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[index]


def _memory_store_with_secret_summary() -> InMemoryStore:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="pref_secret_style",
            user_id="u1",
            session_id="s0",
            memory_type="preference",
            summary="用户喜欢绝密紫色极简风格。",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    return store


def _canonical(events: list[dict]) -> list[str]:
    return [event["canonical_event"] for event in events if event["canonical_event"]]


def _index(canonical: list[str], event_name: str, *, last: bool = False) -> int:
    if not last:
        return canonical.index(event_name)
    return len(canonical) - 1 - canonical[::-1].index(event_name)


def _assert_before(canonical: list[str], *event_names: str) -> None:
    positions = [_index(canonical, name) for name in event_names]
    assert positions == sorted(positions), {name: _index(canonical, name) for name in event_names}


def _assert_single_terminal(canonical: list[str], expected: str = "run.completed") -> None:
    terminals = [event for event in canonical if event in TERMINAL_CANONICAL_EVENTS]
    assert terminals == [expected]


def _assert_single_tool_lifecycle(canonical: list[str]) -> None:
    assert canonical.count("tool.started") == 1
    assert canonical.count("tool.finished") == 1
    assert "tool.failed" not in canonical
    _assert_before(canonical, "tool.started", "tool.finished", "tool.observation")


def _assert_trace_text_absent(events: list[dict], *needles: str) -> None:
    dumped = json.dumps(events, ensure_ascii=False, default=str)
    for needle in needles:
        assert needle not in dumped


def test_trace_event_summary_exposes_redacted_canonical_fields() -> None:
    store = InMemoryTraceStore()
    store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            user_id="u1",
            session_id="s1",
            node_name="native_runtime",
            event_type="assistant_decision",
            canonical_event="react.decision",
            span_id="span_decision_1",
            parent_span_id="span_run_1",
            attributes={
                "decision_type": "tool_call",
                "api_key": "secret",
                "raw_provider_payload": {"token": "hidden"},
            },
        )
    )

    summary = trace_debug_summary(store.list_by_trace("trace_1"))
    event = summary["events"][0]

    assert event["canonical_event"] == "react.decision"
    assert event["span_id"] == "span_decision_1"
    assert event["parent_span_id"] == "span_run_1"
    assert event["attributes"]["decision_type"] == "tool_call"
    dumped = json.dumps(event, ensure_ascii=False, default=str).lower()
    assert "secret" not in dumped
    assert "raw_provider_payload" not in dumped


def test_mock_react_runtime_emits_canonical_run_decision_tool_observation_and_terminal_events() -> None:
    trace_store = InMemoryTraceStore()
    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]

    assert "run.started" in canonical
    assert "react.decision" in canonical
    assert "action.validation.finished" in canonical
    assert "tool.observation" in canonical
    assert "run.completed" in canonical
    assert canonical.count("run.completed") == 1
    assert all("thought" not in json.dumps(event, ensure_ascii=False, default=str).lower() for event in events)


def test_mock_runtime_emits_context_build_trace_with_budget_summary() -> None:
    trace_store = InMemoryTraceStore()
    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找相似款",
            metadata={"context_budget_estimate_tokens": True, "context_budget_max_tokens": 1000},
        )
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    context_events = [event for event in events if event["canonical_event"] == "context.build.finished"]

    assert context_events
    event = context_events[0]
    assert event["status"] == "succeeded"
    assert event["attributes"]["iteration"] == 1
    assert event["attributes"]["max_iterations"] >= 1
    context = event["output_summary"]["context"]
    assert context["context_schema_version"] == "context_observability_v1"
    assert context["budget"]["total_chars"] > 0
    assert "context_usage_ratio" in context["budget"]
    assert "compaction" in context
    dumped = json.dumps(event, ensure_ascii=False, default=str).lower()
    assert "raw_provider_payload" not in dumped
    assert "thought" not in dumped


def test_react_decision_trace_includes_context_report_v1() -> None:
    trace_store = InMemoryTraceStore()
    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我比价通勤耳机，找最低价")
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    decision_event = next(
        event
        for event in events
        if event["canonical_event"] == "react.decision" and "context_report_v1" in event["output_summary"]
    )
    report = decision_event["output_summary"]["context_report_v1"]

    assert report["schema_version"] == "context_report_v1"
    assert report["sections"]["system_prompt"]["chars"] > 0
    assert report["sections"]["tool_schema"]["item_count"] >= 1
    assert "shopping_search" in report["selected_tool_names"]


def test_mock_runtime_emits_memory_lifecycle_trace_without_memory_content() -> None:
    trace_store = InMemoryTraceStore()
    state = AgentGraphRuntime(
        memory_store=_memory_store_with_secret_summary(),
        trace_store=trace_store,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="继续推荐日系极简风格商品"))

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]
    load_event = next(event for event in events if event["canonical_event"] == "memory.load.finished")
    save_event = next(event for event in events if event["canonical_event"] == "memory.save.finished")

    assert "memory.load.started" in canonical
    assert "memory.load.finished" in canonical
    assert "memory.save.started" in canonical
    assert "memory.save.finished" in canonical
    assert canonical.index("memory.load.started") < canonical.index("memory.load.finished")
    assert load_event["status"] == "succeeded"
    assert load_event["attributes"]["retrieval_count"] >= 1
    assert load_event["attributes"]["injected_count"] >= 1
    assert load_event["attributes"]["retrieval_version"]
    assert load_event["output_summary"]["memory"]["injected_memory_ids"] == ["pref_secret_style"]
    assert save_event["status"] == "succeeded"
    assert "save_candidate_count" in save_event["attributes"]
    dumped = json.dumps([load_event, save_event], ensure_ascii=False, default=str)
    assert "绝密紫色极简风格" not in dumped
    assert "memory_context_text" not in dumped
    assert "raw_provider_payload" not in dumped


def test_mock_runtime_trace_satisfies_success_timeline_invariants() -> None:
    trace_store = InMemoryTraceStore()
    state = AgentGraphRuntime(
        memory_store=_memory_store_with_secret_summary(),
        trace_store=trace_store,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="继续推荐日系极简风格商品"))

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = _canonical(events)
    final_event = next(event for event in events if event["canonical_event"] == "response.final")

    _assert_single_terminal(canonical)
    _assert_single_tool_lifecycle(canonical)
    _assert_before(
        canonical,
        "run.started",
        "memory.load.started",
        "memory.load.finished",
        "context.build.started",
        "context.build.finished",
        "react.decision",
    )
    _assert_before(
        canonical,
        "action.validation.finished",
        "tool.started",
        "tool.finished",
        "tool.observation",
        "response.final",
        "memory.save.started",
        "memory.save.finished",
        "run.completed",
    )
    assert final_event["status"] == "succeeded"
    assert final_event["attributes"]["message_present"] is True
    assert final_event["attributes"]["message_chars"] > 0
    _assert_trace_text_absent(events, "绝密紫色极简风格", "参考记忆", "raw_provider_payload", "thought")


def test_native_runtime_emits_canonical_llm_decision_validation_observation_and_terminal_events() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-test",
                latency_ms=11,
                tool_calls=[
                    NativeToolCall(
                        id="call_native_1",
                        name="shopping_search",
                        arguments={"query": "白色运动鞋"},
                    )
                ],
                message_kind="tool_calls",
            ),
            ChatResult(
                response_text="找到了一些白色运动鞋。",
                provider="scripted",
                model="native-test",
                latency_ms=13,
                message_kind="content",
            ),
        ]
    )
    state = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋")
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]

    assert "run.started" in canonical
    assert canonical.count("llm.chat.finished") == 2
    assert "react.decision" in canonical
    assert "action.validation.finished" in canonical
    assert "tool.observation" in canonical
    assert "run.completed" in canonical
    assert canonical.index("react.decision") < canonical.index("tool.observation")


def test_native_runtime_emits_context_build_trace_with_budget_summary() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="可以直接回答。",
                provider="scripted",
                model="native-test",
                latency_ms=7,
                message_kind="content",
            )
        ]
    )
    state = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="简单介绍一下今天的任务",
            metadata={"context_budget_estimate_tokens": True, "context_budget_max_tokens": 1000},
        )
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]
    context_event = next(event for event in events if event["canonical_event"] == "context.build.finished")

    assert "context.build.started" in canonical
    assert "context.build.finished" in canonical
    assert canonical.index("context.build.started") < canonical.index("context.build.finished")
    assert context_event["status"] == "succeeded"
    assert context_event["attributes"]["iteration"] == 1
    assert context_event["output_summary"]["context"]["budget"]["total_chars"] > 0


def test_native_runtime_emits_context_report_with_selected_tool_schema() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="可以直接回答。",
                provider="scripted",
                model="native-test",
                latency_ms=7,
                message_kind="content",
            )
        ]
    )
    state = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    report_event = next(event for event in events if event["canonical_event"] == "context.report")
    report = report_event["output_summary"]["context_report_v1"]
    exposed_tool_names = [
        tool["function"]["name"] for tool in adapter.requests[0].tools
    ]

    assert report["sections"]["system_prompt"]["chars"] > 0
    assert report["selected_tool_names"] == exposed_tool_names
    assert report["sections"]["tool_schema"]["item_count"] == len(exposed_tool_names)
    assert "shopping_search" in report["selected_tool_names"]


def test_native_runtime_emits_memory_load_trace_without_memory_content() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="可以继续推荐。",
                provider="scripted",
                model="native-test",
                latency_ms=7,
                message_kind="content",
            )
        ]
    )
    state = AgentGraphRuntime(
        memory_store=_memory_store_with_secret_summary(),
        trace_store=trace_store,
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="继续推荐日系极简风格商品"))

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]
    load_event = next(event for event in events if event["canonical_event"] == "memory.load.finished")

    assert "memory.load.started" in canonical
    assert "memory.load.finished" in canonical
    assert canonical.index("memory.load.finished") < canonical.index("context.build.started")
    assert load_event["status"] == "succeeded"
    assert load_event["attributes"]["retrieval_count"] >= 1
    assert load_event["output_summary"]["memory"]["injected_memory_ids"] == ["pref_secret_style"]
    dumped = json.dumps(load_event, ensure_ascii=False, default=str)
    assert "绝密紫色极简风格" not in dumped
    assert "memory_context_text" not in dumped


def test_native_runtime_trace_satisfies_success_timeline_invariants() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-test",
                latency_ms=11,
                tool_calls=[
                    NativeToolCall(
                        id="call_native_1",
                        name="shopping_search",
                        arguments={"query": "白色运动鞋"},
                    )
                ],
                message_kind="tool_calls",
            ),
            ChatResult(
                response_text="找到了一些白色运动鞋。",
                provider="scripted",
                model="native-test",
                latency_ms=13,
                message_kind="content",
            ),
        ]
    )
    state = AgentGraphRuntime(
        memory_store=_memory_store_with_secret_summary(),
        trace_store=trace_store,
        chat_adapter=adapter,
    ).run_state(UserRequest(user_id="u1", session_id="s1", text="继续推荐白色运动鞋"))

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = _canonical(events)
    final_event = next(event for event in events if event["canonical_event"] == "response.final")
    memory_save_event = next(event for event in events if event["canonical_event"] == "memory.save.finished")

    _assert_single_terminal(canonical)
    _assert_single_tool_lifecycle(canonical)
    assert canonical.count("llm.chat.finished") == 2
    assert canonical.count("react.decision") >= 2
    assert "native_runtime.finished" in canonical
    assert "runtime.postprocess.finished" in canonical
    llm_events = [event for event in events if event["canonical_event"] == "llm.chat.finished"]
    assert llm_events[0]["attributes"]["provider_latency_ms"] == 11
    assert isinstance(llm_events[0]["attributes"]["wall_latency_ms"], int)
    assert llm_events[0]["attributes"]["wall_latency_ms"] >= 0
    _assert_before(
        canonical,
        "run.started",
        "memory.load.started",
        "memory.load.finished",
        "context.build.started",
        "context.build.finished",
        "llm.chat.finished",
        "react.decision",
        "action.validation.finished",
        "tool.started",
        "tool.finished",
        "tool.observation",
        "native_runtime.finished",
        "runtime.postprocess.finished",
    )
    assert _index(canonical, "tool.observation") < _index(canonical, "response.final", last=True)
    assert _index(canonical, "response.final", last=True) < _index(canonical, "memory.save.started")
    assert _index(canonical, "memory.save.finished") < _index(canonical, "run.completed")
    assert final_event["status"] == "succeeded"
    assert final_event["attributes"]["message_present"] is True
    assert final_event["attributes"]["message_chars"] == len("找到了一些白色运动鞋。")
    assert memory_save_event["status"] == "skipped"
    assert memory_save_event["attributes"]["skip_reason"] == "native_runtime_memory_writes_are_llm_tool_calls"
    native_finished = next(event for event in events if event["canonical_event"] == "native_runtime.finished")
    postprocess_finished = next(event for event in events if event["canonical_event"] == "runtime.postprocess.finished")
    assert isinstance(native_finished["latency_ms"], int)
    assert native_finished["latency_ms"] >= 0
    assert isinstance(postprocess_finished["latency_ms"], int)
    assert postprocess_finished["latency_ms"] >= 0
    _assert_trace_text_absent(
        events,
        "找到了一些白色运动鞋。",
        "绝密紫色极简风格",
        "raw_provider_payload",
        "thought",
    )


def test_native_runtime_traces_validation_and_response_finalization_latency() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-test",
                latency_ms=3,
                tool_calls=[
                    NativeToolCall(
                        id="call_native_latency",
                        name="shopping_search",
                        arguments={"query": "白色运动鞋"},
                    )
                ],
                message_kind="tool_calls",
            ),
            ChatResult(
                response_text="处理完成。",
                provider="scripted",
                model="native-test",
                latency_ms=4,
                message_kind="content",
            ),
        ]
    )

    state = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋")
    )

    events = trace_store.list_by_run(state.run_id)
    validation = next(event for event in events if event.canonical_event == "action.validation.finished")
    final = next(event for event in events if event.canonical_event == "response.final")
    assert isinstance(validation.latency_ms, int)
    assert validation.latency_ms >= 0
    assert isinstance(final.latency_ms, int)
    assert final.latency_ms >= 0


def test_mock_graph_traces_response_finalization_latency() -> None:
    trace_store = InMemoryTraceStore()

    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="你好")
    )

    final = next(
        event
        for event in trace_store.list_by_run(state.run_id)
        if event.canonical_event == "response.final"
    )
    assert isinstance(final.latency_ms, int)
    assert final.latency_ms >= 0
