import json
from pathlib import Path

import pytest

from assistant_agent.services.improvement.evidence import (
    EvidenceLoadError,
    collect_trajectory_evidence,
    deduplicate_evidence,
    load_structured_evidence,
)
from assistant_agent.services.trace_store import TraceEvent
from assistant_agent.services.trajectory_debug import build_redacted_trajectory_replay


def test_collects_stable_context_overflow_evidence_from_redacted_replay() -> None:
    event = TraceEvent(
        trace_id="trace_1",
        run_id="run_1",
        node_name="native_runtime",
        event_type="observability",
        canonical_event="llm.chat.finished",
        status="failed",
        error_code="provider_context_overflow",
    )
    replay = build_redacted_trajectory_replay([event])

    first = collect_trajectory_evidence(replay)
    second = collect_trajectory_evidence(replay)

    assert len(first) == 1
    assert first[0].evidence_id == second[0].evidence_id
    assert first[0].symptom_code == "provider_context_overflow_repeatedly"
    assert first[0].target_hints[0].target_ref == "context_budget"
    assert "raw" not in first[0].model_dump_json().lower()


def test_rejects_non_redacted_trajectory_before_collection() -> None:
    replay = build_redacted_trajectory_replay([]).model_copy(update={"raw_data_included": True})

    with pytest.raises(EvidenceLoadError, match="redacted"):
        collect_trajectory_evidence(replay)


def test_loads_prompt_safe_skill_eval_failure(tmp_path: Path) -> None:
    path = tmp_path / "eval.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "improvement_source_records_v1",
                "records": [
                    {
                        "source_ref": "eval:search:case_1",
                        "component": "skills/realtime_web_search/SKILL.md",
                        "target_type": "skill",
                        "target_ref": "realtime_web_search",
                        "symptom_code": "skill_tool_not_selected_in_eval",
                        "summary": "Governed search tool was not selected in the offline eval.",
                        "severity": "high",
                        "attributes": {
                            "rubric_code": "expected_tool_missing",
                            "score": 0.0,
                            "threshold": 1.0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = load_structured_evidence(path, source_type="eval_failure")

    assert len(items) == 1
    assert items[0].target_hints[0].target_type == "skill"
    assert items[0].redacted is True


def test_rejects_unsafe_structured_evidence(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "improvement_source_records_v1",
                "records": [
                    {
                        "source_ref": "eval:unsafe",
                        "target_type": "runtime",
                        "target_ref": "assistant_loop",
                        "symptom_code": "eval_rubric_regression",
                        "summary": "Bearer secret-token",
                        "attributes": {"raw_provider_response": "private body"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceLoadError, match="unsafe"):
        load_structured_evidence(path, source_type="eval_failure")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_ref", "Bearer private-token"),
        ("component", "data:text/html,<script>private</script>"),
        ("summary", "raw system message: private"),
    ],
)
def test_rejects_unsafe_top_level_fields(tmp_path: Path, field: str, value: str) -> None:
    record = {
        "source_ref": "eval:safe",
        "component": "assistant_loop",
        "target_type": "runtime",
        "target_ref": "assistant_loop",
        "symptom_code": "eval_rubric_regression",
        "summary": "A safe bounded summary.",
        "attributes": {"rubric_code": "runtime_gate", "score": 0.0},
    }
    record[field] = value
    path = tmp_path / "unsafe-top-level.json"
    path.write_text(
        json.dumps({"schema_version": "improvement_source_records_v1", "records": [record]}),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceLoadError, match="unsafe"):
        load_structured_evidence(path, source_type="eval_failure")


@pytest.mark.parametrize("unsafe_key", ["command_output", "system_message", "memory_item", "raw_html"])
def test_rejects_non_allowlisted_attribute_keys(tmp_path: Path, unsafe_key: str) -> None:
    path = tmp_path / "unsafe-attribute.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "improvement_source_records_v1",
                "records": [
                    {
                        "source_ref": "eval:unsafe-attribute",
                        "target_type": "runtime",
                        "target_ref": "assistant_loop",
                        "symptom_code": "eval_rubric_regression",
                        "summary": "A bounded summary.",
                        "attributes": {unsafe_key: "private content"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceLoadError, match="unsafe"):
        load_structured_evidence(path, source_type="eval_failure")


def test_rejects_unknown_symptom_code(tmp_path: Path) -> None:
    path = tmp_path / "unknown-symptom.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "improvement_source_records_v1",
                "records": [
                    {
                        "source_ref": "eval:unknown",
                        "target_type": "runtime",
                        "target_ref": "assistant_loop",
                        "symptom_code": "invented_by_model",
                        "summary": "A bounded summary.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceLoadError, match="symptom"):
        load_structured_evidence(path, source_type="eval_failure")


def test_two_matching_events_from_one_trace_count_as_one_source() -> None:
    events = [
        TraceEvent(
            trace_id="trace_same",
            run_id="run_same",
            node_name="assistant_loop",
            event_type="loop_guard_triggered",
            status="assistant_loop_limit_reached",
            error_code="assistant_loop_limit_reached",
        )
        for _ in range(2)
    ]

    items = collect_trajectory_evidence(build_redacted_trajectory_replay(events))

    assert {item.source_ref for item in items} == {"trajectory:trace_same"}


@pytest.mark.parametrize(
    "secret_value",
    [
        "api_key=plainsecretvalue",
        "password: hunter2",
        "secret_token=qwerty",
        "OPENAI_API_KEY=topsecretvalue",
        "ANTHROPIC_API_KEY=topsecretvalue",
        "AWS_SECRET_ACCESS_KEY=topsecretvalue",
    ],
)
def test_rejects_secret_assignments_in_visible_fields(
    tmp_path: Path,
    secret_value: str,
) -> None:
    path = tmp_path / "secret.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "improvement_source_records_v1",
                "records": [
                    {
                        "source_ref": "eval:secret",
                        "target_type": "runtime",
                        "target_ref": "assistant_loop",
                        "symptom_code": "eval_rubric_regression",
                        "summary": secret_value,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceLoadError, match="unsafe"):
        load_structured_evidence(path, source_type="eval_failure")


def test_normalizes_naive_structured_timestamp_to_utc(tmp_path: Path) -> None:
    path = tmp_path / "timestamp.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "improvement_source_records_v1",
                "records": [
                    {
                        "source_ref": "eval:timestamp",
                        "target_type": "runtime",
                        "target_ref": "assistant_loop",
                        "symptom_code": "eval_rubric_regression",
                        "summary": "A bounded failure summary.",
                        "occurred_at": "2026-07-14T12:00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    item = load_structured_evidence(path, source_type="eval_failure")[0]

    assert item.occurred_at is not None
    assert item.occurred_at.utcoffset() is not None


def test_deduplicates_evidence_by_stable_id(tmp_path: Path) -> None:
    path = tmp_path / "test.json"
    record = {
        "source_ref": "pytest:test_loop",
        "target_type": "code",
        "target_ref": "assistant_agent.agent.runtime:AgentGraphRuntime",
        "symptom_code": "deterministic_test_regression",
        "summary": "The deterministic loop test failed.",
        "severity": "high",
        "attributes": {"module": "assistant_agent.agent.runtime", "symbol": "AgentGraphRuntime"},
    }
    path.write_text(
        json.dumps({"schema_version": "improvement_source_records_v1", "records": [record, record]}),
        encoding="utf-8",
    )

    items = load_structured_evidence(path, source_type="test_failure")

    assert len(deduplicate_evidence(items)) == 1
