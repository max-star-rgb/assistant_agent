import json
import subprocess
import sys

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import AgentResponse
from scripts.run_assistant_cli import run_text_prompt


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_cli_gateway_test")
        state.set_response(AgentResponse(message="cli gateway response"))
        return state


def test_assistant_cli_text_prompt_returns_core_fields() -> None:
    payload = run_text_prompt("帮我写一段商品介绍")

    assert payload["status"] == "succeeded"
    assert payload["response_text"] != "已完成请求处理。"
    assert "商品介绍" in payload["response_text"]
    assert isinstance(payload["tool_sequence"], list)
    assert payload["run_id"].startswith("run_")
    assert payload["trace_id"].startswith("trace_")
    assert payload["errors"] == []
    assert payload["offline"] is True


def test_assistant_cli_text_prompt_runs_through_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr("scripts.run_assistant_cli.create_runtime", lambda **kwargs: runtime)

    payload = run_text_prompt("你好", user_id="cli-u1", session_id="cli-s1")

    assert payload["run_id"] == "run_cli_gateway_test"
    assert payload["response_text"] == "cli gateway response"
    assert len(runtime.requests) == 1
    assert runtime.requests[0].metadata["runtime"]["history"] == ["你好"]
    assert runtime.requests[0].metadata["offline"] is True


def test_assistant_cli_json_output_from_text() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_assistant_cli.py", "--text", "生成一张日系极简海报"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "succeeded"
    assert payload["tool_sequence"] == ["image_generation"]
    assert "图片" in payload["response_text"]
    assert payload["run_id"].startswith("run_")
    assert payload["trace_id"].startswith("trace_")


def test_assistant_cli_scenario_reuses_demo_matrix() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_assistant_cli.py", "--scenario", "shopping_search"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["scenario_id"] == "shopping_search"
    assert payload["tool_sequence"] == ["shopping_search"]
    assert "价格" in payload["response_text"]
    assert payload["offline"] is True


def test_assistant_cli_text_format_includes_trace_fields() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_assistant_cli.py",
            "--text",
            "看看这张图里有什么商品",
            "--image-ref",
            "demo_image_product_1",
            "--format",
            "text",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "response_text:" in result.stdout
    assert "tool_sequence: vision_understanding" in result.stdout
    assert "run_id: run_" in result.stdout
    assert "trace_id: trace_" in result.stdout
