"""Deterministic contracts and aggregation for coding review workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, NotRequired, Required

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphBubbleUp, NodeCancelledError
from langgraph.runtime import Runtime
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant_agent.coding.models import (
    CODING_REVIEW_TASK_SPECS,
    CodingAnalysisSnapshot,
    CodingReviewEvidence,
    CodingReviewFinding,
    CodingReviewInput,
    CodingReviewReport,
    CodingReviewTask,
    CodingReviewerResult,
)
from assistant_agent.coding.tools import build_coding_analysis_tools
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.coding_phase import CodingAnalysisPhaseMiddleware
from assistant_agent.native_agent.providers import coding_analysis_model_view
from assistant_agent.native_agent.providers import coding_analysis_model_settings

REVIEW_TASK_IDS = tuple(CODING_REVIEW_TASK_SPECS)
MAX_REVIEW_RESULT_JSON_CHARS = 16_000
MAX_REVIEW_REPORT_JSON_CHARS = 48_000
_SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4}
REVIEW_READ_TOOL_NAMES = (
    "coding_repo_diff",
    "coding_repo_list",
    "coding_repo_read",
    "coding_repo_search",
    "coding_repo_status",
)
_CODING_REVIEW_PROMPT = (
    "You are a final code-review worker. Review only the immutable, snapshot-bound "
    "workspace exposed by the provided coding_repo_* read tools. Return bounded "
    "structured findings for the assigned review dimension. Do not propose or apply "
    "patches, execute commands, validate, integrate, access the network, request "
    "credentials, or modify files."
)


class _ReviewEvidenceResponse(BaseModel):
    """Untrusted evidence before the worker binds it to its fixed review task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=240)
    line: int = Field(ge=1, le=10_000_000)
    excerpt: str = Field(min_length=1, max_length=800)


class _ReviewFindingResponse(BaseModel):
    """Untrusted finding shape returned by a read-only review worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    severity: Literal["critical", "high", "moderate", "low", "info"]
    category: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    summary: str = Field(min_length=1, max_length=1_200)
    evidence: tuple[_ReviewEvidenceResponse, ...] = Field(min_length=1, max_length=2)

    @field_validator("evidence", mode="before")
    @classmethod
    def _tuple_evidence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingReviewResponse(BaseModel):
    """Public, untrusted structured response before trusted snapshot binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["succeeded", "failed", "stale"]
    findings: tuple[_ReviewFindingResponse, ...] = Field(max_length=8)
    error_code: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^coding_review_[a-z0-9_]+$",
    )

    @field_validator("findings", mode="before")
    @classmethod
    def _tuple_findings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingReviewWorkerState(AgentState):
    """Narrow state delivered to one final snapshot review worker."""

    coding_repo_id: Required[str]
    workspace_ref: Required[str]
    base_commit: Required[str]
    analysis_snapshot: Required[CodingAnalysisSnapshot]
    review_input: Required[CodingReviewInput]
    review_task: Required[CodingReviewTask]
    provider_search_profile: Required[Literal["none"]]


class CodingReviewGraphState(AgentState):
    """Ephemeral final-review state, separate from the mutation graph state."""

    coding_repo_id: Required[str]
    workspace_ref: Required[str]
    base_commit: Required[str]
    review_snapshot: Required[CodingAnalysisSnapshot]
    review_input: Required[CodingReviewInput]
    review_tasks: NotRequired[tuple[CodingReviewTask, ...]]
    review_results: NotRequired[
        Annotated[list[CodingReviewerResult], merge_review_results]
    ]
    review_report: NotRequired[CodingReviewReport]
    review_status: NotRequired[Literal["clean", "findings", "unavailable"]]


@dataclass(frozen=True)
class _ReviewReadObservation:
    """Trusted projection of one snapshot-bound coding_repo_read Tool artifact."""

    path: str
    start_line: int
    lines: tuple[str, ...]
    content_digest: str


def _review_propagation_failure(error: BaseException) -> BaseException | None:
    """Return native control or permission failures hidden by wrappers."""

    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(
            current,
            (
                asyncio.CancelledError,
                GraphBubbleUp,
                NodeCancelledError,
                PermissionError,
            ),
        ):
            return current
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            pending.append(reason)
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
        if isinstance(current.__context__, BaseException):
            pending.append(current.__context__)
    return None


def _validated_review_inventory(value: object) -> tuple[CodingReviewerResult, ...]:
    """Validate raw checkpoint inventory before any task-ID replacement merge."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("coding_review_contract_invalid")
    try:
        results = tuple(CodingReviewerResult.model_validate(item) for item in value)
    except (TypeError, ValueError):
        raise ValueError("coding_review_contract_invalid") from None
    seen: set[str] = set()
    for result in results:
        if result.task_id not in REVIEW_TASK_IDS:
            raise ValueError("coding_review_unknown_task")
        if result.task_id in seen:
            raise ValueError("coding_review_duplicate_task")
        seen.add(result.task_id)
    return results


def build_coding_review_tools(service: CodingWorkspaceService) -> list[BaseTool]:
    """Build the final-review-specific, snapshot-bound read-only tool profile."""

    tools = build_coding_analysis_tools(service)
    if tuple(tool.name for tool in tools) != REVIEW_READ_TOOL_NAMES:
        raise ValueError("coding_review_tool_profile_invalid")
    return tools


def build_review_tasks() -> tuple[CodingReviewTask, ...]:
    """Return the complete fixed review inventory in canonical order."""

    return tuple(
        CodingReviewTask(task_id=task_id, dimension=task_id, objective=objective)
        for task_id, objective in CODING_REVIEW_TASK_SPECS.items()
    )


def merge_review_results(
    current: Sequence[CodingReviewerResult] | None,
    update: Sequence[CodingReviewerResult] | None,
) -> list[CodingReviewerResult]:
    """Merge concurrent worker updates in fixed task order."""

    if update is not None and not update:
        return []
    by_task_id = {result.task_id: result for result in current or ()}
    by_task_id.update({result.task_id: result for result in update or ()})
    return [by_task_id[task_id] for task_id in REVIEW_TASK_IDS if task_id in by_task_id]


def prepare_review_tasks(state: Mapping[str, object]) -> dict[str, object]:
    """Freeze the final review input and its fixed three-worker inventory."""

    snapshot = CodingAnalysisSnapshot.model_validate(
        state.get("review_snapshot", state.get("analysis_snapshot"))
    )
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    if (
        review_input.workspace_ref != snapshot.workspace_ref
        or review_input.base_commit != snapshot.base_commit
        or review_input.workspace_diff_digest != snapshot.workspace_diff_digest
        or review_input.snapshot_created_at != snapshot.created_at
        or review_input.snapshot_expires_at != snapshot.expires_at
        or state.get("workspace_ref") != review_input.workspace_ref
        or state.get("base_commit") != review_input.base_commit
    ):
        raise ValueError("coding_review_binding_mismatch")
    return {
        "review_snapshot": snapshot,
        "review_input": review_input,
        "review_tasks": build_review_tasks(),
        "review_results": [],
        "review_report": None,
        "review_status": None,
    }


def route_review_workers(state: Mapping[str, object]) -> list[Send] | str:
    """Fan out each unfinished canonical task exactly once to the review worker."""

    snapshot = CodingAnalysisSnapshot.model_validate(state.get("review_snapshot"))
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    tasks = tuple(CodingReviewTask.model_validate(task) for task in state.get("review_tasks", ()))
    if tasks != build_review_tasks():
        raise ValueError("coding_review_contract_invalid")
    completed = {
        result.task_id
        for result in _validated_review_inventory(state.get("review_results", ()))
    }
    pending = tuple(task for task in tasks if task.task_id not in completed)
    if not pending:
        return "join_review"
    return [
        Send(
            "review_workspace",
            {
                "messages": _review_worker_messages(task, snapshot, review_input),
                "coding_repo_id": state["coding_repo_id"],
                "workspace_ref": review_input.workspace_ref,
                "base_commit": review_input.base_commit,
                "analysis_snapshot": snapshot,
                "review_input": review_input,
                "review_task": task,
                "provider_search_profile": "none",
            },
        )
        for task in pending
    ]


async def review_workspace(
    state: Mapping[str, object],
    runtime: Runtime[AssistantRunContext] | None,
    *,
    review_agent: Any | None = None,
    config: RunnableConfig | None = None,
) -> dict[str, object]:
    """Run one worker and bind all output to its task and frozen final snapshot."""

    task = CodingReviewTask.model_validate(state.get("review_task"))
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    snapshot = CodingAnalysisSnapshot.model_validate(state.get("analysis_snapshot"))
    if (
        snapshot.workspace_ref != review_input.workspace_ref
        or snapshot.base_commit != review_input.base_commit
        or snapshot.workspace_diff_digest != review_input.workspace_diff_digest
        or snapshot.created_at != review_input.snapshot_created_at
        or snapshot.expires_at != review_input.snapshot_expires_at
    ):
        return {"review_results": [_review_failure_result(task, review_input)]}
    try:
        if review_agent is None:
            raw_result = state.get("structured_response")
        else:
            response = await review_agent.ainvoke(
                dict(state),
                config=config,
                context=runtime.context if runtime is not None else None,
            )
            raw_result = response.get("structured_response")
        observations = _review_read_observations(
            response if review_agent is not None else state,
            snapshot,
        )
        if isinstance(raw_result, BaseModel):
            raw_result = raw_result.model_dump(mode="json")
        if not isinstance(raw_result, Mapping):
            raise ValueError("coding_review_contract_invalid")
        normalized = _normalize_review_result(
            task,
            review_input,
            raw_result,
            observations=observations,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        propagation_failure = _review_propagation_failure(exc)
        if propagation_failure is not None:
            raise propagation_failure
        normalized = _review_failure_result(task, review_input)
    return {"review_results": [normalized]}


def join_review(state: Mapping[str, object]) -> dict[str, object]:
    """Canonicalize the completed final-review workers without mutating the snapshot."""

    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    results = merge_review_results(
        (),
        _validated_review_inventory(state.get("review_results", ())),
    )
    if {result.task_id for result in results} != set(REVIEW_TASK_IDS):
        return {"review_results": results}
    report = canonicalize_review_report(review_input, results)
    return {
        "review_results": results,
        "review_report": report,
        "review_status": report.status,
    }


def create_coding_review_graph(
    model: Any | None = None,
    workspace_service: CodingWorkspaceService | None = None,
    *,
    review_agent: Any | None = None,
):
    """Compile a fresh, read-only three-worker final-review subgraph."""

    if review_agent is None:
        if model is None or workspace_service is None:
            raise ValueError("coding_review_graph_requires_model_and_workspace")
        review_model = coding_analysis_model_view(model)
        review_agent = create_agent(
            model=review_model,
            tools=build_coding_review_tools(workspace_service),
            system_prompt=_CODING_REVIEW_PROMPT,
            response_format=CodingReviewResponse,
            state_schema=CodingReviewWorkerState,
            context_schema=AssistantRunContext,
            middleware=[
                CodingAnalysisPhaseMiddleware(
                    coding_analysis_model_settings(review_model)
                ),
                ModelCallLimitMiddleware(run_limit=1, exit_behavior="error"),
                ToolCallLimitMiddleware(run_limit=8, exit_behavior="error"),
            ],
            name="AssistantCodingReviewAgent",
        )

    async def review_workspace_node(
        state: CodingReviewWorkerState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        return await review_workspace(
            state,
            runtime,
            review_agent=review_agent,
            config=config,
        )

    builder = StateGraph(CodingReviewGraphState, context_schema=AssistantRunContext)
    builder.add_node("prepare_review_tasks", prepare_review_tasks)
    builder.add_node(
        "review_workspace",
        review_workspace_node,
        input_schema=CodingReviewWorkerState,
    )
    builder.add_node("join_review", join_review, defer=True)
    builder.add_edge(START, "prepare_review_tasks")
    builder.add_conditional_edges(
        "prepare_review_tasks",
        route_review_workers,
        ["review_workspace", "join_review"],
    )
    builder.add_edge("review_workspace", "join_review")
    builder.add_edge("join_review", END)
    return builder.compile(name="AssistantCodingReviewGraph")


def _review_worker_messages(
    task: CodingReviewTask,
    snapshot: CodingAnalysisSnapshot,
    review_input: CodingReviewInput,
) -> list[HumanMessage]:
    context = {
        "task_id": task.task_id,
        "objective": task.objective,
        "snapshot_ref": snapshot.snapshot_ref,
        "tree_digest": snapshot.tree_digest,
        "workspace_ref": review_input.workspace_ref,
        "base_commit": review_input.base_commit,
        "patch_digest": review_input.patch_digest,
        "workspace_diff_digest": review_input.workspace_diff_digest,
        "instruction": "Review only the immutable final snapshot. Return structured findings.",
    }
    return [HumanMessage(content=_canonical_json(context))]


def _normalize_review_result(
    task: CodingReviewTask,
    review_input: CodingReviewInput,
    raw_result: Mapping[str, object],
    *,
    observations: Sequence[_ReviewReadObservation] | None = None,
) -> CodingReviewerResult:
    raw = CodingReviewResponse.model_validate(raw_result)
    findings: list[CodingReviewFinding] = []
    for raw_finding in raw.findings:
        evidence = tuple(
            CodingReviewEvidence(
                path=item.path,
                line=item.line,
                excerpt=item.excerpt,
                evidence_digest=_canonical_digest(
                    {"path": item.path, "line": item.line, "excerpt": item.excerpt}
                ),
            )
            for item in raw_finding.evidence
        )
        semantic_payload = {
            "severity": raw_finding.severity,
            "category": raw_finding.category,
            "summary": raw_finding.summary,
        }
        evidence_payload = [
            item.model_dump(mode="json", exclude={"evidence_digest"})
            for item in evidence
        ]
        if observations is not None:
            _validate_finding_evidence(evidence, observations)
        findings.append(
            CodingReviewFinding(
                finding_id=_canonical_digest(
                    {"task_id": task.task_id, **semantic_payload, "evidence": evidence_payload}
                ),
                task_id=task.task_id,
                **semantic_payload,
                evidence=evidence,
                semantic_key=_canonical_digest(semantic_payload),
            )
        )
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "workspace_ref": review_input.workspace_ref,
        "base_commit": review_input.base_commit,
        "patch_digest": review_input.patch_digest,
        "workspace_diff_digest": review_input.workspace_diff_digest,
        "snapshot_created_at": review_input.snapshot_created_at,
        "snapshot_expires_at": review_input.snapshot_expires_at,
        "status": raw.status,
        "findings": tuple(sorted(findings, key=lambda item: item.finding_id)),
        "error_code": raw.error_code,
    }
    if raw.status != "succeeded":
        payload["findings"] = ()
    review_input_json = review_input.model_dump(mode="json")
    digest_payload = {
        **payload,
        "snapshot_created_at": review_input_json["snapshot_created_at"],
        "snapshot_expires_at": review_input_json["snapshot_expires_at"],
        "findings": [
            item.model_dump(mode="json")
            for item in payload["findings"]
        ],
    }
    if (
        len(
            _canonical_json(
                {**digest_payload, "output_digest": "0" * 64}
            )
        )
        > MAX_REVIEW_RESULT_JSON_CHARS
    ):
        raise ValueError("coding_review_result_limit_exceeded")
    return CodingReviewerResult(
        **payload,
        output_digest=_canonical_digest(digest_payload),
    )


def _review_read_observations(
    response: Mapping[str, object],
    snapshot: CodingAnalysisSnapshot,
) -> tuple[_ReviewReadObservation, ...]:
    messages = response.get("messages", ())
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ValueError("coding_review_observation_invalid")
    observations: list[_ReviewReadObservation] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name != "coding_repo_read":
            continue
        artifact = message.artifact
        if not isinstance(artifact, Mapping):
            raise ValueError("coding_review_observation_invalid")
        payload = artifact.get("data", artifact)
        if not isinstance(payload, Mapping):
            raise ValueError("coding_review_observation_invalid")
        if (
            payload.get("snapshot_ref") != snapshot.snapshot_ref
            or payload.get("tree_digest") != snapshot.tree_digest
        ):
            raise ValueError("coding_review_observation_mismatch")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("coding_review_observation_invalid")
        path = result.get("path")
        content = result.get("content")
        start_line = result.get("start_line")
        end_line = result.get("end_line")
        if (
            not isinstance(path, str)
            or not isinstance(content, str)
            or not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or end_line < start_line - 1
        ):
            raise ValueError("coding_review_observation_invalid")
        lines = tuple(content.splitlines())
        if end_line != start_line + len(lines) - 1:
            raise ValueError("coding_review_observation_invalid")
        observations.append(
            _ReviewReadObservation(
                path=path,
                start_line=start_line,
                lines=lines,
                content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(observations)


def _validate_finding_evidence(
    evidence: Sequence[CodingReviewEvidence],
    observations: Sequence[_ReviewReadObservation],
) -> None:
    for item in evidence:
        matches = [
            observation
            for observation in observations
            if observation.path == item.path
            and observation.start_line <= item.line
            < observation.start_line + len(observation.lines)
            and observation.content_digest
        ]
        if not any(
            item.excerpt
            in observation.lines[item.line - observation.start_line]
            for observation in matches
        ):
            raise ValueError("coding_review_evidence_unobserved")


def _review_failure_result(
    task: CodingReviewTask,
    review_input: CodingReviewInput,
) -> CodingReviewerResult:
    return _normalize_review_result(
        task,
        review_input,
        {
            "status": "failed",
            "findings": (),
            "error_code": "coding_review_task_failed",
        },
    )


def canonicalize_review_report(
    review_input: CodingReviewInput,
    results: Sequence[CodingReviewerResult],
) -> CodingReviewReport:
    """Validate the fixed reviewer inventory and return its canonical report."""

    by_task_id: dict[str, CodingReviewerResult] = {}
    for result in results:
        if result.task_id not in REVIEW_TASK_IDS:
            raise ValueError("coding_review_unknown_task")
        if result.task_id in by_task_id:
            raise ValueError("coding_review_duplicate_task")
        _validate_result(result, review_input)
        by_task_id[result.task_id] = result

    missing = [task_id for task_id in REVIEW_TASK_IDS if task_id not in by_task_id]
    if missing:
        raise ValueError("coding_review_missing_task")

    ordered_results = tuple(by_task_id[task_id] for task_id in REVIEW_TASK_IDS)
    findings = _canonical_findings(ordered_results)
    status = _report_status(ordered_results, findings)
    unsigned = CodingReviewReport(
        status=status,
        workspace_ref=review_input.workspace_ref,
        base_commit=review_input.base_commit,
        patch_digest=review_input.patch_digest,
        workspace_diff_digest=review_input.workspace_diff_digest,
        snapshot_created_at=review_input.snapshot_created_at,
        snapshot_expires_at=review_input.snapshot_expires_at,
        results=ordered_results,
        findings=findings,
        report_digest="0" * 64,
    )
    payload = unsigned.model_dump(mode="json", exclude={"report_digest"})
    signed_payload = {**payload, "report_digest": "0" * 64}
    if len(_canonical_json(signed_payload)) > MAX_REVIEW_REPORT_JSON_CHARS:
        raise ValueError("coding_review_report_limit_exceeded")
    return unsigned.model_copy(update={"report_digest": _canonical_digest(payload)})


def _validate_result(result: CodingReviewerResult, review_input: CodingReviewInput) -> None:
    if (
        result.workspace_ref != review_input.workspace_ref
        or result.base_commit != review_input.base_commit
        or result.patch_digest != review_input.patch_digest
        or result.workspace_diff_digest != review_input.workspace_diff_digest
        or result.snapshot_created_at != review_input.snapshot_created_at
        or result.snapshot_expires_at != review_input.snapshot_expires_at
    ):
        raise ValueError("coding_review_binding_mismatch")
    payload = result.model_dump(mode="json", exclude={"output_digest"})
    if result.output_digest != _canonical_digest(payload):
        raise ValueError("coding_review_contract_invalid")
    signed_payload = result.model_dump(mode="json")
    if len(_canonical_json(signed_payload)) > MAX_REVIEW_RESULT_JSON_CHARS:
        raise ValueError("coding_review_result_limit_exceeded")
    for finding in result.findings:
        if finding.task_id != result.task_id:
            raise ValueError("coding_review_finding_task_mismatch")


def _canonical_findings(
    results: Sequence[CodingReviewerResult],
) -> tuple[CodingReviewFinding, ...]:
    seen: set[tuple[str, str]] = set()
    selected: list[CodingReviewFinding] = []
    for result in results:
        for finding in result.findings:
            evidence_key = _canonical_json(
                [item.model_dump(mode="json") for item in finding.evidence]
            )
            deduplication_key = (evidence_key, finding.semantic_key)
            if deduplication_key in seen:
                continue
            seen.add(deduplication_key)
            selected.append(finding)
    return tuple(sorted(selected, key=_finding_sort_key))


def _finding_sort_key(finding: CodingReviewFinding) -> tuple[int, str, str, int, str]:
    first_evidence = min(
        finding.evidence,
        key=lambda item: (item.path, item.line, item.evidence_digest),
    )
    return (
        _SEVERITY_ORDER[finding.severity],
        finding.task_id,
        first_evidence.path,
        first_evidence.line,
        finding.finding_id,
    )


def _report_status(
    results: Sequence[CodingReviewerResult],
    findings: Sequence[CodingReviewFinding],
) -> str:
    if any(result.status != "succeeded" for result in results):
        return "unavailable"
    return "findings" if findings else "clean"


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CodingReviewResponse",
    "MAX_REVIEW_REPORT_JSON_CHARS",
    "MAX_REVIEW_RESULT_JSON_CHARS",
    "REVIEW_READ_TOOL_NAMES",
    "REVIEW_TASK_IDS",
    "build_coding_review_tools",
    "build_review_tasks",
    "canonicalize_review_report",
    "create_coding_review_graph",
    "join_review",
    "merge_review_results",
    "prepare_review_tasks",
    "review_workspace",
    "route_review_workers",
]
