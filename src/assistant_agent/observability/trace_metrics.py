"""Aggregate local observability metrics from redacted trace events."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from statistics import median
from typing import Any

from assistant_agent.observability.trace_store import TraceEvent


TERMINAL_RUN_EVENTS = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}
TERMINAL_TOOL_EVENTS = {"tool.finished", "tool.failed"}
OBSERVATION_TOOL_EVENTS = {"tool.observation"}


def load_trace_events(path: Path | str) -> list[TraceEvent]:
    """Load trace events from the canonical full-content JSONL trace store."""

    trace_path = Path(path)
    if not trace_path.exists():
        return []
    events: list[TraceEvent] = []
    with trace_path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                events.append(TraceEvent.model_validate_json(line))
    return events


def filter_trace_events(
    events: list[TraceEvent],
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> list[TraceEvent]:
    """Filter trace events by stable low-cardinality identity fields."""

    return [
        event
        for event in events
        if (user_id is None or event.user_id == user_id) and (session_id is None or event.session_id == session_id)
    ]


def build_trace_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    """Build prompt-safe metrics from redacted trace events."""

    return {
        "event_count": len(events),
        "trace_count": len({event.trace_id for event in events}),
        "run": _run_metrics(events),
        "errors": _error_metrics(events),
        "tools": _tool_metrics(events),
        "llm": _llm_metrics(events),
        "context": _context_metrics(events),
        "gateway": _gateway_metrics(events),
        "memory": _memory_metrics(events),
    }


def _run_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    by_run: dict[str, list[TraceEvent]] = defaultdict(list)
    for event in events:
        by_run[event.run_id].append(event)

    statuses = Counter(_run_status(run_events) for run_events in by_run.values())
    durations = [_duration_ms(run_events) for run_events in by_run.values() if len(run_events) >= 2]
    total = len(by_run)
    return {
        "count": total,
        "completed": statuses["completed"],
        "failed": statuses["failed"],
        "cancelled": statuses["cancelled"],
        "unknown": statuses["unknown"],
        "success_rate": _rate(statuses["completed"], total),
        "failure_rate": _rate(statuses["failed"], total),
        "cancel_rate": _rate(statuses["cancelled"], total),
        "duration_ms": _numeric_summary(durations),
    }


def _run_status(events: list[TraceEvent]) -> str:
    for event in reversed(events):
        status = TERMINAL_RUN_EVENTS.get(event.canonical_event or "")
        if status is not None:
            return status
    if any(_has_error(event) for event in events):
        return "failed"
    return "unknown"


def _duration_ms(events: list[TraceEvent]) -> int:
    started_at = min(event.created_at for event in events)
    finished_at = max(event.created_at for event in events)
    return max(0, round((finished_at - started_at).total_seconds() * 1000))


def _error_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    codes = Counter(_error_code(event) or "unknown_error" for event in events if _has_error(event))
    return {
        "count": sum(codes.values()),
        "by_code": _counter_dict(codes),
    }


def _tool_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    tool_data: dict[str, dict[str, Any]] = defaultdict(_empty_tool_data)
    for event in _tool_metric_events(events):
        if not event.tool_name:
            continue
        data = tool_data[event.tool_name]
        data["call_count"] += 1
        if _has_error(event) or event.status == "failed":
            data["failure_count"] += 1
        if event.latency_ms is not None:
            data["latencies"].append(event.latency_ms)
        data["retry_count"] += _event_int(event, "retry_count")
        category = _event_str(event, "tool_category")
        if category:
            data["categories"][category] += 1

    by_tool: dict[str, Any] = {}
    for tool_name, data in sorted(tool_data.items()):
        call_count = data["call_count"]
        by_tool[tool_name] = {
            "call_count": call_count,
            "failure_count": data["failure_count"],
            "failure_rate": _rate(data["failure_count"], call_count),
            "retry_count": data["retry_count"],
            "categories": _counter_dict(data["categories"]),
            "latency_ms": _numeric_summary(data["latencies"]),
        }
    return {
        "total_calls": sum(item["call_count"] for item in by_tool.values()),
        "by_tool": by_tool,
    }


def _tool_metric_events(events: list[TraceEvent]) -> list[TraceEvent]:
    """Return one countable tool event per tool call.

    Terminal lifecycle events are preferred. Observation-only events remain
    countable for old traces that predate canonical tool terminal events.
    """

    terminal_events: list[TraceEvent] = []
    observation_events: list[TraceEvent] = []
    terminal_call_keys: set[tuple[str, str]] = set()
    terminal_run_tools: set[tuple[str, str]] = set()

    for event in events:
        if not event.tool_name:
            continue
        if _is_terminal_tool_event(event):
            terminal_events.append(event)
            call_id = _tool_call_id(event)
            if call_id:
                terminal_call_keys.add((event.run_id, call_id))
            terminal_run_tools.add((event.run_id, event.tool_name))
        elif _is_observation_tool_event(event):
            observation_events.append(event)

    selected = list(terminal_events)
    for event in observation_events:
        call_id = _tool_call_id(event)
        if call_id and (event.run_id, call_id) in terminal_call_keys:
            continue
        if (event.run_id, event.tool_name or "") in terminal_run_tools:
            continue
        selected.append(event)
    return selected


def _empty_tool_data() -> dict[str, Any]:
    return {
        "call_count": 0,
        "failure_count": 0,
        "retry_count": 0,
        "categories": Counter(),
        "latencies": [],
    }


def _is_terminal_tool_event(event: TraceEvent) -> bool:
    return event.canonical_event in TERMINAL_TOOL_EVENTS or event.event_type == "tool_failed"


def _is_observation_tool_event(event: TraceEvent) -> bool:
    return event.canonical_event in OBSERVATION_TOOL_EVENTS or event.event_type == "tool_observation"


def _tool_call_id(event: TraceEvent) -> str | None:
    for mapping in (event.attributes, event.output_summary, event.input_summary, event.error or {}):
        if isinstance(mapping, dict):
            for key in ("tool_call_id", "call_id"):
                value = mapping.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _llm_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    llm_events = [event for event in events if event.canonical_event == "llm.chat.finished"]
    provider_counts = Counter(event.provider for event in llm_events if event.provider)
    model_counts = Counter(event.model for event in llm_events if event.model)
    total_tokens = sum(_token_count(event) for event in llm_events)
    direct_answer_count = 0
    native_tool_call_count = 0
    for event in llm_events:
        result_kind = _event_str(event, "result_kind") or _event_str(event, "response_kind")
        if result_kind in {"text", "content", "direct_answer", "final_answer"}:
            direct_answer_count += 1
        if result_kind in {"tool_call", "tool_calls", "native_tool_calls"} or _event_int(
            event,
            "native_tool_call_count",
        ):
            native_tool_call_count += 1
    return {
        "call_count": len(llm_events),
        "error_count": sum(1 for event in llm_events if _has_error(event) or event.status == "failed"),
        "provider_counts": _counter_dict(provider_counts),
        "model_counts": _counter_dict(model_counts),
        "latency_ms": _numeric_summary([event.latency_ms for event in llm_events if event.latency_ms is not None]),
        "total_tokens": total_tokens,
        "direct_answer_count": direct_answer_count,
        "native_tool_call_count": native_tool_call_count,
    }


def _context_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    ratios: list[float] = []
    total_tokens = 0
    compaction_triggered_count = 0
    overflow_retry_count = 0
    sample_count = 0
    for event in events:
        sample = _context_sample(event)
        if not sample:
            continue
        sample_count += 1
        ratio = sample.get("ratio")
        if isinstance(ratio, (int, float)):
            ratios.append(float(ratio))
        tokens = sample.get("total_tokens")
        if isinstance(tokens, int):
            total_tokens += tokens
        if sample.get("compaction_triggered") is True:
            compaction_triggered_count += 1
        overflow_retry_count += int(sample.get("overflow_retry_count") or 0)
    return {
        "sample_count": sample_count,
        "average_budget_ratio": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        "max_budget_ratio": round(max(ratios), 4) if ratios else 0.0,
        "compaction_triggered_count": compaction_triggered_count,
        "overflow_retry_count": overflow_retry_count,
        "total_tokens": total_tokens,
    }


def _context_sample(event: TraceEvent) -> dict[str, Any]:
    budget = _mapping_path(event.output_summary, ("context", "budget"))
    if not budget:
        budget = _mapping_path(event.output_summary, ("budget",))
    attributes = event.attributes if isinstance(event.attributes, dict) else {}
    has_context_budget = bool(budget) or (event.canonical_event or "").startswith("context.build")
    has_context_budget = has_context_budget or any(
        key in attributes
        for key in (
            "context_usage_ratio",
            "budget_ratio",
            "max_tokens",
            "compaction_triggered",
            "overflow_retry_count",
        )
    )
    if not has_context_budget:
        return {}

    ratio = _number_from_mapping(attributes, "context_usage_ratio")
    if ratio is None:
        ratio = _number_from_mapping(attributes, "budget_ratio")
    if ratio is None:
        ratio = _number_from_mapping(budget, "context_usage_ratio")
    if ratio is None:
        ratio = _number_from_mapping(budget, "budget_ratio")

    total_tokens = _int_from_mapping(attributes, "total_tokens")
    if total_tokens is None:
        total_tokens = _int_from_mapping(budget, "total_tokens")
    max_tokens = _int_from_mapping(attributes, "max_tokens")
    if max_tokens is None:
        max_tokens = _int_from_mapping(budget, "max_tokens")
    if ratio is None and total_tokens is not None and max_tokens:
        ratio = total_tokens / max_tokens

    compaction_triggered = _bool_from_mapping(attributes, "compaction_triggered")
    if compaction_triggered is None:
        compaction_triggered = _bool_from_mapping(budget, "compaction_triggered")
    overflow_retry_count = _int_from_mapping(attributes, "overflow_retry_count")
    if overflow_retry_count is None:
        overflow_retry_count = _int_from_mapping(budget, "overflow_retry_count")

    if ratio is None and total_tokens is None and compaction_triggered is None and overflow_retry_count is None:
        return {}
    return {
        "ratio": ratio,
        "total_tokens": total_tokens,
        "compaction_triggered": compaction_triggered,
        "overflow_retry_count": overflow_retry_count or 0,
    }


def _gateway_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    cancel_sources: Counter[str] = Counter()
    interrupt_count = 0
    deadline_expired_count = 0
    for event in events:
        canonical = event.canonical_event or ""
        error_code = _error_code(event)
        if canonical == "run.cancelled" or canonical.endswith(".cancelled"):
            cancel_sources[_event_str(event, "cancel_source") or "unknown"] += 1
        if "interrupt" in canonical or _event_bool(event, "interrupt") is True:
            interrupt_count += 1
        if error_code == "deadline_expired" or _event_str(event, "cancel_source") == "deadline_expired":
            deadline_expired_count += 1
    return {
        "cancel_count": sum(cancel_sources.values()),
        "cancel_sources": _counter_dict(cancel_sources),
        "interrupt_count": interrupt_count,
        "deadline_expired_count": deadline_expired_count,
    }


def _memory_metrics(events: list[TraceEvent]) -> dict[str, Any]:
    recalls = [
        event
        for event in events
        if event.canonical_event
        in {"memory.recall.finished", "memory.session_recall.finished"}
    ]
    commits = [
        event
        for event in events
        if event.canonical_event
        in {"memory.commit.finished", "memory.ingestion.finished"}
    ]
    recall_failures = sum(
        event.status not in {"ready", "empty", "succeeded"} for event in recalls
    )
    commit_failures = sum(
        event.status not in {"succeeded", "skipped"} for event in commits
    )
    return {
        "recall_count": len(recalls),
        "recall_failure_count": recall_failures,
        "commit_count": len(commits),
        "commit_failure_count": commit_failures,
        # Compatibility keys for existing metrics consumers.
        "session_recall_count": len(recalls),
        "session_recall_failure_count": recall_failures,
        "ingestion_count": len(commits),
        "ingestion_failure_count": commit_failures,
    }


def _numeric_summary(values: Iterable[int | float]) -> dict[str, Any]:
    numeric_values = [value for value in values if isinstance(value, (int, float))]
    if not numeric_values:
        return {"count": 0, "avg": 0.0, "p50": 0, "p95": 0, "max": 0}
    sorted_values = sorted(numeric_values)
    return {
        "count": len(sorted_values),
        "avg": round(sum(sorted_values) / len(sorted_values), 2),
        "p50": median(sorted_values),
        "p95": _nearest_percentile(sorted_values, 95),
        "max": max(sorted_values),
    }


def _nearest_percentile(sorted_values: list[int | float], percentile: int) -> int | float:
    if not sorted_values:
        return 0
    rank = max(1, round((percentile / 100) * len(sorted_values)))
    return sorted_values[min(rank - 1, len(sorted_values) - 1)]


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _counter_dict(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): counter[key] for key in sorted(counter)}


def _has_error(event: TraceEvent) -> bool:
    return bool(_error_code(event) or _error_message(event))


def _error_code(event: TraceEvent) -> str | None:
    if event.error_code:
        return event.error_code
    if isinstance(event.error, dict):
        value = event.error.get("code")
        return value if isinstance(value, str) and value else None
    return None


def _error_message(event: TraceEvent) -> str | None:
    if isinstance(event.error, dict):
        value = event.error.get("message")
        return value if isinstance(value, str) and value else None
    return None


def _token_count(event: TraceEvent) -> int:
    for key in ("total_tokens", "token_count", "tokens"):
        value = _event_int(event, key)
        if value:
            return value
    usage = _mapping_path(event.output_summary, ("usage",))
    value = _int_from_mapping(usage, "total_tokens")
    return value or 0


def _event_int(event: TraceEvent, key: str) -> int:
    for mapping in (event.attributes, event.output_summary, event.input_summary, event.error or {}):
        value = _int_from_mapping(mapping, key)
        if value is not None:
            return value
    return 0


def _event_bool(event: TraceEvent, key: str) -> bool | None:
    for mapping in (event.attributes, event.output_summary, event.input_summary):
        value = _bool_from_mapping(mapping, key)
        if value is not None:
            return value
    return None


def _event_str(event: TraceEvent, key: str) -> str | None:
    for mapping in (event.attributes, event.output_summary, event.input_summary):
        if isinstance(mapping, dict):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _number_from_mapping(mapping: Any, key: str) -> float | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int_from_mapping(mapping: Any, key: str) -> int | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _bool_from_mapping(mapping: Any, key: str) -> bool | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    return value if isinstance(value, bool) else None


def _mapping_path(mapping: Any, path: tuple[str, ...]) -> dict[str, Any]:
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}
