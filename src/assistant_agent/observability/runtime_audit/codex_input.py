"""Build a bounded evidence index for the daily Codex audit."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any

from assistant_agent.observability.runtime_audit.models import RuntimeAuditBundle


DEFAULT_DAILY_CODEX_INPUT_MAX_BYTES = 350_000
_DETAIL_VALUE_MAX_BYTES = 24_000
_INDEX_VALUE_MAX_BYTES = 4_000
_NON_ANOMALY_FINDING_CODES = frozenset(
    {"judge_pending", "memory_extraction_no_change"}
)


def anomaly_findings(bundle: RuntimeAuditBundle):
    """Return deterministic findings that warrant a Codex audit."""

    return [
        finding
        for finding in bundle.findings
        if finding.code not in _NON_ANOMALY_FINDING_CODES
    ]


def requires_codex_audit(bundle: RuntimeAuditBundle) -> bool:
    """Whether the anomaly-only third layer requires semantic review."""

    return bool(anomaly_findings(bundle))


def build_daily_codex_input(
    bundle: RuntimeAuditBundle,
    *,
    max_bytes: int = DEFAULT_DAILY_CODEX_INPUT_MAX_BYTES,
) -> dict[str, Any]:
    """Build a self-contained third layer containing anomalous traces only."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    anomalies = anomaly_findings(bundle)
    evidence_trace_ids = {
        finding.trace_id for finding in anomalies if finding.trace_id
    }
    missing_export_ids = {
        finding.trace_id
        for finding in anomalies
        if finding.code == "langfuse_export_missing" and finding.trace_id
    }
    trace_index = []
    evidence_traces = []
    referenced_catalogs: set[str] = set()
    for trace in bundle.traces:
        if trace.trace_id not in evidence_trace_ids:
            continue
        observation_names = Counter(item.name or item.type for item in trace.observations)
        index_entry = {
            "trace_id": trace.trace_id,
            "name": trace.name,
            "timestamp": trace.timestamp.isoformat(),
            "session_id": trace.session_id,
            "environment": trace.environment,
            "input": _bounded_value(trace.input, _INDEX_VALUE_MAX_BYTES),
            "output": _bounded_value(trace.output, _INDEX_VALUE_MAX_BYTES),
            "observation_count": len(trace.observations),
            "observation_name_counts": dict(sorted(observation_names.items())),
            "error_observation_ids": [
                item.observation_id
                for item in trace.observations
                if (item.level or "").upper() == "ERROR" or item.status_message
            ],
            "scores": [_score_payload(item) for item in trace.scores],
        }
        trace_index.append(index_entry)
        referenced_catalogs.update(_tool_catalog_refs(index_entry["input"]))
        referenced_catalogs.update(_tool_catalog_refs(index_entry["output"]))
        # The third layer is the complete bounded context for an anomalous trace.
        # Codex must not need the all-trace archive to understand its sequence.
        observations = [_observation_payload(item) for item in trace.observations]
        for observation in observations:
            referenced_catalogs.update(_tool_catalog_refs(observation.get("input")))
        evidence_traces.append(
            {
                "trace_id": trace.trace_id,
                "observations": observations,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": "assistant_agent_daily_codex_input_v1",
        "audit_run_id": bundle.audit_run_id,
        "source_bundle_sha256": hashlib.sha256(
            bundle.model_dump_json(exclude_none=True).encode("utf-8")
        ).hexdigest(),
        "window_start": bundle.window_start.isoformat(),
        "window_end": bundle.window_end.isoformat(),
        "coverage": bundle.coverage.model_dump(mode="json"),
        "audit_gate": {
            "requires_codex": bool(anomalies),
            "anomaly_trace_count": len(evidence_trace_ids),
            "anomaly_finding_count": len(anomalies),
            "finding_codes": sorted({item.code for item in anomalies}),
        },
        "local_auxiliary_summary": bundle.local_auxiliary_summary.model_dump(
            mode="json"
        ),
        "findings": [item.model_dump(mode="json", exclude_none=True) for item in anomalies],
        "trace_index": trace_index,
        "evidence_traces": evidence_traces,
        "local_fallbacks": [
            item.model_dump(mode="json", exclude_none=True)
            for item in bundle.local_fallbacks
            if item.trace_id in missing_export_ids
        ],
        "tool_catalogs": {
            key: value
            for key, value in bundle.tool_catalogs.items()
            if key in referenced_catalogs
        },
        "production_mutation_allowed": False,
    }
    _fit_budget(payload, max_bytes=max_bytes)
    return payload


def _score_payload(score) -> dict[str, Any]:
    return score.model_dump(mode="json", exclude={"metadata"}, exclude_none=True)


def _observation_payload(observation) -> dict[str, Any]:
    value = observation.model_dump(mode="json", exclude={"metadata"}, exclude_none=True)
    for key in ("input", "output"):
        if key in value:
            value[key] = _bounded_value(value[key], _DETAIL_VALUE_MAX_BYTES)
    return value


def _bounded_value(value: Any, max_bytes: int) -> Any:
    if value is None:
        return None
    encoded = _json_bytes(value)
    if len(encoded) <= max_bytes:
        return value
    return {
        "evidence_omitted": True,
        "reason": "codex_input_field_budget",
        "serialized_bytes": len(encoded),
    }


def _fit_budget(payload: dict[str, Any], *, max_bytes: int) -> None:
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for trace in payload["evidence_traces"]:
        for observation in trace["observations"]:
            for key in ("input", "output"):
                if key in observation and not _is_omission(observation[key]):
                    candidates.append((len(_json_bytes(observation[key])), observation, key))
    for trace in payload["trace_index"]:
        for key in ("input", "output"):
            if trace.get(key) is not None and not _is_omission(trace[key]):
                candidates.append((len(_json_bytes(trace[key])), trace, key))
    for size, owner, key in sorted(candidates, key=lambda item: item[0], reverse=True):
        if len(_json_bytes(payload)) <= max_bytes:
            return
        owner[key] = {
            "evidence_omitted": True,
            "reason": "codex_input_total_budget",
            "serialized_bytes": size,
        }
    if len(_json_bytes(payload)) > max_bytes:
        raise ValueError("daily Codex audit input exceeds its structural byte budget")


def _is_omission(value: Any) -> bool:
    return isinstance(value, dict) and value.get("evidence_omitted") is True


def _tool_catalog_refs(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("tool_catalog_ref")
        if isinstance(ref, str):
            result.add(ref)
        for child in value.values():
            result.update(_tool_catalog_refs(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_tool_catalog_refs(child))
    return result


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
