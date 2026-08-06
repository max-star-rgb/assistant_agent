from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from assistant_agent.observability.runtime_audit.bundle_compaction import (
    compact_trace_evidence,
)
from assistant_agent.observability.runtime_audit.models import (
    AuditCoverage,
    LangfuseObservationSnapshot,
    LangfuseScoreSnapshot,
    LangfuseTraceSnapshot,
    RuntimeAuditBundle,
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
