from pathlib import Path

from multimodal_agent.mcp.server import OfflineMCPServer
from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


def test_mcp_server_source_uses_runtime_and_registry_not_provider_sdks() -> None:
    source = Path("src/multimodal_agent/mcp/server.py").read_text(encoding="utf-8")

    assert "AgentGraphRuntime" in source
    assert "ToolRegistry" in source
    assert "openai" not in source.lower()
    assert "dashscope" not in source.lower()
    assert "requests." not in source
    assert "httpx." not in source


def test_mcp_redacts_sensitive_error_material() -> None:
    result = OfflineMCPServer().call_tool("missing_tool", {"api_key": "sk-test-secret"})
    payload = result.model_dump_json()

    assert result.status == "failed"
    assert "sk-test-secret" not in payload
    assert "api_key" not in payload


def test_packaging_eval_suite_passes_offline() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "packaging")

    summary = run_evals(cases)

    assert summary["failed"] == 0
    assert summary["passed"] == len(cases)
