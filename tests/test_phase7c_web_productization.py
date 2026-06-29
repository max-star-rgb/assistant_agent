from fastapi.testclient import TestClient

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
from multimodal_agent.services.chat_adapter import ChatRequest, ChatResult


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return ChatResult(response_text=self.outputs[index], provider=self.provider, model="scripted")


def test_demo_runtime_info_is_redacted_and_offline_by_default() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    response = client.get("/demo/runtime-info")
    payload = response.json()

    assert response.status_code == 200
    assert payload["protocol_version"] == "v1"
    assert payload["runtime_profile"] == "local_demo"
    assert payload["graph_mode"] == "assistant_loop"
    assert payload["offline_default"] is True
    assert payload["providers"]["chat"] == "mock"
    assert "api_key" not in response.text.lower()
    assert "authorization" not in response.text.lower()
    assert "bearer" not in response.text.lower()


def test_phase7c_console_contains_productized_web_controls() -> None:
    client = TestClient(create_app())

    response = client.get("/demo/console")
    html = response.text

    assert response.status_code == 200
    assert "Assistant Chat" in html
    assert "试用入口" in html
    assert "请输入你的工号：00xxxx" in html
    assert ">确认<" in html
    assert "trial-user-id" in html
    assert "multimodal_agent_trial_user_id" in html
    assert "Examples" in html
    assert "Input" in html
    assert "Plan Mode" in html
    assert "普通 ReAct" in html
    assert "计划优先" in html
    assert 'name="execution-strategy"' in html
    assert 'value="plan_and_solve"' in html
    assert "Conversation History" in html
    assert "Product Results" in html
    assert "Assistant ReAct Process" in html
    assert "Memory Snapshot" in html
    assert "长期记忆" in html
    assert "短期对话" in html
    assert "技术信息" in html
    assert "memoryStatusText" in html
    assert "memoryLayerLabel" in html
    assert "memoryTypeLabel" in html
    assert "memorySourceLabel" in html
    assert "memoryRecallText" in html
    assert "memory-query" in html
    assert "refreshMemorySnapshot" in html
    assert "renderMemorySnapshot" in html
    assert "deleteMemoryItem" in html
    assert "removeLocalSession" in html
    assert "/sessions/" in html
    assert 'method: "POST"' in html
    assert "会话已创建并写入服务端" in html
    assert "服务端短期对话历史已清理" in html
    assert "showMemoryDeleteConfirm" in html
    assert "setMemoryDeleteBusy" in html
    assert "确认删除" in html
    assert "取消" in html
    assert "已删除，snapshot 已刷新" in html
    assert 'method: "DELETE"' in html
    assert "/memory/users/" in html
    assert "/items/" in html
    assert "/snapshot?" in html
    assert "记忆层" in html
    assert "偏好/事实记忆" in html
    assert "任务/经历记忆" in html
    assert "来源" in html
    assert "底层类型" in html
    assert "召回原因" in html
    assert "Thought" not in html
    assert "thought" not in html
    assert "思维链" not in html
    assert "conversationHistory" in html
    assert "renderConversationHistory" in html
    assert "renderProductGallery" in html
    assert "runAssistantStream" in html
    assert "formatReactProcess" in html
    assert "formatTimelineEvent" in html
    assert "formatContextSummary" in html
    assert "context_budget" in html
    assert "context_compaction" in html
    assert "[plan]" in html
    assert "[tool:" in html
    assert "Final Decision Trace" not in html
    assert "Live events" not in html
    assert "formatReactSteps" not in html
    assert "new WebSocket" in html
    assert "params.set(\"user_id\", userId)" in html
    assert "execution_strategy: currentExecutionStrategy()" in html
    assert "socket.send(JSON.stringify(requestPayload))" in html
    assert "/ws/agent/" in html
    assert "Run detail panel" not in html
    assert "Trace detail panel" not in html
    assert "Browser-session request history" not in html


def test_phase7c_run_trace_detail_endpoints_support_console_flow() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={
            "user_id": "web_demo_user",
            "session_id": "web_demo_productization",
            "text": "生成一张日系极简商品海报。",
            "metadata": {"source": "web_console", "offline": True},
        },
    )
    run_payload = run_response.json()

    run_detail = client.get(f"/runs/{run_payload['run_id']}").json()
    trace_detail = client.get(f"/traces/{run_payload['trace_id']}").json()
    tool_detail = client.get(f"/runs/{run_payload['run_id']}/tool-calls").json()

    assert run_response.status_code == 200
    assert run_payload["errors"] == []
    assert run_payload["react_steps"]
    assert run_payload["decision_trace"]
    assert any(step.get("decision_type") == "tool_call" for step in run_payload["react_steps"])
    assert any(step.get("observation_tool") == "image_generation" for step in run_payload["react_steps"])
    assert any(step.get("event") == "decision" for step in run_payload["decision_trace"])
    assert any(step.get("event") == "observation" for step in run_payload["decision_trace"])
    assert any(step.get("reason") for step in run_payload["react_steps"])
    assert any(step.get("decision_summary") for step in run_payload["decision_trace"])
    assert all("thought" not in key.lower() for step in run_payload["react_steps"] for key in step)
    assert all("thought" not in key.lower() for step in run_payload["decision_trace"] for key in step)
    assert "api_key" not in run_response.text.lower()
    assert "authorization" not in run_response.text.lower()
    assert "bearer" not in run_response.text.lower()
    assert run_detail["run_id"] == run_payload["run_id"]
    assert run_detail["event_count"] > 0
    assert trace_detail["trace_id"] == run_payload["trace_id"]
    assert trace_detail["events"]
    assert tool_detail["run_id"] == run_payload["run_id"]
    assert "image_generation" in [call["tool_name"] for call in tool_detail["tool_calls"]]


def test_agent_run_accepts_explicit_plan_and_solve_strategy() -> None:
    try:
        routes_agent._RUNTIME = AgentGraphRuntime(
            chat_adapter=ScriptedChatAdapter(
                [
                    (
                        '{"type": "enter_plan_mode", "plan": {"goal": "search", "steps": ['
                        '{"step_id": "step_1", "action": "search_product", "tool_name": "product_search", '
                        '"input_refs": [], "depends_on": [], "required_inputs": ["query"], '
                        '"optional": false, "reason": "search first"}]}, "reason": "plan search"}'
                    ),
                    (
                        '{"type": "tool_call", "step_id": "step_1", "tool_name": "product_search", '
                        '"tool_input": {"query": "白色运动鞋", "top_k": 2}, "reason": "execute search"}'
                    ),
                    (
                        '{"type": "exit_plan_mode", "next_action": "final_answer", '
                        '"message": "plan complete", "reason": "search observed"}'
                    ),
                ]
            )
        )
        client = TestClient(create_app())

        response = client.post(
            "/agent/run",
            json={
                "user_id": "web_demo_user",
                "session_id": "web_demo_plan_strategy",
                "text": "找白色运动鞋",
                "execution_strategy": "plan_and_solve",
            },
        )
    finally:
        routes_agent._RUNTIME = None

    payload = response.json()
    assert response.status_code == 200
    assert payload["execution_strategy"] == "plan_and_solve"
    assert payload["data"]["final_answer_source"] == "assistant_loop"
    assert [call["tool_name"] for call in payload["tool_calls"]] == ["product_search"]


def test_phase7c_websocket_run_is_queryable_via_shared_http_runtime() -> None:
    routes_agent._RUNTIME = None
    client = TestClient(create_app())

    with client.websocket_connect("/ws/agent/ws_shared?text=帮我找相似款") as websocket:
        final = None
        while True:
            event = websocket.receive_json()
            if event["type"] == "agent_response" and event.get("payload", {}).get("response"):
                final = event["payload"]["response"]
                break

    assert final is not None
    run_id = final["run_id"]
    trace_id = final["trace_id"]

    # WS and HTTP share the same singleton runtime, so the WS run resolves here.
    run_detail = client.get(f"/runs/{run_id}")
    trace_detail = client.get(f"/traces/{trace_id}")
    tool_detail = client.get(f"/runs/{run_id}/tool-calls")

    assert run_detail.status_code == 200
    assert run_detail.json()["run_id"] == run_id
    assert trace_detail.status_code == 200
    assert trace_detail.json()["trace_id"] == trace_id
    assert tool_detail.status_code == 200
    assert tool_detail.json()["run_id"] == run_id
