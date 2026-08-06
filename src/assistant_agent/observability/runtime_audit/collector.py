"""Langfuse-first collection and deterministic audit checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from assistant_agent.observability.runtime_audit.models import (
    AuditCoverage,
    AuditFinding,
    LangfuseObservationSnapshot,
    LangfuseTraceSnapshot,
    LocalFallbackEvent,
    LocalTraceFallback,
    LocalTraceManifest,
    RuntimeAuditBundle,
)
from assistant_agent.observability.trace_store import TraceEvent, redact_trace_event
from assistant_agent.observability.runtime_audit.storage import format_audit_run_id
from assistant_agent.providers.provider_errors import sanitize_error_message


RESPONSE_QUALITY = "assistant_agent.quality.response_quality"
GROUNDING = "assistant_agent.quality.grounding"
TOOL_RESULT_QUALITY = "assistant_agent.quality.tool_result_quality"
MEMORY_EXTRACTION = "assistant_agent.quality.memory_extraction"
MEMORY_RECALL = "assistant_agent.quality.memory_recall"
TASK_CONFORMANCE = "assistant_agent.quality.task_conformance"
DAILY_SCORE_NAMES = frozenset(
    {RESPONSE_QUALITY, GROUNDING, TOOL_RESULT_QUALITY, MEMORY_EXTRACTION, MEMORY_RECALL}
)
DEFAULT_LOW_SCORE_THRESHOLD = 0.5
DEFAULT_FALLBACK_EVENT_LIMIT = 80


class LangfuseAuditSource(Protocol):
    def list_traces(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[LangfuseTraceSnapshot]: ...


def collect_runtime_audit(
    *,
    source: LangfuseAuditSource,
    local_trace_path: Path | None,
    window_start: datetime,
    window_end: datetime,
    collected_at: datetime | None = None,
    audit_run_id: str | None = None,
    judge_grace: timedelta = timedelta(minutes=15),
    low_score_threshold: float = DEFAULT_LOW_SCORE_THRESHOLD,
) -> RuntimeAuditBundle:
    """Collect one window without mutating Runtime, Langfuse, or memory."""

    collected_at = _utc(collected_at or datetime.now(timezone.utc))
    window_start = _utc(window_start)
    window_end = _utc(window_end)
    langfuse_available = True
    source_issues: list[AuditFinding] = []
    try:
        traces = sorted(
            (
                trace
                for trace in source.list_traces(
                    window_start=window_start,
                    window_end=window_end,
                )
                if window_start <= _utc(trace.timestamp) < window_end
            ),
            key=lambda item: (item.timestamp, item.trace_id),
        )
    except Exception as exc:
        langfuse_available = False
        traces = []
        source_issues.append(
            AuditFinding(
                code="langfuse_read_failed",
                category="infrastructure",
                severity="error",
                summary="Langfuse trace collection failed: " + sanitize_error_message(exc),
            )
        )
    local_events, local_issues, local_available = _read_local_events(
        local_trace_path,
        window_start=window_start,
        window_end=window_end,
    )
    local_by_trace: dict[str, list[TraceEvent]] = defaultdict(list)
    for event in local_events:
        local_by_trace[event.trace_id].append(event)
    turn_by_trace = {
        trace_id: events
        for trace_id, events in local_by_trace.items()
        if _is_assistant_turn(events)
    }
    manifests = [_manifest(events) for _, events in sorted(turn_by_trace.items())]
    remote_ids = {trace.trace_id for trace in traces}
    missing_ids = sorted(set(turn_by_trace) - remote_ids) if langfuse_available else []
    orphan_side_ids = (
        sorted(set(local_by_trace) - set(turn_by_trace) - remote_ids)
        if langfuse_available
        else []
    )
    findings = [*source_issues, *local_issues]
    fallbacks: list[LocalTraceFallback] = []
    for trace_id in missing_ids:
        events = turn_by_trace[trace_id]
        manifest = _manifest(events)
        fallbacks.append(_fallback(events, manifest=manifest))
        findings.append(
            AuditFinding(
                code="langfuse_export_missing",
                category="coverage",
                severity="warning",
                trace_id=trace_id,
                summary="Local terminal evidence exists but the Langfuse window has no matching trace.",
            )
        )
    for trace_id in orphan_side_ids:
        events = local_by_trace[trace_id]
        manifest = _manifest(events)
        fallbacks.append(_fallback(events, manifest=manifest))
        findings.append(
            AuditFinding(
                code="local_side_stream_unmatched",
                category="coverage",
                severity="info",
                trace_id=trace_id,
                summary="A local auxiliary lifecycle event is not an assistant.turn completeness record.",
            )
        )
    for trace in traces:
        findings.extend(
            _score_findings(
                trace,
                collected_at=collected_at,
                judge_grace=judge_grace,
                low_score_threshold=low_score_threshold,
            )
        )
        findings.extend(_observation_findings(trace))
    return RuntimeAuditBundle(
        audit_run_id=audit_run_id or format_audit_run_id(collected_at),
        collected_at=collected_at,
        window_start=window_start,
        window_end=window_end,
        coverage=AuditCoverage(
            langfuse_source_available=langfuse_available,
            langfuse_trace_count=len(traces),
            local_trace_count=len(turn_by_trace),
            matched_trace_count=len(set(turn_by_trace) & remote_ids),
            missing_export_count=len(missing_ids),
            local_source_available=local_available,
        ),
        traces=traces,
        local_manifests=manifests,
        local_fallbacks=fallbacks,
        findings=sorted(
            findings,
            key=lambda item: (
                item.trace_id or "",
                item.observation_id or "",
                item.score_name or "",
                item.code,
            ),
        ),
        production_mutation_allowed=False,
    )


def _read_local_events(
    path: Path | None,
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[TraceEvent], list[AuditFinding], bool]:
    if path is None:
        return [], [], False
    path = Path(path)
    if not path.exists():
        return (
            [],
            [
                AuditFinding(
                    code="local_completeness_source_missing",
                    category="infrastructure",
                    severity="info",
                    summary="The optional local completeness source does not exist.",
                )
            ],
            False,
        )
    events: list[TraceEvent] = []
    invalid = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = TraceEvent.model_validate_json(line)
            except Exception:
                invalid += 1
                continue
            created_at = _utc(event.created_at)
            if window_start <= created_at < window_end:
                events.append(event)
    issues = []
    if invalid:
        issues.append(
            AuditFinding(
                code="local_completeness_record_invalid",
                category="infrastructure",
                severity="warning",
                summary=f"Skipped {invalid} invalid local completeness record(s).",
            )
        )
    return events, issues, True


def _manifest(events: list[TraceEvent]) -> LocalTraceManifest:
    ordered = sorted(events, key=lambda item: item.created_at)
    terminal = next(
        (
            event.canonical_event
            for event in reversed(ordered)
            if event.canonical_event in {"run.completed", "run.failed", "run.cancelled"}
        ),
        None,
    )
    return LocalTraceManifest(
        trace_id=ordered[0].trace_id,
        run_id=ordered[0].run_id,
        first_event_at=ordered[0].created_at,
        last_event_at=ordered[-1].created_at,
        event_count=len(ordered),
        terminal_event=terminal,
    )


def _is_assistant_turn(events: list[TraceEvent]) -> bool:
    return any(
        event.canonical_event in {
            "run.started",
            "run.completed",
            "run.failed",
            "run.cancelled",
            "assistant.turn.summary",
        }
        for event in events
    )
def _fallback(events: list[TraceEvent], *, manifest: LocalTraceManifest) -> LocalTraceFallback:
    ordered = sorted(events, key=lambda item: item.created_at)[-DEFAULT_FALLBACK_EVENT_LIMIT:]
    timeline = []
    for event in ordered:
        safe = redact_trace_event(event)
        error = safe.error if isinstance(safe.error, dict) else {}
        timeline.append(
            LocalFallbackEvent(
                canonical_event=safe.canonical_event,
                event_type=safe.event_type,
                node_name=safe.node_name,
                status=safe.status,
                tool_name=safe.tool_name,
                error_code=safe.error_code or error.get("code"),
                created_at=safe.created_at,
            )
        )
    return LocalTraceFallback(
        trace_id=manifest.trace_id,
        run_id=manifest.run_id,
        event_count=manifest.event_count,
        terminal_event=manifest.terminal_event,
        timeline=timeline,
    )


def _score_findings(
    trace: LangfuseTraceSnapshot,
    *,
    collected_at: datetime,
    judge_grace: timedelta,
    low_score_threshold: float,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    requirements = _score_requirements(trace.observations)
    by_key: dict[tuple[str, str | None], list] = defaultdict(list)
    for score in trace.scores:
        if score.name in DAILY_SCORE_NAMES:
            by_key[(score.name, score.observation_id)].append(score)
    young = collected_at - trace.timestamp < judge_grace
    for score_name, observation_id in requirements:
        matches = by_key.get((score_name, observation_id), [])
        if not matches:
            findings.append(
                AuditFinding(
                    code="judge_pending" if young else "score_missing",
                    category="infrastructure",
                    severity="info" if young else "warning",
                    trace_id=trace.trace_id,
                    observation_id=observation_id,
                    score_name=score_name,
                    summary=(
                        "The native evaluator is still inside its completion grace period."
                        if young
                        else "The expected native evaluator Score is missing after the grace period."
                    ),
                    quality_failure=False,
                )
            )
            continue
        if len(matches) > 1:
            findings.append(
                AuditFinding(
                    code="score_duplicate",
                    category="infrastructure",
                    severity="warning",
                    trace_id=trace.trace_id,
                    observation_id=observation_id,
                    score_name=score_name,
                    summary="More than one Score exists for the same evaluator target.",
                    quality_failure=False,
                )
            )
        value = matches[-1].value
        if _is_low_score(value, threshold=low_score_threshold):
            findings.append(
                AuditFinding(
                    code="score_low",
                    category="quality",
                    severity="warning",
                    trace_id=trace.trace_id,
                    observation_id=observation_id,
                    score_name=score_name,
                    summary=f"The evaluator Score is below the configured threshold ({low_score_threshold:g}).",
                    quality_failure=True,
                )
            )
    return findings


def _score_requirements(
    observations: list[LangfuseObservationSnapshot],
) -> list[tuple[str, str | None]]:
    requirements: list[tuple[str, str | None]] = []
    final_generations = [
        item
        for item in observations
        if item.name == "llm.chat"
        and _observation_metadata_attr(item, "assistant_agent.runtime_action") == "text"
    ]
    target = final_generations[-1] if final_generations else None
    if target is None:
        target = next((item for item in observations if item.name == "agent.runtime"), None)
        if target is None:
            response_targets = [
                item
                for item in observations
                if item.name in {"response.final", "assistant.response"}
                or item.name.startswith("assistant.response")
            ]
            target = response_targets[-1] if response_targets else None
    response_id = target.observation_id if target is not None else None
    requirements.extend([(RESPONSE_QUALITY, response_id), (GROUNDING, response_id)])
    if target is not None and target.name == "llm.chat":
        requirements.append((MEMORY_RECALL, response_id))
    for observation in observations:
        name = observation.name.lower()
        is_span = observation.type.upper() != "EVENT"
        if _is_tool_execution_observation(observation):
            requirements.append((TOOL_RESULT_QUALITY, observation.observation_id))
        elif (
            is_span
            and name == "memory.turn_ingestion"
            and _memory_ingestion_has_change(observation)
            and _memory_extraction_evidence_present(observation)
        ):
            requirements.append((MEMORY_EXTRACTION, observation.observation_id))
    return list(dict.fromkeys(requirements))


def _observation_findings(trace: LangfuseTraceSnapshot) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for observation in trace.observations:
        name = observation.name.lower()
        is_span = observation.type.upper() != "EVENT"
        if (
            is_span
            and name == "memory.turn_ingestion"
            and _memory_ingestion_has_change(observation)
            and not _memory_extraction_evidence_present(observation)
        ):
            findings.append(
                AuditFinding(
                    code="memory_extraction_evidence_missing",
                    category="memory",
                    severity="info",
                    trace_id=trace.trace_id,
                    observation_id=observation.observation_id,
                    summary="Memory ingestion has counts/IDs but no extracted change text for semantic evaluation.",
                )
            )
        if (
            is_span
            and name == "memory.turn_ingestion"
            and not _memory_ingestion_has_change(observation)
        ):
            findings.append(
                AuditFinding(
                    code="memory_extraction_no_change",
                    category="memory",
                    severity="info",
                    trace_id=trace.trace_id,
                    observation_id=observation.observation_id,
                    summary="Memory ingestion completed without adding, updating, or deleting long-term memory.",
                )
            )
        if (
            is_span
            and name in {"memory.session_recall", "memory.recall"}
            and not _memory_recall_evidence_present(observation)
        ):
            findings.append(
                AuditFinding(
                    code="memory_recall_evidence_missing",
                    category="memory",
                    severity="info",
                    trace_id=trace.trace_id,
                    observation_id=observation.observation_id,
                    summary="Memory recall exposes only counts/status, so semantic recall quality is unsupported.",
                )
            )
        failed = (observation.level or "").upper() == "ERROR"
        if failed:
            category = (
                "memory"
                if observation.name.startswith("memory.")
                else "tool" if _is_tool_execution_observation(observation)
                else "infrastructure"
            )
            findings.append(
                AuditFinding(
                    code="observation_error",
                    category=category,
                    severity="warning",
                    trace_id=trace.trace_id,
                    observation_id=observation.observation_id,
                    summary=f"Langfuse observation {observation.name!r} is marked ERROR.",
                )
            )
    return findings


def _memory_extraction_evidence_present(observation: LangfuseObservationSnapshot) -> bool:
    return (
        isinstance(observation.input, dict)
        and isinstance(observation.input.get("messages"), list)
        and isinstance(observation.output, dict)
        and "changes" in observation.output
    )


def _memory_ingestion_has_change(observation: LangfuseObservationSnapshot) -> bool:
    if not isinstance(observation.output, dict):
        return True
    memory_count = observation.output.get("memory_count")
    if isinstance(memory_count, (int, float)):
        return memory_count > 0
    change_counts = observation.output.get("change_counts")
    if isinstance(change_counts, dict):
        return any(isinstance(value, (int, float)) and value > 0 for value in change_counts.values())
    changes = observation.output.get("changes")
    return not isinstance(changes, list) or bool(changes)


def _memory_recall_evidence_present(observation: LangfuseObservationSnapshot) -> bool:
    if not isinstance(observation.output, dict):
        return False
    return any(
        key in observation.output
        for key in ("memories", "recalled_memories", "memory_context", "items")
    )


def _observation_metadata_attr(
    observation: LangfuseObservationSnapshot,
    key: str,
) -> object:
    if not isinstance(observation.metadata, dict):
        return None
    attributes = observation.metadata.get("attributes")
    if isinstance(attributes, dict) and key in attributes:
        return attributes[key]
    return observation.metadata.get(key)


def _is_tool_execution_observation(
    observation: LangfuseObservationSnapshot,
) -> bool:
    if observation.type.upper() == "EVENT":
        return False
    if (
        _observation_metadata_attr(
            observation,
            "assistant_agent.observation_kind",
        )
        == "tool_execution"
    ):
        return True
    if _observation_metadata_attr(
        observation,
        "assistant_agent.canonical_event",
    ) in {"tool.finished", "tool.failed"}:
        return True
    name = observation.name.lower()
    return name in {"tool", "tool.execute"} or (
        name.startswith("tool.")
        and name not in {"tool.attempt.failed", "tool.retry.scheduled"}
    )


def _is_low_score(value: object, *, threshold: float) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return float(value) < threshold
    if isinstance(value, str):
        return value.strip().lower() in {"false", "fail", "failed", "poor", "bad"}
    return False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
