from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.web_search import WebSearchInput
from assistant_agent.services.web_search_adapter import create_web_search_adapter
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.web_search_tool import WebSearchTool
from tests.tool_smoke_metrics import build_tool_smoke_metrics, measure_tool_run


THIS_FILE = Path(__file__).resolve()


def _configured_web_search_tool() -> tuple[WebSearchTool, ProviderConfig]:
    _skip_unless_manual_tool_selected("RUN_REAL_WEB_SEARCH_TOOL_TEST")
    env = {
        **os.environ,
        "MULTIMODAL_AGENT_RUNTIME_PROFILE": os.environ.get(
            "MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke"
        ),
        "MULTIMODAL_AGENT_SEARCH_PROVIDER": os.environ.get(
            "MULTIMODAL_AGENT_SEARCH_PROVIDER", "http"
        ),
    }
    config = ProviderConfig.from_env(env)
    if config.runtime_profile.name not in {"provider_smoke", "pilot"}:
        pytest.skip("set MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or pilot")
    if config.search_provider != "http":
        pytest.skip("set MULTIMODAL_AGENT_SEARCH_PROVIDER=http")
    missing = []
    if not config.web_search_base_url:
        missing.append("WEB_SEARCH_BASE_URL")
    if not config.web_search_api_key:
        missing.append("WEB_SEARCH_API_KEY")
    if missing:
        pytest.skip(f"set {', '.join(missing)}")
    return WebSearchTool(adapter=create_web_search_adapter(config)), config


def test_real_web_search_tool_provider_smoke(capsys) -> None:
    tool, config = _configured_web_search_tool()

    result, tool_elapsed_ms = measure_tool_run(
        lambda: tool.run(
            WebSearchInput(
                query="OpenAI Realtime API latest changes",
                recency_days=30,
                limit=3,
            ),
            ToolContext(),
        )
    )

    with capsys.disabled():
        print()
        _print_json_section(
            "WEB SEARCH PROVIDER CONFIG",
            _provider_config_diagnostics(config),
        )
        _print_text_section(
            "WEB SEARCH RAW OUTPUT",
            None,
            empty="<raw provider payload is not exposed by this tool>",
        )
        _print_json_section("TOOL RESULT", result.model_dump(mode="json"))
        _print_json_section(
            "TOOL SMOKE METRICS",
            build_tool_smoke_metrics(result, tool_elapsed_ms=tool_elapsed_ms),
        )
        _print_json_section(
            "FILTERED TOOL RESULT (LLM-FACING)",
            _filtered_tool_result_for_llm(result),
        )
        _print_json_section("SUCCESS LAYERS", _success_layers(result))

    layers = _success_layers(result)
    assert layers["execution_success"] is True, layers
    assert layers["semantic_success"] is True, layers


def _success_layers(result) -> dict[str, object]:
    data = result.data if isinstance(result.data, dict) else {}
    errors = data.get("errors")
    data_errors = errors if isinstance(errors, list) else []
    results = data.get("results") if isinstance(data.get("results"), list) else []
    execution_success = result.success is True
    semantic_success = execution_success and bool(results) and not data_errors
    return {
        "execution_success": execution_success,
        "semantic_success": semantic_success,
        "tool_success": result.success,
        "contract_status": result.contract.status if result.contract else None,
        "query_used": data.get("query_used"),
        "result_count": len(results),
        "total": data.get("total"),
        "error": result.error,
        "data_errors": data_errors,
    }


def _provider_config_diagnostics(config: ProviderConfig) -> dict[str, object]:
    return {
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "runtime_profile": config.runtime_profile.name,
        "search_provider": config.search_provider,
        "base_url": config.web_search_base_url,
        "timeout_seconds": config.web_search_timeout_seconds,
        "credential_source": "WEB_SEARCH_API_KEY",
        "proxy_env_names": _proxy_env_names(),
    }


def _skip_unless_manual_tool_selected(env_var: str) -> None:
    if _manual_tool_selected(env_var):
        return
    pytest.skip(f"run this test file directly, or set {env_var}=1")


def _manual_tool_selected(env_var: str) -> bool:
    if os.environ.get(env_var) == "1":
        return True
    if os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT") != "1":
        return False
    selected_file = os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE")
    return bool(selected_file and Path(selected_file).resolve() == THIS_FILE)


def _filtered_tool_result_for_llm(result) -> dict[str, object]:
    return observation_from_tool_result(result).model_dump(mode="json")


def _print_json_section(title: str, value: object) -> None:
    print(f"=== {title} ===", flush=True)
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _print_text_section(title: str, text: str | None, *, empty: str) -> None:
    print(f"=== {title} ===", flush=True)
    print(text or empty, flush=True)


def _proxy_env_names() -> list[str]:
    return sorted(
        key
        for key in os.environ
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    )
