from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from assistant_agent.observability.runtime_audit.bundle_compaction import (
    compact_trace_evidence,
)
from assistant_agent.observability.runtime_audit.collector import (
    GROUNDING,
    MEMORY_EXTRACTION,
    MEMORY_RECALL,
    RESPONSE_QUALITY,
    TOOL_RESULT_QUALITY,
    collect_runtime_audit,
)
from assistant_agent.observability.runtime_audit.models import (
    AuditCoverage,
    LangfuseObservationSnapshot,
    LangfuseScoreSnapshot,
    LangfuseTraceSnapshot,
    RuntimeAuditBundle,
)
from assistant_agent.observability.runtime_audit.runner import _daily_codex_prompt
from assistant_agent.observability.runtime_audit.storage import (
    RuntimeAuditArtifactStore,
)


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _catalog(name: str = "probe") -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]


def _trace() -> LangfuseTraceSnapshot:
    catalog = _catalog()
    return LangfuseTraceSnapshot(
        trace_id="trace-1",
        timestamp=NOW,
        input={"tools": catalog},
        metadata={"trace-secret": "not-persisted"},
        observations=[
            LangfuseObservationSnapshot(
                observation_id="observation-1",
                name="llm.chat",
                type="GENERATION",
                input={"request": {"tools": catalog}},
                output={"tools": ["business-result"]},
                metadata={"assistant_agent.runtime_action": "text"},
            ),
            LangfuseObservationSnapshot(
                observation_id="observation-2",
                name="llm.chat",
                type="GENERATION",
                input={"tools": catalog},
                metadata={"attributes": {"provider": "probe"}},
            ),
        ],
        scores=[
            LangfuseScoreSnapshot(
                score_id="score-1",
                name="assistant_agent.quality.response_quality",
                value=True,
                metadata={"judge": "probe"},
            )
        ],
    )


def _coverage() -> AuditCoverage:
    return AuditCoverage(
        langfuse_trace_count=1,
        local_trace_count=0,
        matched_trace_count=0,
        missing_export_count=0,
        local_source_available=False,
    )


def test_compaction_removes_metadata_and_reuses_catalog_without_touching_output() -> None:
    original = _trace()

    compacted, catalogs = compact_trace_evidence([original])

    assert len(catalogs) == 1
    catalog_id = next(iter(catalogs))
    assert len(catalog_id) == 64
    assert catalog_id == catalog_id.lower()
    trace = compacted[0]
    first, second = trace.observations
    assert trace.metadata is None
    assert first.metadata is None
    assert trace.scores[0].metadata is None
    assert trace.input == {"tool_catalog_ref": catalog_id}
    assert first.input["request"] == {"tool_catalog_ref": catalog_id}
    assert second.input == {"tool_catalog_ref": catalog_id}
    assert first.output["tools"] == ["business-result"]
    assert original.metadata == {"trace-secret": "not-persisted"}
    assert "tools" in original.observations[0].input["request"]


def test_compaction_uses_one_digest_per_distinct_catalog() -> None:
    trace = _trace()
    trace.observations.extend(
        LangfuseObservationSnapshot(
            observation_id=f"observation-{index}",
            input={"tools": _catalog(name)},
        )
        for index, name in enumerate(("second", "third", "fourth"), start=3)
    )

    compacted, catalogs = compact_trace_evidence([trace])

    assert len(catalogs) == 4
    refs = {
        observation.input["tool_catalog_ref"]
        for observation in compacted[0].observations
        if isinstance(observation.input, dict) and "tool_catalog_ref" in observation.input
    }
    assert len(refs) == 4
    assert all(len(ref) == 64 for ref in refs)


def test_v1_bundle_remains_readable_with_raw_metadata() -> None:
    payload = {
        "schema_version": "assistant_agent_runtime_audit_bundle_v1",
        "audit_run_id": "legacy",
        "collected_at": NOW.isoformat(),
        "window_start": NOW.isoformat(),
        "window_end": NOW.isoformat(),
        "coverage": _coverage().model_dump(mode="json"),
        "traces": [_trace().model_dump(mode="json")],
        "production_mutation_allowed": False,
    }

    bundle = RuntimeAuditBundle.model_validate(payload)

    assert bundle.schema_version == "assistant_agent_runtime_audit_bundle_v1"
    assert bundle.traces[0].metadata == {"trace-secret": "not-persisted"}


def test_v2_bundle_rejects_dangling_tool_catalog_reference() -> None:
    trace = _trace().model_copy(
        update={"input": {"tool_catalog_ref": "a" * 64}, "metadata": None}
    )

    with pytest.raises(ValidationError, match="tool catalog reference"):
        RuntimeAuditBundle(
            audit_run_id="dangling",
            collected_at=NOW,
            window_start=NOW,
            window_end=NOW,
            coverage=_coverage(),
            traces=[trace],
            tool_catalogs={},
        )


class _Source:
    def list_traces(self, *, window_start: datetime, window_end: datetime):
        del window_start, window_end
        trace = _trace()
        trace.timestamp = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        trace.observations = [
            LangfuseObservationSnapshot(
                observation_id="final-text",
                name="llm.chat",
                type="GENERATION",
                input={"messages": [], "tools": _catalog()},
                output={"text": "ok"},
                metadata={"assistant_agent.runtime_action": "text"},
            ),
            LangfuseObservationSnapshot(
                observation_id="tool-execution",
                name="probe",
                type="SPAN",
                input={"arguments": {"query": "probe"}},
                output={"status": "success"},
                metadata={"assistant_agent.observation_kind": "tool_execution"},
            ),
            LangfuseObservationSnapshot(
                observation_id="memory-ingestion",
                name="memory.turn_ingestion",
                type="SPAN",
                input={"messages": [{"role": "user", "content": "probe"}]},
                output={"memory_count": 1, "changes": [{"event": "ADD"}]},
                metadata={
                    "assistant_agent.memory_semantic_evidence": "available"
                },
            ),
        ]
        trace.scores = []
        return [trace]


def _collected_bundle() -> RuntimeAuditBundle:
    return collect_runtime_audit(
        source=_Source(),
        local_trace_path=None,
        window_start=datetime(2026, 8, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 6, tzinfo=timezone.utc),
        collected_at=datetime(2026, 8, 6, 1, tzinfo=timezone.utc),
    )


def test_collector_classifies_metadata_before_persisted_compaction() -> None:
    bundle = _collected_bundle()

    score_names = {
        finding.score_name
        for finding in bundle.findings
        if finding.code == "score_missing"
    }
    assert score_names == {
        RESPONSE_QUALITY,
        GROUNDING,
        MEMORY_RECALL,
        TOOL_RESULT_QUALITY,
        MEMORY_EXTRACTION,
    }
    assert bundle.schema_version == "assistant_agent_runtime_audit_bundle_v2"
    assert bundle.tool_catalogs
    assert bundle.traces[0].metadata is None
    assert all(item.metadata is None for item in bundle.traces[0].observations)


def test_store_writes_compact_bundle_without_none_or_raw_metadata(
    tmp_path: Path,
) -> None:
    bundle = _collected_bundle()

    path = RuntimeAuditArtifactStore(tmp_path).write_bundle(bundle)

    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "\n  " not in text
    assert "metadata" not in payload["traces"][0]
    assert all("metadata" not in item for item in payload["traces"][0]["observations"])


def test_daily_prompt_explains_tool_catalog_references() -> None:
    prompt = _daily_codex_prompt(
        audit_date=date(2026, 8, 5),
        bundle_path=Path("/tmp/bundle.json"),
        issues_path=Path("/tmp/issues.json"),
    )

    assert "tool_catalog_ref" in prompt
    assert "tool_catalogs" in prompt


def test_synthetic_daily_catalogs_compact_below_forty_percent() -> None:
    names = ["first"] * 30 + ["second"] * 14 + ["third"] * 3 + ["fourth"] * 2
    trace = LangfuseTraceSnapshot(
        trace_id="trace-volume",
        timestamp=NOW,
        metadata={"resourceAttributes": {"service": "assistant-agent"}},
        observations=[
            LangfuseObservationSnapshot(
                observation_id=f"observation-{index}",
                name="llm.chat",
                type="GENERATION",
                input={"messages": [], "tools": _catalog(name)},
                output={"text": "ok"},
                metadata={
                    "attributes": {
                        "assistant_agent.runtime_action": "text",
                        "resource": "repeated-transport-metadata",
                    }
                },
            )
            for index, name in enumerate(names)
        ],
    )
    raw_bundle = RuntimeAuditBundle(
        schema_version="assistant_agent_runtime_audit_bundle_v1",
        audit_run_id="raw-volume",
        collected_at=NOW,
        window_start=NOW,
        window_end=NOW,
        coverage=_coverage(),
        traces=[trace],
    )
    compacted, catalogs = compact_trace_evidence([trace])
    compact_bundle = RuntimeAuditBundle(
        audit_run_id="compact-volume",
        collected_at=NOW,
        window_start=NOW,
        window_end=NOW,
        coverage=_coverage(),
        traces=compacted,
        tool_catalogs=catalogs,
    )

    pretty_raw_size = len(raw_bundle.model_dump_json(indent=2).encode("utf-8"))
    compact_size = len(
        compact_bundle.model_dump_json(exclude_none=True).encode("utf-8")
    )

    assert len(compact_bundle.tool_catalogs) == 4
    assert compact_size < pretty_raw_size * 0.40
