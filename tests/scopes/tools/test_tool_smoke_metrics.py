from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tools import ToolResult
from tests.tool_smoke_metrics import (
    TOOL_SMOKE_METRICS_SCHEMA_VERSION,
    build_tool_smoke_metrics,
    measure_tool_run,
)


def test_build_tool_smoke_metrics_keeps_wall_clock_as_authoritative() -> None:
    contract = build_capability_output_contract(
        capability="shopping_search",
        status="succeeded",
        output_ref="provider://shopping/result",
        metadata={"latency_ms": 42},
    )
    result = ToolResult(
        tool_name="shopping_search",
        success=True,
        data={"latency_ms": 17},
        output_ref="provider://shopping/result",
        latency_ms=None,
        contract=contract,
        trace_summary={"total_observation_latency_ms": 9},
    )

    metrics = build_tool_smoke_metrics(
        result,
        tool_elapsed_ms=123,
        provider_diagnostics={
            "first_delta_latency_ms": 5,
            "total_observation_latency_ms": 11,
        },
    )

    assert metrics["schema_version"] == TOOL_SMOKE_METRICS_SCHEMA_VERSION
    assert metrics["tool_name"] == "shopping_search"
    assert metrics["tool_elapsed_ms"] == 123
    assert metrics["tool_elapsed_source"] == "perf_counter around tool.run"
    assert metrics["reported_latency_ms"] == {
        "tool_result": None,
        "data": 17,
        "contract_metadata": 42,
        "trace_observation": 9,
        "provider_first_delta": 5,
        "provider_total_observation": 11,
    }
    assert metrics["result"] == {
        "tool_success": True,
        "contract_status": "succeeded",
        "output_ref": "provider://shopping/result",
        "error_present": False,
    }


def test_measure_tool_run_returns_result_and_elapsed_ms() -> None:
    result, elapsed_ms = measure_tool_run(
        lambda: ToolResult(tool_name="mock_tool", success=True)
    )

    assert result.tool_name == "mock_tool"
    assert elapsed_ms >= 0
