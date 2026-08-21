"""Deterministic contracts and aggregation for read-only coding analysis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant_agent.coding.models import (
    CodingAnalysisFinding,
    CodingAnalysisResult,
    CodingAnalysisSnapshot,
    CodingAnalysisTask,
)

ANALYSIS_TASK_IDS = (
    "structure_context",
    "change_test_impact",
    "safety_governance",
)
ANALYSIS_READ_TOOL_NAMES = (
    "coding_repo_list",
    "coding_repo_search",
    "coding_repo_read",
    "coding_repo_status",
    "coding_repo_diff",
)
MAX_FINDINGS_PER_TASK = 12
MAX_TASK_CONTEXT_CHARS = 6_000
MAX_ANALYSIS_CONTEXT_CHARS = 24_000

AnalysisStatus = Literal["completed", "partial", "unavailable"]


class _RawAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["succeeded", "failed", "stale"]
    findings: tuple[dict[str, object], ...] = Field(max_length=MAX_FINDINGS_PER_TASK)
    covered_paths: tuple[str, ...] = Field(max_length=128)
    output_digest: object | None = None
    error_code: str | None = None

    @field_validator("findings", "covered_paths", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


def build_analysis_tasks() -> tuple[CodingAnalysisTask, ...]:
    """Return the complete static read-only analysis inventory."""

    objectives = {
        "structure_context": (
            "Identify relevant modules, interfaces, data flow, and existing "
            "implementation patterns in the frozen workspace snapshot."
        ),
        "change_test_impact": (
            "Identify likely change surfaces, test entry points, compatibility "
            "constraints, and regression risks in the frozen workspace snapshot."
        ),
        "safety_governance": (
            "Identify permission, credential, network, path, persistence, HITL, "
            "and governance boundaries in the frozen workspace snapshot."
        ),
    }
    return tuple(
        CodingAnalysisTask(
            task_id=task_id,
            dimension=task_id,
            objective=objectives[task_id],
            allowed_tool_names=ANALYSIS_READ_TOOL_NAMES,
        )
        for task_id in ANALYSIS_TASK_IDS
    )


def normalize_analysis_result(
    *,
    task: CodingAnalysisTask,
    snapshot: CodingAnalysisSnapshot,
    raw_result: Mapping[str, object],
) -> CodingAnalysisResult:
    """Bind untrusted model output to trusted task and snapshot facts."""

    raw = _RawAnalysisResult.model_validate(raw_result)
    findings: list[CodingAnalysisFinding] = []
    for raw_finding in raw.findings:
        payload = dict(raw_finding)
        payload.pop("finding_id", None)
        payload["finding_id"] = "0" * 64
        parsed = CodingAnalysisFinding.model_validate(payload)
        finding_payload = parsed.model_dump(
            mode="json",
            exclude={"finding_id"},
        )
        findings.append(
            parsed.model_copy(
                update={
                    "finding_id": _canonical_digest(
                        {"task_id": task.task_id, **finding_payload}
                    )
                }
            )
        )

    normalized = CodingAnalysisResult(
        task_id=task.task_id,
        snapshot_ref=snapshot.snapshot_ref,
        tree_digest=snapshot.tree_digest,
        status=raw.status,
        findings=tuple(sorted(findings, key=lambda item: item.finding_id)),
        covered_paths=tuple(sorted(set(raw.covered_paths))),
        output_digest="0" * 64,
        error_code=raw.error_code,
    )
    return normalized.model_copy(
        update={"output_digest": _result_output_digest(normalized)}
    )


def merge_analysis_results(
    current: Sequence[CodingAnalysisResult] | None,
    update: Sequence[CodingAnalysisResult] | None,
) -> list[CodingAnalysisResult]:
    """Replace replayed worker output by stable task ID in fixed order."""

    by_id = {item.task_id: item for item in current or ()}
    by_id.update({item.task_id: item for item in update or ()})
    return [by_id[task_id] for task_id in ANALYSIS_TASK_IDS if task_id in by_id]


def join_analysis_results(
    snapshot: CodingAnalysisSnapshot,
    results: Sequence[CodingAnalysisResult] | None,
) -> tuple[AnalysisStatus, tuple[CodingAnalysisResult, ...]]:
    """Validate, order, and deduplicate snapshot-bound worker results."""

    ordered = merge_analysis_results((), results)
    seen_evidence: set[tuple[str | None, str, str]] = set()
    joined: list[CodingAnalysisResult] = []
    for result in ordered:
        if (
            result.snapshot_ref != snapshot.snapshot_ref
            or result.tree_digest != snapshot.tree_digest
        ):
            raise ValueError("coding_analysis_snapshot_mismatch")
        if result.output_digest != _result_output_digest(result):
            raise ValueError("coding_analysis_contract_invalid")

        findings: list[CodingAnalysisFinding] = []
        for finding in sorted(result.findings, key=lambda item: item.finding_id):
            evidence_key = (
                finding.path,
                finding.category,
                finding.evidence_digest,
            )
            if evidence_key in seen_evidence:
                continue
            seen_evidence.add(evidence_key)
            findings.append(finding)

        normalized = result.model_copy(
            update={
                "findings": tuple(findings[:MAX_FINDINGS_PER_TASK]),
                "covered_paths": tuple(sorted(set(result.covered_paths))),
                "output_digest": "0" * 64,
            }
        )
        joined.append(
            normalized.model_copy(
                update={"output_digest": _result_output_digest(normalized)}
            )
        )

    succeeded = sum(item.status == "succeeded" for item in joined)
    if succeeded == len(ANALYSIS_TASK_IDS) and len(joined) == len(ANALYSIS_TASK_IDS):
        status: AnalysisStatus = "completed"
    elif succeeded:
        status = "partial"
    else:
        status = "unavailable"
    return status, tuple(joined)


def render_analysis_context(
    status: AnalysisStatus,
    results: Sequence[CodingAnalysisResult],
) -> str:
    """Render complete finding objects within deterministic per-task budgets."""

    task_payloads: list[dict[str, object]] = []
    for result in merge_analysis_results((), results):
        task_payload: dict[str, object] = {
            "task_id": result.task_id,
            "status": result.status,
            "output_digest": result.output_digest,
            "error_code": result.error_code,
            "covered_paths": [],
            "covered_paths_truncated": False,
            "findings": [],
            "truncated": False,
        }
        rendered_findings = task_payload["findings"]
        assert isinstance(rendered_findings, list)
        for finding in sorted(result.findings, key=lambda item: item.finding_id):
            candidate = [*rendered_findings, finding.model_dump(mode="json")]
            candidate_payload = {**task_payload, "findings": candidate, "truncated": True}
            if len(_canonical_json(candidate_payload)) > MAX_TASK_CONTEXT_CHARS:
                task_payload["truncated"] = True
                break
            rendered_findings.append(finding.model_dump(mode="json"))

        rendered_paths = task_payload["covered_paths"]
        assert isinstance(rendered_paths, list)
        for path in sorted(set(result.covered_paths)):
            candidate = [*rendered_paths, path]
            candidate_payload = {
                **task_payload,
                "covered_paths": candidate,
                "covered_paths_truncated": True,
            }
            if len(_canonical_json(candidate_payload)) > MAX_TASK_CONTEXT_CHARS:
                task_payload["covered_paths_truncated"] = True
                break
            rendered_paths.append(path)
        task_payloads.append(task_payload)

    payload = {
        "trust": "advisory",
        "analysis_status": status,
        "tasks": task_payloads,
    }
    rendered = _canonical_json(payload)
    if len(rendered) > MAX_ANALYSIS_CONTEXT_CHARS:
        raise ValueError("coding_analysis_context_limit_exceeded")
    return rendered


def _result_output_digest(result: CodingAnalysisResult) -> str:
    return _canonical_digest(
        result.model_dump(mode="json", exclude={"output_digest"})
    )


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ANALYSIS_READ_TOOL_NAMES",
    "ANALYSIS_TASK_IDS",
    "MAX_ANALYSIS_CONTEXT_CHARS",
    "MAX_FINDINGS_PER_TASK",
    "MAX_TASK_CONTEXT_CHARS",
    "build_analysis_tasks",
    "join_analysis_results",
    "merge_analysis_results",
    "normalize_analysis_result",
    "render_analysis_context",
]
