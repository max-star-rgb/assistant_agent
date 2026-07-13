from __future__ import annotations

import asyncio
import threading
import time
import unittest
from collections import defaultdict
from typing import Any

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway import GatewaySessionService, InMemoryDuplex, frame
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.realtime import GatewayAgentAdapter, ProgressPolicy
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.memory import MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.assistant_run_service import (
    InMemoryConversationStore,
    run_assistant_request,
)
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.realtime_task_state import InMemoryRealtimeTaskStateStore
from assistant_agent.services.trace_store import InMemoryTraceStore


class _PersonalRealtimeScriptedAdapter:
    provider = "scripted-native"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self._counts: dict[str, int] = defaultdict(int)

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        query = request.user_query
        self._counts[query] += 1
        count = self._counts[query]

        if "记住我喜欢简短回答" in query:
            if count == 1:
                return _native_result(
                    "memory_save",
                    {
                        "query": "记住我喜欢简短回答",
                        "source_intent": "user_explicit",
                        "source_reason": "用户明确要求保存回答风格偏好。",
                        "future_use": "后续实时通话回答可以更简短。",
                        "evidence": "用户说：记住我喜欢简短回答",
                    },
                )
            return _final_result("已记住你的简短回答偏好。")

        if "按我保存的偏好" in query:
            return _final_result("我会按你保存的偏好，用更简短的方式回答。")

        if "今天 AI 行业最新消息" in query:
            if count == 1:
                return _native_result(
                    "web_search",
                    {"query": "AI industry latest news", "limit": 2},
                )
            return _final_result("已根据联网搜索结果简要回答。")

        return _final_result("已处理。")


class _CancelledMemorySaveAdapter:
    provider = "scripted-native"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.chat_started = threading.Event()
        self.release_chat = threading.Event()
        self.chat_returned = threading.Event()

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        self.chat_started.set()
        self.release_chat.wait(timeout=2)
        self.chat_returned.set()
        return _native_result(
            "memory_save",
            {
                "query": "记住取消中的偏好",
                "source_intent": "user_explicit",
                "source_reason": "用户明确要求保存偏好。",
                "future_use": "后续实时通话可参考。",
                "evidence": "用户说：记住取消中的偏好",
            },
        )


class PersonalRealtimeAssistantRuntimeGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_realtime_runtime_memory_skill_tool_trace_vertical_loop(
        self,
    ) -> None:
        harness = _RuntimeHarness(_PersonalRealtimeScriptedAdapter())
        session, client_ep, session_ep, session_task = harness.start_gateway_session()

        try:
            save_frames = await _run_gateway_turn(
                client_ep,
                session_id="personal-runtime-session",
                text="记住我喜欢简短回答",
            )
            recall_frames = await _run_gateway_turn(
                client_ep,
                session_id="personal-runtime-session",
                text="按我保存的偏好回答今天安排",
            )
            search_frames = await _run_gateway_turn(
                client_ep,
                session_id="personal-runtime-session",
                text="查一下今天 AI 行业最新消息",
                metadata={
                    "tool_visibility": {"enabled_skills": ["realtime_web_search"]}
                },
            )
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert session.config == {}
        assert _run_end(save_frames)["reason"] == "completed"
        assert _run_end(recall_frames)["reason"] == "completed"
        search_end = _run_end(search_frames)
        assert search_end["reason"] == "completed"
        assert search_end["payload"]["trace_id"]

        saved = harness.memory_store.search(
            MemoryQuery(user_id="default", query="简短回答")
        ).items
        assert saved
        assert {item.source for item in harness.memory_store.list_by_user("default")} == {
            "explicit_user_request",
            "user_profile",
        }

        save_state = harness.single_state("记住我喜欢简短回答")
        recall_state = harness.single_state("按我保存的偏好回答今天安排")
        search_state = harness.single_state("查一下今天 AI 行业最新消息")
        assert [call.tool_name for call in save_state.tool_calls] == ["memory_save"]
        assert [call.tool_name for call in search_state.tool_calls] == ["web_search"]
        assert recall_state.request.metadata["memory_recall_report"]["injected_count"] >= 1
        assert recall_state.request.metadata["memory_recall_report"]["query_present"] is True
        assert recall_state.request.metadata["memory_context_injected_ids"]

        recall_request = harness.adapter_request("按我保存的偏好回答今天安排")
        recall_message = _joined_message_content(recall_request)
        assert "简短回答" in recall_message
        assert "相关记忆" in recall_message

        search_request = harness.adapter_request("查一下今天 AI 行业最新消息")
        search_message = _joined_message_content(search_request)
        assert "realtime_web_search" in search_message
        assert "ToolExecutor" in search_message
        assert "web_search" in _native_tool_names(search_request)
        assert "run_skill" not in _native_tool_names(search_request)
        assert any(frame["type"] == "event.tool" for frame in search_frames)
        assert any(frame["type"] == "stream.chunk" for frame in search_frames)

        context_report = search_state.request.metadata["last_context_report_v1"]
        assert context_report["skill_report"]["selected_skill_ids"] == [
            "realtime_web_search"
        ]
        assert context_report["skill_report"]["governed_tool_names"] == ["web_search"]

        trace_events = harness.trace_store.list_by_trace(search_end["payload"]["trace_id"])
        canonical_events = [event.canonical_event for event in trace_events]
        assert "context.report" in canonical_events
        assert "tool.started" in canonical_events
        assert "tool.finished" in canonical_events
        assert "realtime.backend.finished" in canonical_events
        context_build = next(
            event
            for event in trace_events
            if event.canonical_event == "context.build.finished"
            and event.output_summary.get("context", {}).get("skill_report_v1", {}).get(
                "selected_skill_ids"
            )
        )
        assert context_build.output_summary["context"]["skill_report_v1"][
            "selected_skill_ids"
        ] == ["realtime_web_search"]

    async def test_cancelled_realtime_turn_does_not_write_late_memory_tool_call(
        self,
    ) -> None:
        adapter = _CancelledMemorySaveAdapter()
        harness = _RuntimeHarness(adapter)
        _session, client_ep, session_ep, session_task = harness.start_gateway_session()
        frames: list[dict[str, Any]] = []

        async def read_cancel_flow() -> None:
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await asyncio.to_thread(adapter.chat_started.wait, 2)
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="personal-runtime-cancel",
                            run_id=received["run_id"],
                            payload={"reason": "user_cancelled_memory_write"},
                        )
                    )
                if received["type"] == "run.end":
                    return

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="personal-runtime-cancel",
                    payload={"text": "记住取消中的偏好"},
                )
            )
            await asyncio.wait_for(read_cancel_flow(), timeout=3.0)
            adapter.release_chat.set()
            await asyncio.to_thread(adapter.chat_returned.wait, 2)
            await _wait_until(lambda: bool(harness.states_by_text["记住取消中的偏好"]))
        finally:
            adapter.release_chat.set()
            await _close_session(client_ep, session_ep, session_task)

        run_end = _run_end(frames)
        assert run_end["reason"] == "cancelled"
        assert run_end["payload"]["cancel"]["source"] == "gateway_cancel"
        assert harness.memory_store.search(
            MemoryQuery(user_id="default", query="取消中的偏好")
        ).items == []
        state = harness.single_state("记住取消中的偏好")
        assert state.status == "cancelled"
        assert state.tool_calls == []


class _RuntimeHarness:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.memory_store = InMemoryStore()
        self.trace_store = InMemoryTraceStore()
        self.conversation_store = InMemoryConversationStore()
        self.realtime_task_state_store = InMemoryRealtimeTaskStateStore()
        self.runtime = AgentGraphRuntime(
            config=ProviderConfig(),
            memory_store=self.memory_store,
            trace_store=self.trace_store,
            chat_adapter=adapter,
        )
        self.states_by_text: dict[str, list[Any]] = defaultdict(list)

    def run_request(self, request: UserRequest, **kwargs: Any) -> Any:
        artifacts = run_assistant_request(
            request,
            runtime=self.runtime,
            event_sink=kwargs.get("event_sink"),
            conversation_store=self.conversation_store,
            realtime_task_state_store=self.realtime_task_state_store,
            load_env=False,
            cancel_token=kwargs.get("cancel_token"),
        )
        self.states_by_text[request.text or ""].append(artifacts.state)
        return artifacts

    def start_gateway_session(self) -> tuple[GatewaySessionService, Any, Any, asyncio.Task[None]]:
        session = GatewaySessionService(
            backend=GatewayAgentAdapter(
                run_request=self.run_request,
                load_env=False,
                progress_policy=ProgressPolicy(first_progress_timeout_s=0),
            )
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        return session, client_ep, session_ep, session_task

    def single_state(self, text: str) -> Any:
        states = self.states_by_text[text]
        assert len(states) == 1
        return states[0]

    def adapter_request(self, query: str) -> ChatRequest:
        for request in self.adapter.requests:
            if request.user_query == query:
                return request
        raise AssertionError(f"missing adapter request for query: {query}")


async def _run_gateway_turn(
    client_ep: Any,
    *,
    session_id: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    await client_ep.send(
        frame(
            type="message.user",
            session_id=session_id,
            payload={"text": text, "metadata": dict(metadata or {})},
        )
    )
    async for received in client_ep:
        frames.append(received)
        if received["type"] == "run.end":
            return frames
    raise AssertionError("gateway session closed before run.end")


async def _close_session(client_ep: Any, session_ep: Any, session_task: asyncio.Task[None]) -> None:
    await client_ep.close()
    await session_ep.close()
    session_task.cancel()
    await asyncio.gather(session_task, return_exceptions=True)


async def _wait_until(predicate: Any, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not satisfied before timeout")


def _run_end(frames: list[dict[str, Any]]) -> dict[str, Any]:
    for item in reversed(frames):
        if item["type"] == "run.end":
            return item
    raise AssertionError("missing run.end frame")


def _joined_message_content(request: ChatRequest) -> str:
    return "\n".join(str(message.get("content") or "") for message in request.messages)


def _native_tool_names(request: ChatRequest) -> list[str]:
    names: list[str] = []
    for tool in request.tools:
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _native_result(name: str, arguments: dict[str, Any]) -> ChatResult:
    return ChatResult(
        response_text="",
        tool_calls=[
            NativeToolCall(
                id="call_1",
                name=name,
                arguments=arguments,
                raw={
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                },
            )
        ],
        finish_reason="tool_calls",
        message_kind="tool_call",
        provider="scripted-native",
        model="personal-runtime-test",
    )


def _final_result(message: str) -> ChatResult:
    return ChatResult(
        response_text=message,
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-native",
        model="personal-runtime-test",
    )


if __name__ == "__main__":
    unittest.main()
