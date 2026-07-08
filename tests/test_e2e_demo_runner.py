import json
import subprocess
import sys

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import AgentResponse
import scripts.run_demo_flows as runner
from scripts.run_demo_flows import run_demo_flows


REQUIRED_RESULT_KEYS = {
    "scenario_id",
    "status",
    "tool_sequence",
    "response_text",
    "errors",
    "run_id",
    "trace_id",
}


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_demo_gateway_test")
        state.set_response(AgentResponse(message="demo gateway response"))
        return state


def test_demo_runner_import_is_safe() -> None:
    import scripts.run_demo_flows as runner

    assert callable(runner.run_demo_flows)


def test_demo_runner_runs_all_mock_scenarios() -> None:
    summary = run_demo_flows()

    assert summary["total"] >= 6
    assert summary["failed"] == 0
    assert summary["passed"] == summary["total"]
    assert all(result["status"] == "succeeded" for result in summary["results"])
    assert all(result["checks"]["expected_tools_match"] for result in summary["results"])
    assert all(result["checks"]["response_contains_match"] for result in summary["results"])
    assert all(result["checks"]["non_generic_response"] for result in summary["results"])


def test_demo_runner_runs_single_scenario() -> None:
    summary = run_demo_flows("product_search_compare")

    assert summary["total"] == 1
    result = summary["results"][0]
    assert result["scenario_id"] == "product_search_compare"
    assert result["tool_sequence"] == ["product_search", "price_compare"]


def test_demo_runner_runs_scenarios_through_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(runner, "create_runtime", lambda **kwargs: runtime, raising=False)

    summary = runner.run_demo_flows("product_search_compare")

    result = summary["results"][0]
    assert result["run_id"] == "run_demo_gateway_test"
    assert result["response_text"] == "demo gateway response"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.metadata["runtime"]["history"] == [request.text]
    assert request.metadata["offline"] is True


def test_demo_runner_output_json_structure_from_cli() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_demo_flows.py", "--scenario", "text_image_generation"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)
    scenario = summary["results"][0]

    assert summary["total"] == 1
    assert REQUIRED_RESULT_KEYS.issubset(set(scenario))
    assert scenario["tool_sequence"] == ["image_generation"]
    assert scenario["response_text"] != "已完成请求处理。"
    assert "图片生成结果" in scenario["response_text"]


def test_demo_runner_tool_sequence_is_verifiable() -> None:
    summary = run_demo_flows("full_multistep_image_search_compare_generate")
    result = summary["results"][0]

    assert result["tool_sequence"] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert result["checks"]["expected_tools_match"] is True
    assert result["response_text"] != "已完成请求处理。"


def test_demo_runner_unknown_scenario_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_demo_flows.py", "--scenario", "missing"],
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert result.returncode == 2
    assert payload["error"] == "Unknown scenario_id: missing"
