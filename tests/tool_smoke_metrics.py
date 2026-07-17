"""Shared metrics helpers for manual tool provider smoke tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from time import perf_counter
from typing import TypeVar

from assistant_agent.schemas.tools import ToolResult


TToolResult = TypeVar("TToolResult", bound=ToolResult)

TOOL_SMOKE_METRICS_SCHEMA_VERSION = "tool_smoke_metrics_v1"


def measure_tool_run(run: Callable[[], TToolResult]) -> tuple[TToolResult, int]:
    """Run one tool call and return its wall-clock elapsed time in milliseconds."""

    started_at = perf_counter()
    result = run()
    return result, _elapsed_ms(started_at)


def build_tool_smoke_metrics(
    result: ToolResult,
    *,
    tool_elapsed_ms: int,
    provider_diagnostics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the common JSON metrics block printed by manual tool smoke tests."""

    data = result.data if isinstance(result.data, dict) else {}
    trace_summary = (
        result.trace_summary if isinstance(result.trace_summary, dict) else {}
    )
    contract = result.contract
    contract_metadata = contract.metadata if contract is not None else {}
    diagnostics = provider_diagnostics or {}
    return {
        "schema_version": TOOL_SMOKE_METRICS_SCHEMA_VERSION,
        "tool_name": result.tool_name,
        "tool_elapsed_ms": max(0, int(tool_elapsed_ms)),
        "tool_elapsed_source": "perf_counter around tool.run",
        "reported_latency_ms": {
            "tool_result": _int_or_none(result.latency_ms),
            "data": _int_or_none(data.get("latency_ms")),
            "contract_metadata": _int_or_none(contract_metadata.get("latency_ms")),
            "trace_observation": _int_or_none(
                trace_summary.get("total_observation_latency_ms")
            ),
            "provider_first_delta": _int_or_none(
                diagnostics.get("first_delta_latency_ms")
            ),
            "provider_total_observation": _int_or_none(
                diagnostics.get("total_observation_latency_ms")
            ),
        },
        "result": {
            "tool_success": result.success,
            "contract_status": contract.status if contract is not None else None,
            "output_ref": result.output_ref,
            "error_present": bool(result.error),
        },
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int(round((perf_counter() - started_at) * 1000)))


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(round(value)))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None
